"""Kiro Usage Tracker — terminal viewer.

Reads archived sessions from ~/.kiro_sessions/ and live DB,
renders a TUI dashboard with token usage, costs, and session details.
"""

import json, sys, os, time, signal, platform
from datetime import datetime, timedelta
from pathlib import Path

from . import (CLI_DB, IDE_DB, SESSIONS_DIR, CLI_SESSIONS_DIR, CHARS_PER_TOKEN, calc_cost, query,
               c, fmt, fmt_cost, fmt_credits, bar, tw,
               vpad, vlpad, box_top, box_bot, box_sep, box_line)
from . import cli_sessions
from .archiver import load_archived_sessions

_cache = {}  # conversation_id -> parsed result (keyed by updated_at)

# ── Measure text size of a turn field, excluding base64 images ────────────────
def _text_len(field):
    """Return char length of the textual content in a user/assistant field."""
    if not field:
        return 0
    if not isinstance(field, dict):
        return len(str(field))
    # Exclude 'images' (base64 blobs) from the count
    return sum(len(str(v)) for k, v in field.items() if k != "images")


def _image_tokens(field):
    """Estimate vision tokens for images in a user turn. ~(w*h)/750 per image."""
    if not isinstance(field, dict):
        return 0
    images = field.get("images")
    if not images or not isinstance(images, list):
        return 0
    import struct, base64
    total = 0
    for img in images:
        src = img.get("source", {}) if isinstance(img, dict) else {}
        raw_data = src.get("Bytes", [])
        try:
            raw = bytes(raw_data) if isinstance(raw_data, list) else base64.b64decode(raw_data)
            if raw[:4] == b'\x89PNG':
                w, h = struct.unpack('>II', raw[16:24])
                total += (w * h) // 750
                continue
        except Exception:
            pass
        # Fallback: estimate from data size (~1600 tokens for a typical image)
        total += 1600
    return total


# ── Parse a single conversation snapshot into display-ready stats ─────────────
def parse_conversation(conv_id, cwd, created_at_ms, updated_at_ms, data):
    turns = data.get("history", [])
    totals = {"cw": 0, "cr": 0, "out": 0, "cost": 0.0}
    # Seed cumulative with compact summary size so post-compact cache reads
    # account for the summary context that is re-sent every turn.
    summary = data.get("latest_summary") or []
    summary_tok = len(str(summary)) // CHARS_PER_TOKEN if summary else 0
    cumulative, prev_asst = summary_tok, 0
    models, tools = set(), []
    daily = {}

    for i, turn in enumerate(turns):
        meta = turn.get("request_metadata") or {}
        user_tok = _text_len(turn.get("user")) // CHARS_PER_TOKEN + _image_tokens(turn.get("user"))
        asst_tok = _text_len(turn.get("assistant")) // CHARS_PER_TOKEN
        out_tok = len(meta.get("time_between_chunks", []))
        model = meta.get("model_id")

        cr = cumulative if i > 0 else 0
        cw = user_tok + (prev_asst if i > 0 else 0)
        tc = calc_cost(cw, cr, out_tok, model)

        totals["cw"] += cw; totals["cr"] += cr
        totals["out"] += out_tok; totals["cost"] += tc
        cumulative += user_tok + asst_tok
        prev_asst = asst_tok

        if model:
            models.add(model)
        for t in meta.get("tool_use_ids_and_names", []):
            if len(t) > 1:
                tools.append(t[1])

        ts_ms = meta.get("request_start_timestamp_ms")
        if ts_ms:
            day = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
            if day not in daily:
                daily[day] = {"cw": 0, "cr": 0, "out": 0, "cost": 0.0, "reqs": 0}
            daily[day]["cw"] += cw; daily[day]["cr"] += cr
            daily[day]["out"] += out_tok; daily[day]["cost"] += tc
            daily[day]["reqs"] += 1

    return {
        "id": conv_id[:8], "full_id": conv_id, "cwd": cwd,
        "created": datetime.fromtimestamp(created_at_ms / 1000),
        "updated": datetime.fromtimestamp(updated_at_ms / 1000),
        "turns": len(turns), **totals,
        "models": models, "tools": tools, "daily": daily,
    }

