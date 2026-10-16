"""Tests for the ~/.kiro/sessions/cli reader (synthetic data only)."""

import json
import tempfile
import unittest
from pathlib import Path

from kiro_usage import cli_sessions as cs
from kiro_usage import fmt_credits, price_for


def _text(s):
    return {"kind": "text", "data": s}


def _prompt(mid, text, ts=1_700_000_000):
    return {"version": "v1", "kind": "Prompt",
            "data": {"message_id": mid, "content": [_text(text)], "meta": {"timestamp": ts}}}


def _asst(mid, text, tool=None):
    content = [_text(text)]
    if tool:
        content.append({"kind": "toolUse", "data": {"toolUseId": "t", "name": tool, "input": {"a": "x" * 40}}})
    return {"version": "v1", "kind": "AssistantMessage", "data": {"message_id": mid, "content": content}}


def _tool_result(mid, text):
    return {"version": "v1", "kind": "ToolResults",
            "data": {"message_id": mid, "content": [], "results": {}},
            "_content": [{"kind": "toolResult", "data": {"toolUseId": "t", "content": [_text(text)]}}]}


def _turn(ids, credits, reqs=2, pct=None, model="claude-sonnet-5.5", end="2026-10-15T10:00:00.123456789Z"):
    t = {"message_ids": ids, "total_request_count": reqs, "end_timestamp": end, "model": model,
         "input_token_count": 0, "output_token_count": 0,
         "metering_usage": [{"value": v, "unit": "credit", "unitPlural": "credits"} for v in credits]}
    if pct is not None:
        t["final_context_usage_percentage"] = pct
    return t


def _write(dirpath, sid, turns, records, window=1_000_000, created="2026-10-15T09:00:00Z",
           updated="2026-10-15T10:00:00Z", raw_tail=""):
    meta = {"session_id": sid, "cwd": "/tmp/proj", "created_at": created, "updated_at": updated,
            "title": "t", "session_created_reason": "user",
            "session_state": {"conversation_metadata": {"user_turn_metadatas": turns},
                              "rts_model_state": {"model_info": {"model_id": "claude-sonnet-5.5",
                                                                 "context_window_tokens": window}}}}
    p = Path(dirpath) / (sid + ".json")
    p.write_text(json.dumps(meta))
    lines = []
    for r in records:
        content = r.pop("_content", None)
        if content is not None:
            r["data"]["content"] = content
        lines.append(json.dumps(r))
    (Path(dirpath) / (sid + ".jsonl")).write_text("\n".join(lines) + "\n" + raw_tail)
    return p


class HelpersTest(unittest.TestCase):
    def test_parse_iso_ms_handles_nanoseconds_and_garbage(self):
        self.assertEqual(cs.parse_iso_ms("2026-10-15T10:00:00Z"), 1792058400000)
        self.assertEqual(cs.parse_iso_ms("2026-10-15T10:00:00.123456789Z"), 1792058400123)
        self.assertIsNone(cs.parse_iso_ms("nope"))
        self.assertIsNone(cs.parse_iso_ms(None))

    def test_credits_of_sums_only_credit_units(self):
        meta = {"metering_usage": [{"value": 1.5, "unit": "credit"}, {"value": 0.25, "unit": "credit"},
                                   {"value": 99, "unit": "second"}, {"value": None, "unit": "credit"}]}
        self.assertAlmostEqual(cs.credits_of(meta), 1.75)
        self.assertEqual(cs.credits_of(None), 0.0)
        self.assertEqual(cs.credits_of({}), 0.0)

    def test_price_family_fallback(self):
        self.assertEqual(price_for("claude-sonnet-5.5"), price_for("claude-sonnet-4.6"))
        self.assertEqual(price_for("claude-opus-5.5"), price_for("claude-opus-4.6"))
        self.assertEqual(price_for("claude-haiku-4.5")[0], 1.25)

    def test_fmt_credits(self):
        self.assertEqual(fmt_credits(0), "0")
        self.assertEqual(fmt_credits(0.001), "<0.01")
        self.assertEqual(fmt_credits(1.234), "1.23")
        self.assertEqual(fmt_credits(12.34), "12.3")
        self.assertEqual(fmt_credits(1234.5), "1234")


class ParseSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _basic(self, **kw):
        records = [_prompt("p1", "a" * 400), _asst("a1", "b" * 200, tool="shell"),
                   _tool_result("r1", "c" * 800), _asst("a2", "d" * 100)]
        turns = [_turn(["p1", "a2"], [1.0, 0.5], reqs=2, **kw)]
        return _write(self.tmp.name, "sess-1", turns, records)

    def test_credits_requests_model_are_taken_from_metadata(self):
        s = cs.parse_session(self._basic())
        self.assertEqual(len(s["rows"]), 1)
        r = s["rows"][0]
        self.assertAlmostEqual(r["credits"], 1.5)
        self.assertEqual(r["reqs"], 2)
        self.assertEqual(r["model"], "claude-sonnet-5.5")
        self.assertEqual(r["tools"], ["shell"])

    def test_token_estimate_follows_cache_model(self):
        r = cs.parse_session(self._basic())["rows"][0]
        # request 1: prompt 400/4=100 new, nothing cached.
        # request 2: tool result 200 + request-1 assistant (50 text + toolUse) new, 100 + 0 cached? -> ctx grows
        self.assertGreater(r["cw"], 100)
        self.assertGreater(r["cr"], 100)          # request 2 re-reads the prompt
        self.assertAlmostEqual(r["out"], (200 + 100) / 4 + (len("shell") + len(json.dumps({"a": "x" * 40}, separators=(",", ":")))) / 4)

    def test_baseline_is_derived_from_context_percentage(self):
        plain = cs.parse_session(self._basic())["rows"][0]
        with_pct = cs.parse_session(self._basic(pct=10.0))["rows"][0]   # 10% of 1M = 100k real context
        self.assertGreater(with_pct["cw"], plain["cw"] + 90_000)
        self.assertGreater(with_pct["cr"], plain["cr"] + 90_000)         # re-sent on request 2
        self.assertGreater(with_pct["cost"], plain["cost"])

    def test_corrupt_and_blank_lines_are_skipped(self):
        p = _write(self.tmp.name, "sess-2", [_turn(["p1", "a1"], [2.0], reqs=1)],
                   [_prompt("p1", "hello"), _asst("a1", "world")],
                   raw_tail="\n\x00\x00\x00 not json\n")
        s = cs.parse_session(p)
        self.assertAlmostEqual(s["rows"][0]["credits"], 2.0)

    def test_unrecorded_turn_still_counts_tokens_but_not_credits(self):
        p = _write(self.tmp.name, "sess-3", [], [_prompt("p1", "x" * 40), _asst("a1", "y" * 40)])
        s = cs.parse_session(p)
        self.assertEqual(len(s["rows"]), 1)
        self.assertEqual(s["rows"][0]["credits"], 0.0)
        self.assertEqual(s["rows"][0]["reqs"], 1)
        self.assertGreater(s["rows"][0]["out"], 0)

    def test_clear_resets_the_cached_context(self):
        recs = [_prompt("p1", "a" * 4000), _asst("a1", "b" * 40), {"version": "v1", "kind": "Clear"},
                _prompt("p2", "c" * 40), _asst("a2", "d" * 40)]
        turns = [_turn(["p1", "a1"], [1], reqs=1), _turn(["p2", "a2"], [1], reqs=1)]
        s = cs.parse_session(_write(self.tmp.name, "sess-4", turns, recs))
        self.assertEqual(s["rows"][1]["cr"], 0)

    def test_unreadable_metadata_returns_none(self):
        p = Path(self.tmp.name) / "bad.json"
        p.write_text("{not json")
        self.assertIsNone(cs.parse_session(p))
        p.write_text(json.dumps({"no": "id"}))
        self.assertIsNone(cs.parse_session(p))

    def test_summarize_filters_by_cutoff_and_groups_by_day(self):
        turns = [_turn(["p1", "a1"], [1.0], reqs=1, end="2026-10-14T10:00:00Z"),
                 _turn(["p2", "a2"], [2.0], reqs=1, end="2026-10-15T10:00:00Z")]
        recs = [_prompt("p1", "x"), _asst("a1", "y"), _prompt("p2", "x"), _asst("a2", "y")]
        s = cs.parse_session(_write(self.tmp.name, "sess-5", turns, recs))
        full = cs.summarize(s)
        self.assertAlmostEqual(full["credits"], 3.0)
        self.assertEqual(full["turns"], 2)
        self.assertEqual(len(full["daily"]), 2)
        self.assertAlmostEqual(full["model_credits"]["claude-sonnet-5.5"], 3.0)
        cutoff = cs.parse_iso_ms("2026-10-15T00:00:00Z")
        part = cs.summarize(s, cutoff)
        self.assertAlmostEqual(part["credits"], 2.0)
        self.assertIsNone(cs.summarize(s, cs.parse_iso_ms("2026-12-01T00:00:00Z")))


if __name__ == "__main__":
    unittest.main()
