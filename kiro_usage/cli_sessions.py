"""Reader for the kiro-cli session store (~/.kiro/sessions/cli).

Newer kiro-cli versions no longer keep conversations in ``data.sqlite3``.
Each session is a pair of files::

    <session_id>.json    metadata + one entry per user turn (credits, model,
                         request count, timestamps; token counters are 0)
    <session_id>.jsonl   the transcript (Prompt / AssistantMessage /
                         ToolResults / Clear records)

What is *measured* by Kiro: credits (``metering_usage``), request counts,
model and timing.  Token counts are not recorded, so they are *estimated* from
the transcript (chars / 4, images by pixel count) with the same cache model the
legacy reader uses: every request re-sends the prior context (CacheRead) plus
whatever is new since the previous request (CacheWrite).
"""

import json
import re
import struct
from datetime import datetime, timezone

from . import CHARS_PER_TOKEN, CLI_SESSIONS_DIR, calc_cost

# Rough vision-token cost when image dimensions cannot be read.
_DEFAULT_IMAGE_TOKENS = 1600

_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:Z|[+-]00:?00)?$")


# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_iso_ms(value):
    """ISO-8601 (nanosecond precision, trailing Z) -> epoch milliseconds, or None."""
    if not isinstance(value, str):
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        dt = datetime.strptime(m.group(1) + "." + frac, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _image_tokens(data):
    """Estimate vision tokens for an image block (~(w*h)/750 for PNG)."""
    try:
        raw = (data or {}).get("source", {}).get("data")
        if isinstance(raw, list):
            head = bytes(raw[:24])
            if head[:4] == b"\x89PNG" and len(head) >= 24:
                w, h = struct.unpack(">II", head[16:24])
                return (w * h) // 750
    except Exception:
        pass
    return _DEFAULT_IMAGE_TOKENS


def _tool_result_tokens(data):
    total = 0.0
    for part in (data or {}).get("content") or []:
        if not isinstance(part, dict):
            continue
        kind, body = part.get("kind"), part.get("data")
        if kind == "image":
            total += _image_tokens(body)
        elif isinstance(body, str):
            total += len(body) / CHARS_PER_TOKEN
        elif body is not None:
            total += len(json.dumps(body, separators=(",", ":"))) / CHARS_PER_TOKEN
    return total


def _content_tokens(content):
    """Return (tokens_in_context, output_tokens_extra, tool_names) for one record.

    ``tokens_in_context`` is everything that stays in the transcript;
    ``output_tokens_extra`` is thinking text, which is generated but not re-sent.
    """
    ctx, thinking, tools = 0.0, 0.0, []
    for block in content if isinstance(content, list) else []:
        kind, data = block.get("kind"), block.get("data")
        if kind == "text" and isinstance(data, str):
            ctx += len(data) / CHARS_PER_TOKEN
        elif kind == "thinking" and isinstance(data, dict):
            thinking += len(data.get("text") or "") / CHARS_PER_TOKEN
        elif kind == "toolUse" and isinstance(data, dict):
            tools.append(data.get("name") or "?")
            ctx += (len(data.get("name") or "") +
                    len(json.dumps(data.get("input"), separators=(",", ":")))) / CHARS_PER_TOKEN
        elif kind == "toolResult":
            ctx += _tool_result_tokens(data)
        elif kind == "image":
            ctx += _image_tokens(data)
    return ctx, thinking, tools


def _read_records(path):
    try:
        fh = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # partial write / corrupt line
            if isinstance(rec, dict):
                yield rec


def credits_of(turn_meta):
    """Sum of credits in a turn's ``metering_usage`` list."""
    total = 0.0
    for m in (turn_meta or {}).get("metering_usage") or []:
        try:
            if str(m.get("unit", "credit")).lower().startswith("credit"):
                total += float(m.get("value") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _new_row(meta, ts_ms):
    return {
        "ts_ms": ts_ms, "model": None, "reqs": 0, "cw": 0.0, "cr": 0.0, "out": 0.0,
        "cost": 0.0, "credits": 0.0, "meta": meta, "tools": [], "has_transcript": False,
        "n_asst": 0, "ctx_end": 0.0,
    }


def _apply_baseline(rows, window):
    """Add the fixed per-request context (system prompt, tool definitions,
    steering files, skills) that is absent from the transcript.

    Kiro records ``final_context_usage_percentage`` per turn; times the model's
    context window that is the *real* context size at the end of the turn.  The
    part our transcript estimate cannot explain is that fixed overhead.  It is
    re-sent with every request, so it is a CacheWrite once per session and a
    CacheRead afterwards.  Turns without a percentage reuse the last known
    value; before any is known the overhead is taken as 0 (a lower bound).
    """
    base, first_done = 0.0, False
    for r in sorted((r for r in rows if r["n_asst"]), key=lambda r: r["ts_ms"]):
        pct = (r["meta"] or {}).get("final_context_usage_percentage")
        if window and isinstance(pct, (int, float)) and pct > 0:
            base = min(max(pct / 100.0 * window - r["ctx_end"], 0.0), float(window))
        n = r["n_asst"]
        if not first_done:
            r["cw"] += base
            r["cr"] += base * (n - 1)
            first_done = True
        else:
            r["cr"] += base * n


def _signature(meta_path):
    """(newest mtime in ms, total size) of a session's .json + .jsonl pair."""
    mt, size = 0, 0
    for p in (meta_path, meta_path.with_suffix(".jsonl")):
        try:
            st = p.stat()
        except OSError:
            continue
        mt = max(mt, int(st.st_mtime * 1000))
        size += st.st_size
    return mt, size


# ── Public API ────────────────────────────────────────────────────────────────
_cache = {}  # path -> (signature, parsed session)


def load_sessions(cutoff_ms=None):
    """Parsed sessions from the store; unchanged files are served from memory."""
    out = []
    for path in list_session_files():
        sig = _signature(path)
        if cutoff_ms and sig[0] < cutoff_ms:
            continue
        hit = _cache.get(path)
        if hit and hit[0] == sig:
            parsed = hit[1]
        else:
            parsed = parse_session(path)
            _cache[path] = (sig, parsed)
        if parsed:
            out.append(parsed)
    return out


def list_session_files():
    """Paths of ``*.json`` session metadata files in the store."""
    if not CLI_SESSIONS_DIR.is_dir():
        return []
    return list(CLI_SESSIONS_DIR.glob("*.json"))


def parse_session(meta_path):
    """Parse one session into a dict with per-turn ``rows``; None if unreadable."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not meta.get("session_id"):
        return None

    state = meta.get("session_state") or {}
    turns = (state.get("conversation_metadata") or {}).get("user_turn_metadatas") or []
    default_model = ((state.get("rts_model_state") or {}).get("model_info") or {}).get("model_id")
    created_ms = parse_iso_ms(meta.get("created_at")) or 0
    updated_ms = parse_iso_ms(meta.get("updated_at")) or created_ms

    # Rows from metadata: credits, requests, model and time are authoritative.
    rows, id2row = [], {}
    for t in turns:
        ts = parse_iso_ms(t.get("end_timestamp")) or updated_ms
        row = _new_row(t, ts)
        row["model"] = t.get("model") or default_model
        row["reqs"] = int(t.get("total_request_count") or 0)
        row["credits"] = credits_of(t)
        rows.append(row)
        for mid in t.get("message_ids") or []:
            id2row[mid] = row

    window = ((state.get("rts_model_state") or {}).get("model_info") or {}).get("context_window_tokens")

    # Token estimates from the transcript.
    jsonl = meta_path.with_suffix(".jsonl")
    ctx = pending = 0.0
    cur = None
    for rec in _read_records(jsonl):
        kind, data = rec.get("kind"), rec.get("data") or {}
        if kind == "Clear":
            ctx = pending = 0.0
            cur = None
            continue
        content_tok, thinking_tok, tools = _content_tokens(data.get("content"))
        if kind == "Prompt":
            row = id2row.get(data.get("message_id"))
            if row is None:  # prompt of a turn Kiro never recorded (interrupted/sub-agent)
                ts = ((data.get("meta") or {}).get("timestamp") or 0) * 1000 or updated_ms
                row = _new_row(None, ts)
                row["model"] = default_model
                rows.append(row)
            cur = row
            cur["has_transcript"] = True
            pending += content_tok
        elif kind == "AssistantMessage":
            if cur is None:
                cur = _new_row(None, updated_ms)
                cur["model"] = default_model
                rows.append(cur)
            out = content_tok + thinking_tok
            cur["cw"] += pending
            cur["cr"] += ctx
            cur["out"] += out
            cur["tools"].extend(tools)
            cur["n_asst"] += 1
            if cur["meta"] is None:
                cur["reqs"] += 1
            ctx += pending + content_tok
            pending = 0.0
            cur["ctx_end"] = ctx
        elif kind == "ToolResults":
            pending += content_tok
            if cur is not None:
                cur["has_transcript"] = True
                cur["ctx_end"] = ctx + pending

    _apply_baseline(rows, window)
    for r in rows:
        r["cost"] = calc_cost(int(r["cw"]), int(r["cr"]), int(r["out"]), r["model"])

    rows.sort(key=lambda r: r["ts_ms"])
    return {
        "full_id": meta["session_id"], "id": meta["session_id"][:8],
        "cwd": meta.get("cwd") or "", "title": meta.get("title") or "",
        "reason": meta.get("session_created_reason") or "",
        "created_ms": created_ms, "updated_ms": updated_ms, "rows": rows,
    }


def summarize(session, cutoff_ms=None):
    """Aggregate a parsed session (optionally only rows at/after ``cutoff_ms``).

    Returns the dict shape the viewer expects, plus ``credits``; None when no
    row survives the cutoff.
    """
    rows = [r for r in session["rows"] if not cutoff_ms or r["ts_ms"] >= cutoff_ms]
    if not rows:
        return None
    totals = {"cw": 0, "cr": 0, "out": 0, "cost": 0.0, "credits": 0.0}
    models, tools, daily, model_credits = set(), [], {}, {}
    for r in rows:
        for k in totals:
            totals[k] += r[k]
        if r["model"]:
            models.add(r["model"])
            model_credits[r["model"]] = model_credits.get(r["model"], 0.0) + r["credits"]
        tools.extend(r["tools"])
        day = datetime.fromtimestamp(r["ts_ms"] / 1000).strftime("%Y-%m-%d")
        d = daily.setdefault(day, {"cw": 0, "cr": 0, "out": 0, "cost": 0.0, "credits": 0.0, "reqs": 0})
        for k in ("cw", "cr", "out", "cost", "credits"):
            d[k] += r[k]
        d["reqs"] += r["reqs"]
    for k in ("cw", "cr", "out"):
        totals[k] = int(totals[k])
    for d in daily.values():
        for k in ("cw", "cr", "out"):
            d[k] = int(d[k])
    return {
        "id": session["id"], "full_id": session["full_id"], "cwd": session["cwd"],
        "title": session["title"], "reason": session["reason"],
        "created": datetime.fromtimestamp(session["created_ms"] / 1000),
        "updated": datetime.fromtimestamp(session["updated_ms"] / 1000),
        "turns": sum(r["reqs"] for r in rows), "prompts": len(rows), **totals,
        "models": models, "model_credits": model_credits, "tools": tools, "daily": daily,
        "source": "cli-sessions",
    }