# ── Load all sessions (live DB + archive), deduplicated ───────────────────────
def load_all_sessions(days=None):
    cutoff_ms = None
    if days and days < 9000:
        cutoff_ms = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

    seen = {}

    for snap in load_archived_sessions(cutoff_ms):
        cid = snap["conversation_id"]
        updated = snap["updated_at"]
        if cid in _cache and _cache[cid]["_updated_at"] == updated:
            seen[cid] = _cache[cid]
            continue
        if cutoff_ms and updated < cutoff_ms:
            continue
        try:
            parsed = parse_conversation(
                cid, snap["cwd"], snap["created_at"], updated, snap["value"])
            parsed["_updated_at"] = updated
            _cache[cid] = parsed
            seen[cid] = parsed
        except Exception:
            continue

    for row in query(CLI_DB, "SELECT conversation_id, key as cwd, created_at, updated_at, value FROM conversations_v2 ORDER BY updated_at DESC"):
        cid = row["conversation_id"]
        updated = row["updated_at"]
        if cutoff_ms and updated < cutoff_ms:
            continue
        if cid in _cache and _cache[cid]["_updated_at"] == updated:
            seen[cid] = _cache[cid]
            continue
        try:
            data = json.loads(row["value"])
        except Exception:
            continue
        parsed = parse_conversation(cid, row["cwd"], row["created_at"], updated, data)
        parsed["_updated_at"] = updated
        _cache[cid] = parsed
        seen[cid] = parsed

    # kiro-cli 2.0.1+ uses the v1 conversations table (key=cwd, value=JSON)
    for row in query(CLI_DB, "SELECT key as cwd, value FROM conversations"):
        try:
            data = json.loads(row["value"])
        except Exception:
            continue
        cid = data.get("conversation_id")
        if not cid:
            continue
        turns = data.get("history", [])
        if not turns:
            continue
        first_ts = (turns[0].get("request_metadata") or {}).get("request_start_timestamp_ms")
        last_ts = (turns[-1].get("request_metadata") or {}).get("request_start_timestamp_ms")
        created_at = first_ts or 0
        updated_at = last_ts or first_ts or 0
        if cutoff_ms and updated_at < cutoff_ms:
            continue
        if cid in _cache and _cache[cid]["_updated_at"] == updated_at:
            seen[cid] = _cache[cid]
            continue
        parsed = parse_conversation(cid, row["cwd"], created_at, updated_at, data)
        parsed["_updated_at"] = updated_at
        _cache[cid] = parsed
        seen[cid] = parsed

    # kiro-cli sessions store (~/.kiro/sessions/cli): real credits + estimated tokens
    for sess in cli_sessions.load_sessions(cutoff_ms):
        summ = cli_sessions.summarize(sess, cutoff_ms)
        if summ:
            seen[summ["full_id"]] = summ

    for cv in seen.values():  # legacy sources have no credits
        cv.setdefault("credits", 0.0)
        for d in cv["daily"].values():
            d.setdefault("credits", 0.0)

    return sorted(seen.values(), key=lambda x: x["updated"], reverse=True)

# ── Load IDE usage (Kiro IDE token data) ──────────────────────────────────────
# Before ~Feb 28 2026, tokens_prompt = full context (total API input per call).
# After that, tokens_prompt = incremental only (new content per call).
_IDE_CUTOVER = "2026-02-28"

def load_ide_usage(days=None):
    """Load Kiro IDE token data with cache-aware cost estimation."""
    if not IDE_DB.exists():
        return None
    cutoff = ""
    params = ()
    if days and days < 9000:
        cutoff = " WHERE timestamp >= datetime('now', ?)"
        params = ("-{} days".format(days),)
    rows = query(IDE_DB,
        "SELECT tokens_prompt, tokens_generated, timestamp"
        " FROM tokens_generated" + cutoff + " ORDER BY id", params)
    if not rows:
        return None

    daily = {}
    totals = {"input": 0, "out": 0, "cost": 0.0, "calls": 0}
    # For new-format data: accumulate CacheRead within sessions
    cum = 0
    prev = 0

    for r in rows:
        p = r["tokens_prompt"] or 0
        out = r["tokens_generated"] or 0
        day = r["timestamp"][:10]
        is_old = day < _IDE_CUTOVER

        if is_old:
            # tokens_prompt = full context; ~10% write, ~90% read
            cw = int(p * 0.10)
            cr = int(p * 0.90)
            total_input = p
        else:
            # tokens_prompt = incremental (CacheWrite); derive CacheRead
            if p < prev * 0.5:
                cum = 0  # session reset
            cw = p
            cr = cum
            cum += p
            total_input = cw + cr
            prev = p

        tc = calc_cost(cw, cr, out)
        totals["input"] += total_input
        totals["out"] += out; totals["cost"] += tc; totals["calls"] += 1

        if day not in daily:
            daily[day] = {"input": 0, "out": 0, "cost": 0.0, "calls": 0}
        daily[day]["input"] += total_input
        daily[day]["out"] += out; daily[day]["cost"] += tc; daily[day]["calls"] += 1

    return {**totals, "daily": daily}

# ── Render ────────────────────────────────────────────────────────────────────
def _fit(label, parts, width):
    """Join ``(styled, visible_len)`` parts after ``label`` and drop what overflows the box."""
    budget = width - 4 - len(label)
    out, used = [], 0
    for text, n in parts:
        if used + n + (2 if out else 0) > budget:
            break
        out.append(text)
        used += n + (2 if len(out) > 1 else 0)
    return label + "  ".join(out)

def render(days, max_sessions=5):
    cli_convos = load_all_sessions(days)
    w = min(tw(), 120)
    L = []
    now = datetime.now().strftime("%H:%M:%S")
    label = "all time" if days > 9000 else "last {} day{}".format(days, "s" if days != 1 else "")

    L.append("")
    L.append("  " + c("Kiro Usage Tracker", "bold", "cyan") +
             "   " + c("{}  {}".format(label, now), "dim"))
    L.append("")

    # Compute CLI cost-per-prompt-token ratio for IDE estimation
    cli_total_cost = sum(cv["cost"] for cv in cli_convos) if cli_convos else 0
    cli_total_prompt = sum(cv["cw"] + cv["cr"] for cv in cli_convos) if cli_convos else 0

    L.append(box_top("Kiro-CLI", w))
    if cli_convos:
        t_cw   = sum(cv["cw"]   for cv in cli_convos)
        t_cr   = sum(cv["cr"]   for cv in cli_convos)
        t_out  = sum(cv["out"]  for cv in cli_convos)
        t_cost = sum(cv["cost"] for cv in cli_convos)
        t_reqs = sum(cv["turns"] for cv in cli_convos)
        t_cred = sum(cv["credits"] for cv in cli_convos)
        show_cred = t_cred > 0
        all_models = set()
        for cv in cli_convos:
            all_models.update(cv["models"])

        L.append(box_line(
            "{} reqs  CWrite {}  CRead {}  Output {}  Cost {}".format(
                c(t_reqs, "bold", "white"), c(fmt(t_cw), "green"),
                c(fmt(t_cr), "yellow"), c(fmt(t_out), "blue"),
                c(fmt_cost(t_cost), "red", "bold")), w))
        if show_cred:
            L.append(box_line(
                "Credits {}  {}".format(
                    c(fmt_credits(t_cred), "bold", "magenta"),
                    c("(measured by Kiro; tokens & $ are estimates)", "dim")), w))
        if all_models:
            # Count turns per model
            model_counts = {}
            for cv in cli_convos:
                for m in cv["models"]:
                    model_counts[m] = model_counts.get(m, 0) + cv["turns"]
            parts = [("{}{}{}".format(c(m, "magenta"), c(" x", "dim"), c(n, "bold")),
                      len(m) + 2 + len(str(n)))
                     for m, n in sorted(model_counts.items(), key=lambda x: -x[1])]
            L.append(box_line(_fit("Models: ", parts, w), w))
            if show_cred:
                mc = {}
                for cv in cli_convos:
                    for m, v in cv.get("model_credits", {}).items():
                        mc[m] = mc.get(m, 0.0) + v
                parts = [("{} {}".format(c(m, "magenta"), c(fmt_credits(v), "bold")),
                          len(m) + 1 + len(fmt_credits(v)))
                         for m, v in sorted(mc.items(), key=lambda x: -x[1]) if v > 0]
                if parts:
                    L.append(box_line(_fit("Credits by model: ", parts, w), w))

        # Daily breakdown
        cli_daily = {}
        for cv in cli_convos:
            for d, v in cv["daily"].items():
                if d not in cli_daily:
                    cli_daily[d] = {"cw": 0, "cr": 0, "out": 0, "cost": 0.0, "credits": 0.0, "reqs": 0}
                for k in ("cw", "cr", "out", "cost", "credits", "reqs"):
                    cli_daily[d][k] += v[k]

        if cli_daily:
            L.append(box_sep(w))
            max_r = max(v["reqs"] for v in cli_daily.values())
            hdr = "{} {:>5} {:>8} {:>8} {:>7} {:>8}{}  {}".format(
                vpad("Date", 12), "Reqs", "CWrite", "CRead", "Output", "Cost",
                " {:>7}".format("Credits") if show_cred else "", "Activity")
            L.append(box_line(c(hdr, "dim"), w))

            for d in sorted(cli_daily.keys(), reverse=True):
                v = cli_daily[d]
                pct = v["reqs"] / max_r * 100 if max_r else 0
                is_today = d == datetime.now().strftime("%Y-%m-%d")
                day_c  = c(d, "bold", "white") if is_today else c(d, "dim")
                marker = c("> ", "cyan") if is_today else "  "
                reqs_c = c(str(v["reqs"]), "bold") if is_today else str(v["reqs"])
                cost_c = c(fmt_cost(v["cost"]), "red") if v["cost"] >= 0.1 else fmt_cost(v["cost"])
                cred_c = " " + vlpad(c(fmt_credits(v["credits"]), "magenta"), 7) if show_cred else ""
                line = "{}{} {} {} {} {} {}{}  {}".format(
                    marker, vpad(day_c, 10), vlpad(reqs_c, 5),
                    vlpad(fmt(v["cw"]), 8), vlpad(fmt(v["cr"]), 8),
                    vlpad(fmt(v["out"]), 7), vlpad(cost_c, 8), cred_c,
                    bar(pct, max(8, min(20, w - 3 - (63 if show_cred else 55)))))
                L.append(box_line(line, w))

        # Tool usage
        all_tools = {}
        for cv in cli_convos:
            for t in cv["tools"]:
                all_tools[t] = all_tools.get(t, 0) + 1
        if all_tools:
            L.append(box_sep(w))
            top = sorted(all_tools.items(), key=lambda x: -x[1])[:6]
            inner = w - 4
            parts, cur = [], 7
            for n, cnt in top:
                part = "{}{}{}".format(c(n, "cyan"), c("x", "dim"), c(cnt, "bold"))
                plen = len(n) + 1 + len(str(cnt))
                if cur + plen + 2 > inner:
                    break
                parts.append(part)
                cur += plen + 2
            L.append(box_line("Tools: " + "  ".join(parts), w))

        # Sessions (recent only)
        L.append(box_sep(w))
        dir_w = max(10, min(18, w - 3 - (67 if show_cred else 59)))
        shdr = "{} {} {:>5} {:>7} {:>7} {:>6} {:>6}{} {}".format(
            vpad("ID", 8), vpad("Directory", dir_w),
            "Turns", "CWrite", "CRead", "Out", "Cost",
            " {:>7}".format("Credits") if show_cred else "", "Updated")
        L.append(box_line(c(shdr, "dim"), w))

        for cv in cli_convos[:max_sessions]:
            cwd = cv["cwd"].replace(str(Path.home()), "~")
            if len(cwd) > dir_w - 1:
                cwd = ".." + cwd[-(dir_w - 3):]
            age = datetime.now() - cv["updated"]
            if age < timedelta(hours=1):
                dot, ts = c("*", "green"), cv["updated"].strftime("%H:%M")
            elif age < timedelta(hours=6):
                dot, ts = c("*", "yellow"), cv["updated"].strftime("%H:%M")
            else:
                dot, ts = c(".", "dim"), cv["updated"].strftime("%m-%d %H:%M")
            updated = vpad("{} {}".format(dot, ts), 13)
            cred_c = " " + vlpad(c(fmt_credits(cv["credits"]), "magenta"), 7) if show_cred else ""
            line = "{} {} {:>5} {:>7} {:>7} {:>6} {:>6}{} {}".format(
                vpad(c(cv["id"], "cyan"), 8), vpad(cwd, dir_w), cv["turns"],
                fmt(cv["cw"]), fmt(cv["cr"]), fmt(cv["out"]),
                fmt_cost(cv["cost"]), cred_c, updated)
            L.append(box_line(line, w))
        if len(cli_convos) > max_sessions:
            L.append(box_line(c("  … and {} more (use --all to show)".format(
                len(cli_convos) - max_sessions), "dim"), w))
    else:
        L.append(box_line(c("No CLI data found.", "dim"), w))

    L.append(box_bot(w))

    # ── IDE section (disabled — IDE only tracks partial prompt data) ─────
    # ide = load_ide_usage(days)

    # ── Total ─────────────────────────────────────────────────────────────
    total_credits = sum(cv["credits"] for cv in cli_convos) if cli_convos else 0
    if cli_total_cost > 0 or total_credits > 0:
        L.append("")
        line = "  " + c("Estimated cost: ", "dim") + c(fmt_cost(cli_total_cost), "bold", "red")
        if total_credits > 0:
            line += c("   Credits used: ", "dim") + c(fmt_credits(total_credits), "bold", "magenta")
        L.append(line)

    from .service import _has_systemd, _launchd_plist_path
    has_archiver = (platform.system() == "Darwin" and _launchd_plist_path().exists()) or \
                   (platform.system() != "Darwin" and _has_systemd())
    if has_archiver:
        L.append("  " + c(
            "📁 Sessions archived to ~/.kiro_sessions/ — history persists across /clear and restarts",
            "dim", "green"))
    L.append("")
    return "\n".join(L)

# ── Session detail view ───────────────────────────────────────────────────────
# ── Session detail view ───────────────────────────────────────────────────────
def _render_cli_session(sess):
    """Per-turn breakdown for a session from the ~/.kiro/sessions/cli store."""
    w = min(tw(), 120)
    rows = sess["rows"]
    L = [""]
    L.append("  " + c("Session ", "bold", "cyan") + c(sess["id"], "cyan") +
             "   " + c(sess["cwd"].replace(str(Path.home()), "~"), "dim"))
    if sess["title"]:
        L.append("  " + c(sess["title"][:w - 4], "dim"))
    L.append("")
    hdr = " {:>4}  {:>5}  {:>8}  {:>8}  {:>7}  {:>8}  {:>7}  {:<18}  {}".format(
        "#", "Reqs", "CWrite", "CRead", "Out", "Credits", "Cost", "Model", "Time")
    L.append(c(hdr, "dim"))
    for i, r in enumerate(rows):
        ts = datetime.fromtimestamp(r["ts_ms"] / 1000).strftime("%m-%d %H:%M:%S")
        tools = [t for t in r["tools"][:3]]
        tool_str = "  " + c(",".join(tools), "dim") if tools else ""
        L.append(" {:>4}  {:>5}  {:>8}  {:>8}  {:>7}  {:>8}  {:>7}  {:<18}  {}{}".format(
            i, r["reqs"], fmt(int(r["cw"])), fmt(int(r["cr"])), fmt(int(r["out"])),
            fmt_credits(r["credits"]) if r["meta"] else "-", fmt_cost(r["cost"]),
            (r["model"] or "")[:18], ts, tool_str))
    L.append("")
    L.append("  " + c("Total: ", "dim") +
             c("{} credits".format(fmt_credits(sum(r["credits"] for r in rows))), "bold", "magenta") +
             "  " + c(fmt_cost(sum(r["cost"] for r in rows)), "bold", "red") +
             c("  ({} turns, {} requests; tokens & $ estimated)".format(
                 len(rows), sum(r["reqs"] for r in rows)), "dim"))
    L.append("")
    return "\n".join(L)


def render_session(prefix):
    """Show per-turn breakdown for a session matching the given ID prefix."""
    for sess in cli_sessions.load_sessions():
        if sess["full_id"].startswith(prefix):
            return _render_cli_session(sess)

    # Find matching conversation from all sources
    data, cid, cwd = None, None, None
    for snap in load_archived_sessions():
        if snap["conversation_id"].startswith(prefix):
            data, cid, cwd = snap["value"], snap["conversation_id"], snap["cwd"]
    for row in query(CLI_DB, "SELECT conversation_id, key as cwd, value FROM conversations_v2"):
        if row["conversation_id"].startswith(prefix):
            data, cid, cwd = json.loads(row["value"]), row["conversation_id"], row["cwd"]
    for row in query(CLI_DB, "SELECT key as cwd, value FROM conversations"):
        try:
            d = json.loads(row["value"])
        except Exception:
            continue
        if d.get("conversation_id", "").startswith(prefix):
            data, cid, cwd = d, d["conversation_id"], row["cwd"]

    if not data:
        return "No session found matching '{}'".format(prefix)

    turns = data.get("history", [])
    summary = data.get("latest_summary") or []
    summary_tok = len(str(summary)) // CHARS_PER_TOKEN if summary else 0
    cumulative, prev_asst = summary_tok, 0
    w = min(tw(), 120)
    L = [""]
    L.append("  " + c("Session ", "bold", "cyan") + c(cid[:8], "cyan") +
             "   " + c(cwd.replace(str(Path.home()), "~"), "dim"))
    if summary_tok:
        L.append("  " + c("Compact summary: ~{} tokens carried forward".format(
            fmt(summary_tok)), "dim"))
    L.append("")

    hdr = " {:>4}  {:>6}  {:>8}  {:>8}  {:>6}  {:>7}  {}".format(
        "#", "Out", "CWrite", "CRead", "Cost", "Model", "Time")
    L.append(c(hdr, "dim"))

    total_cost = 0.0
    for i, turn in enumerate(turns):
        meta = turn.get("request_metadata") or {}
        user_tok = _text_len(turn.get("user")) // CHARS_PER_TOKEN + _image_tokens(turn.get("user"))
        asst_tok = _text_len(turn.get("assistant")) // CHARS_PER_TOKEN
        out_tok = len(meta.get("time_between_chunks", []))
        model = meta.get("model_id") or ""

        cr = cumulative if i > 0 else 0
        cw = user_tok + (prev_asst if i > 0 else 0)
        tc = calc_cost(cw, cr, out_tok, model)
        total_cost += tc
        cumulative += user_tok + asst_tok
        prev_asst = asst_tok

        ts_ms = meta.get("request_start_timestamp_ms")
        ts = datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S") if ts_ms else ""
        model_short = model.replace("claude-", "").replace("claude_", "")[:12]

        tools_used = [t[1] for t in meta.get("tool_use_ids_and_names", []) if len(t) > 1]
        tool_str = "  " + c(",".join(tools_used[:3]), "dim") if tools_used else ""

        line = " {:>4}  {:>6}  {:>8}  {:>8}  {:>6}  {:>7}  {}{}".format(
            i, out_tok, fmt(cw), fmt(cr), fmt_cost(tc), model_short, ts, tool_str)
        L.append(line)

    L.append("")
    L.append("  " + c("Total: ", "dim") + c(fmt_cost(total_cost), "bold", "red") +
             c("  ({} turns)".format(len(turns)), "dim"))
    L.append("")
    return "\n".join(L)


# ── Modes ─────────────────────────────────────────────────────────────────────
def _clear():
    sys.stdout.write("\033[2J\033[H"); sys.stdout.flush()

def live(days, interval=5, max_sessions=5):
    signal.signal(signal.SIGINT, lambda *_: (sys.stdout.write("\033[?25h\n"), sys.exit(0)))
    sys.stdout.write("\033[?25l")
    try:
        while True:
            _clear()
            print(render(days, max_sessions))
            print(c("  ⏸  Ctrl+C to exit  │  🔄 refreshing every {}s".format(interval), "dim"))
            time.sleep(interval)
    finally:
        sys.stdout.write("\033[?25h")

def view_json(days):
    cli_convos = load_all_sessions(days)
    cli_total_cost = sum(cv["cost"] for cv in cli_convos) if cli_convos else 0
    cli_total_prompt = sum(cv["cw"] + cv["cr"] for cv in cli_convos) if cli_convos else 0
    cpp = cli_total_cost / cli_total_prompt if cli_total_prompt > 0 else None

    out = {"cli": {"daily": {}, "sessions": []}}
    for cv in cli_convos:
        for d, v in cv["daily"].items():
            if d not in out["cli"]["daily"]:
                out["cli"]["daily"][d] = {
                    "requests": 0, "cache_write_est": 0,
                    "cache_read_est": 0, "output_tokens": 0, "cost_est_usd": 0.0,
                    "credits": 0.0,
                }
            for k, jk in [("reqs", "requests"), ("cw", "cache_write_est"),
                           ("cr", "cache_read_est"), ("out", "output_tokens"),
                           ("cost", "cost_est_usd"), ("credits", "credits")]:
                out["cli"]["daily"][d][jk] += v[k]
        out["cli"]["sessions"].append({
            "id": cv["id"], "cwd": cv["cwd"], "turns": cv["turns"],
            "cache_write_est": cv["cw"], "cache_read_est": cv["cr"],
            "output_tokens": cv["out"], "cost_est_usd": round(cv["cost"], 4),
            "credits": round(cv["credits"], 4),
            "models": list(cv["models"]),
            "created": cv["created"].isoformat(),
            "updated": cv["updated"].isoformat(),
        })
    for d in out["cli"]["daily"]:
        out["cli"]["daily"][d]["cost_est_usd"] = round(
            out["cli"]["daily"][d]["cost_est_usd"], 4)
        out["cli"]["daily"][d]["credits"] = round(
            out["cli"]["daily"][d]["credits"], 4)

    # IDE disabled — data too incomplete for reliable estimates
    # ide = load_ide_usage(days)
    out["combined_cost_est_usd"] = round(cli_total_cost, 4)
    out["total_credits"] = round(sum(cv["credits"] for cv in cli_convos), 4)
    print(json.dumps(out, indent=2))

# ── CLI entry point ───────────────────────────────────────────────────────────
PERIODS = {"today": 1, "week": 7, "month": 30, "all": 9999}

def main():
    from . import service

    args = sys.argv[1:]
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    no_live = "--no-live" in args
    if no_live:
        args.remove("--no-live")
    show_all = "--all" in args
    if show_all:
        args.remove("--all")
    cmd = args[0] if args else "week"

    # Service management subcommands
    if cmd == "install":
        service.install()
        return
    if cmd == "uninstall":
        service.uninstall()
        return
    if cmd == "status":
        service.status()
        return
    if cmd == "session":
        sid = args[1] if len(args) > 1 else ""
        if not sid:
            print("Usage: kiro-usage session <id-prefix>")
            sys.exit(1)
        print(render_session(sid))
        return

    if cmd in ("help", "-h", "--help"):
        print("""
  {} {}

  {} kiro-usage [command] [flags]

  {}
    today     Last 24 hours (default, live refresh)
    week      Last 7 days
    month     Last 30 days
    all       All time
    session   Per-turn detail for a session (kiro-usage session <id>)
    install   Register background archiver as a system service
    uninstall Remove background archiver service
    status    Show archiver service status

  {}
    --json      JSON output (no live mode)
    --no-live   Print once and exit

  {} (CLI only)
    💳 Credits     = measured by Kiro (per-turn metering) — the authoritative number
    ✏️  CacheWrite  = new tokens per turn (estimated, chars/4)
    📖 CacheRead   = prior context resent to API (estimated)
    📤 Output      = tokens generated incl. thinking (estimated; legacy DB: from chunks)
    💰 Cost        = cache-aware pricing estimate

  {} (5-min cache write rate)
    Model              CWrite    CRead    Output
    claude-opus-4.6    $6.25     $0.50    $25/MTok
    claude-opus-4.5    $6.25     $0.50    $25/MTok
    claude-opus-4.1    $18.75    $1.50    $75/MTok
    claude-sonnet-4.x  $3.75     $0.30    $15/MTok
    newer opus/sonnet  priced like the latest known model of the family

  {} 📂
    CLI sessions: {}
    CLI DB:       {}
    Archive:      {}
""".format(
            c("⚡", "yellow"), c("Kiro Usage Tracker", "bold", "cyan"),
            c("Usage:", "bold"), c("📋 Commands:", "bold"),
            c("🚩 Flags:", "bold"), c("📊 Metrics:", "bold"),
            c("💲 Pricing $/MTok:", "bold"),
            c("Data sources:", "dim"), CLI_SESSIONS_DIR, CLI_DB, SESSIONS_DIR))
        return

    period = PERIODS.get(cmd)
    if period is None:
        print("Unknown command: " + cmd)
        sys.exit(1)

    ms = 9999 if show_all else 5

    if as_json:
        view_json(period)
    elif no_live or not sys.stdout.isatty():
        print(render(period, ms))
    else:
        live(period, max_sessions=ms)

if __name__ == "__main__":
    main()
