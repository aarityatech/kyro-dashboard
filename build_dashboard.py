#!/usr/bin/env python3
"""
build_dashboard.py — ETL for the Kyro thread analytics dashboard.

Reads:
  - threads/*/*.json           (one file per agent "turn")
  - trader_persona_june.csv    (user_id -> primary trading persona)

Extracts one compact record per turn (plus per-user identity/persona) and writes
them to `dashboard_data.js` as:
    window.DASHBOARD_DATA = { meta, pricing, client_tools, ident, turns };

All aggregation happens client-side in assets/dashboard_calc.js (buildData) —
that is what lets the dashboard's global date-range and persona filters
recompute every metric in the browser from the raw turns.

The dashboard (index.html) loads that file via a <script> tag, so the whole
thing works offline straight from the filesystem — no server required.

Stdlib only. Run:  python3 build_dashboard.py
"""

import csv
import json
import glob
import re
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))  # users are Indian -> bucket by IST

import sys
sys.path.insert(0, ROOT)
import eval_checks  # automated quality-eval rules (Tier A deterministic + Tier B heuristic)

# Client/UI tools (rendered on the device) vs server data tools.
CLIENT_TOOLS = {
    "show_widget", "ask_user", "open_page", "show_order_form", "show_feature_intro",
    "get_screen_context", "chart_configure", "chart_add_indicators",
    "chart_remove_indicators", "chart_modify_indicator", "chart_apply_template",
    "chart_save_template", "chart_add_drawing",
}

# Cost model — Gemini 3.1 Flash-Lite API pricing, USD per 1M tokens (verified Jun 2026).
# Cached reads are 90% cheaper than fresh input; thinking/reasoning is billed at the
# output rate. Edit these to re-price for a different model.
PRICING = {
    "model": "Gemini 3.1 Flash-Lite",
    "input_per_m": 0.25,          # fresh (cache-miss) input
    "cached_input_per_m": 0.025,  # cache reads
    "output_per_m": 1.50,         # output (thinking billed as output)
}

# ----------------------------------------------------------------------------- helpers

def ist_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=IST)

# ----------------------------------------------------------------------------- user whitelist

# Internal users are EXCLUDED from analysis. A user is internal if their profile
# email is on a company domain, OR their user_id is in INTERNAL_IDS.
# (user-info.sql is no longer read.)
INTERNAL_DOMAINS = {"aaritya.com", "sahi.com"}

# Numbers that recur in the system prompt / fixed copy, not the market — excluded
# from the NUMBER_UNGROUNDED_NO_FETCH thread check so citing them isn't flagged.
# 22172 = Sahi Research reg no. INH000022172; 20 = the ₹20-flat competitor strawman.
_GROUNDING_CONSTANTS = {22172, 20, 500000, 25000}
INTERNAL_IDS = {
    "cs8b74j7545c1as3v8u0", "csu57kj04esd6q3lm2hg", "csvvnpr7545fsgng4u1g", "ct3dnan2crq9009t4gb0", 
    "ct3ehij7545f892i04k0", "ct41ar72crq06uof7rd0", "ct4o53v2crqe9kuuoaf0", "ct4ovqf2crqcf7r355r0", 
    "ct6kimr75459r2gcun30", "ct77ih375451o5hc5eig", "ct782tb75451o5hc5tcg", "ct787fr75451o5hc61ng", 
    "ct7c1i375453seuerl10", "ct7cghj75453seues3q0", "ct80sn304esbnc9uqhhg", "ct8ktu156bs5pla6uuog", 
    "ct8lbkr75457mka1340g", "ctasqbv2crq9ktji70mg", "ctg22ij04escocr3rtc0", "ctvtsm09524s73fjoalg", 
    "cu3m9dd5m0ps73fmo1l0", "cu7qbv0o80ps73ff568g", "cuhgd1j0mv7c73a5tgdg", "cv5vejgo80ps738dc9s0", 
    "d00rj946dd6s738f15n0", "d0nerrs6dd6s73e2krv0", "d0q02js6dd6s73e8hkgg", "d0u2qas6dd6s73b8ad70", 
    "d1b5uo46dd6s73e5q4qg", "d1uvtq9uteqs73ejj240", "d2hdjcujnkhs73fguvrg", "d3n2cmhrdllc73drb07g", 
    "d40vu36jnkhs73bvfd8g", "d4gasg6jnkhs739nkf40", "d4o3j2hrk43c73fava60", "d4tu4rg2am8s73dt7m20", 
    "d4vrai3rncrc739sdv9g", "d53nmjbrncrc739v87mg", "d53usv3rncrc739vefs0", "d5g6c3a4suvs73f6loag", 
    "d5o9gpa7gegc739vekbg", "d6u2jrc9ndac73cjuvvg", "d7g84i12c4qc73800icg", "d7h4gqmkkqmc73f0ooe0", 
    "d7j2kn1hbt2s738tu84g", "d7o94f9hbt2s7392gttg", "d7s7sf5v4ocs73eco9ag", "d7s7smh6e1pc73c58e70", 
    "d669ise1stss73c8c7ig", "cs3tjhkcutqkeh0jq9sg", "crfeanr7545bdpm9d6i0", "d4fcv62cn9jc73dbjthg",
    "d1db4nk6dd6s738rpm20", "ct6vu7375451o5hbusi0", "d8i0b4fjji4c73fll3lg", "d6avk3s1k2bc73e4pntg",
    'd1e27lputeqs739dcq70', 'd8f8rf3s9r4c739g4ls0', 'd8l3fdsseh2s73eku0bg'
}

def parse_profile(d):
    """Pull (email, full_name) from a turn's system_prompt.vars.profile."""
    sp = d.get("system_prompt") or {}
    prof = sp.get("vars", {}).get("profile") if isinstance(sp, dict) else None
    if isinstance(prof, str):
        try:
            prof = json.loads(prof)
        except Exception:
            prof = None
    if isinstance(prof, dict):
        return (prof.get("email") or "", prof.get("full_name") or "")
    return ("", "")

# ----------------------------------------------------------------------------- query analysis
# (query word-cloud tokenization moved to assets/dashboard_calc.js with the rest
# of the aggregation; only per-turn enrichment — intent, sentiment — stays here)

INTENT_RULES = [
    ("Screen analysis",      [r"\banaly[sz]e?\s+(this|the|my)?\s*screen", r"\bthis screen\b"]),
    ("Options & chain",      [r"option chain", r"\boptions?\b", r"\bcall\b", r"\bput\b", r"straddle",
                              r"strangle", r"iron condor", r"\boi\b", r"open interest", r"payoff",
                              r"strategy", r"strike", r"expiry"]),
    ("Charts & technicals",  [r"\bchart\b", r"indicator", r"candlestick", r"\bchoch\b", r"\bbos\b",
                              r"support", r"resistance", r"\brsi\b", r"\bmacd\b", r"\bema\b", r"\bsma\b",
                              r"pattern", r"trend ?line", r"draw"]),
    ("Fundamentals & research", [r"fundamental", r"\bpe\b", r"\bp/e\b", r"valuation", r"under ?valued",
                              r"over ?valued", r"dividend", r"\bresults?\b", r"earnings", r"balance sheet",
                              r"compare", r"\beps\b", r"\broe\b", r"market cap", r"company\b"]),
    ("Scanner & discovery",  [r"list of stocks", r"stocks (whose|that|which|with|in)", r"\bscan\b",
                              r"\bscreen for\b", r"find stocks", r"\bsector\b", r"top gainers",
                              r"top losers", r"shortlist", r"give me stocks"]),
    ("Positions & portfolio",[r"\bposition", r"\bholding", r"portfolio", r"\bmy stocks\b", r"my trades"]),
    ("Orders & trades",      [r"\border", r"\btrade\b", r"\bbuy\b", r"\bsell\b", r"execute", r"\bsquare ?off"]),
    ("P&L, funds & charges", [r"\bpnl\b", r"p&l", r"profit", r"\bloss\b", r"charges", r"\bfunds?\b",
                              r"balance", r"margin", r"brokerage", r"realized", r"payout"]),
    ("Market & news",        [r"\bmarket\b", r"\bnews\b", r"\bsession\b", r"\brecap\b", r"global",
                              r"economic", r"\bevents?\b", r"\bnifty\b", r"banknifty", r"sensex",
                              r"commentary", r"\bipo\b"]),
    ("Watchlist",            [r"watchlist", r"watch list"]),
    ("Greeting & chit-chat", [r"^\s*(hi|hello|hey|hii+|namaste|good morning|good evening)\b",
                              r"how are you", r"who are you", r"what can you do", r"\bthanks?\b"]),
]

# Frustration / negative-sentiment lexicon (English + common Hinglish).
NEGATIVE_PATTERNS = [
    (r"\bwrong\b", "says 'wrong'"),
    (r"\bincorrect\b", "says 'incorrect'"),
    (r"not work", "'not working'"),
    (r"doesn'?t work", "'doesn't work'"),
    (r"\buseless\b", "says 'useless'"),
    (r"\bbad\b", "says 'bad'"),
    (r"\bnonsense\b", "says 'nonsense'"),
    (r"\bstupid\b", "says 'stupid'"),
    (r"not what i", "'not what I…'"),
    (r"\bnot helpful\b", "'not helpful'"),
    (r"\bgalat\b", "'galat' (wrong)"),
    (r"\bbekar\b", "'bekar' (useless)"),
    (r"\bkharab\b", "'kharab' (bad)"),
    (r"\bnahi\b.*\b(chahiye|kaam)", "Hindi negation"),
    (r"\bwhy (don'?t|can'?t|isn'?t|doesn'?t)\b", "complaint phrasing"),
    (r"\bagain\b.*\b(wrong|error|same)\b", "repeated failure"),
    (r"\bstill\b.*\b(not|wrong|same|error)\b", "'still not…'"),
    (r"\bfix (this|it)\b", "asks to fix"),
    (r"\byou (are|r) wrong\b", "'you are wrong'"),
    (r"\bthat'?s wrong\b", "'that's wrong'"),
    (r"\bnot correct\b", "'not correct'"),
    (r"\bdisappoint", "disappointment"),
    (r"\bworst\b", "says 'worst'"),
    (r"\bhopeless\b", "says 'hopeless'"),
]

def classify_intent(text, tools_used):
    t = (text or "").lower()
    for name, pats in INTENT_RULES:
        for p in pats:
            if re.search(p, t):
                return name
    # fall back to tool signals
    ts = set(tools_used)
    if {"get_live_option_chain", "get_payoff", "explore_strategies", "compute_margin"} & ts:
        return "Options & chain"
    if {"scan_instruments", "search_instruments"} & ts:
        return "Scanner & discovery"
    if {"get_comprehensive_fundamental_analysis", "get_index_constituent_analysis"} & ts:
        return "Fundamentals & research"
    if {"get_positions", "get_holdings"} & ts:
        return "Positions & portfolio"
    if {"get_company_news", "get_market_news", "get_market_commentary"} & ts:
        return "Market & news"
    if any(x.startswith("chart_") for x in ts):
        return "Charts & technicals"
    return "Other"

def negative_reason(text):
    t = (text or "").lower()
    for pat, reason in NEGATIVE_PATTERNS:
        if re.search(pat, t):
            return reason
    return None

def parse_screen(content):
    """Pull the page/screen descriptor out of a get_screen_context tool result."""
    txt = None
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                txt = b.get("text"); break
    elif isinstance(content, str):
        txt = content
    if not txt:
        return None
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    sc = obj.get("screen") if isinstance(obj, dict) else None
    if not isinstance(sc, dict) or not sc.get("id"):
        return None
    return {
        "id": sc.get("id"),
        "sub_tab": sc.get("sub_tab") or sc.get("selected_tab") or sc.get("current_portfolio_tab"),
        "desc": (sc.get("description") or "").strip()[:90],
    }

def clip(s, n):
    """Truncate a string for the transcript preview (keeps the data file small)."""
    s = s or ""
    return (s[:n] + " …[truncated]") if len(s) > n else s

def content_text(content):
    """Flatten tool_result content (str / list of blocks) into a text preview."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text", "") if b.get("type") == "text"
                             else json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return " ".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)

# ----------------------------------------------------------------------------- per-turn extraction

def extract_turn(d):
    """Return a structured record for one turn dict, or None on malformed."""
    uid = d.get("user_id")
    started = d.get("started_at_ms") or 0
    finished = d.get("finished_at_ms") or 0
    latency = d.get("latency_ms")
    if latency is None and started and finished:
        latency = finished - started
    usage = d.get("usage") or {}

    rec = {
        "thread_id": d.get("thread_id"),
        "turn_id": d.get("turn_id"),
        "user_id": uid,
        "started_at_ms": started,
        "finished_at_ms": finished,
        "latency_ms": latency or 0,
        "outcome": d.get("outcome"),
        "stop_reason": d.get("stop_reason"),
        "agent": (d.get("agent") or {}).get("id"),
        "tokens": {
            "input": usage.get("input_tokens", 0) or 0,
            "output": usage.get("output_tokens", 0) or 0,
            "cache_read": usage.get("cache_read_tokens", 0) or 0,
            "cache_write": usage.get("cache_write_tokens", 0) or 0,
            "reasoning": usage.get("reasoning_tokens", 0) or 0,
        },
        "follow_ups": [
            {"type": f.get("type"), "label": f.get("label")}
            for f in (d.get("follow_ups") or [])
        ],
        "skills_loaded": (d.get("agent") or {}).get("skills_loaded", []) or [],
    }

    # ---- walk events: query, reasoning, tools (matched by id), client interactions
    tools = {}        # id -> {name, input_size, output_size, latency_ms, is_error, dispatched}
    interactions = [] # client_tool_dispatch enriched records
    screens = []      # screen/page descriptors from get_screen_context results
    transcript = []   # ordered reasoning / tool / response items for the expandable view
    tool_item_by_id = {}
    reasoning_steps = 0
    cancelled = False
    user_query = None
    all_user_text = []
    resp_texts = []   # assistant text blocks (for the quality checks)
    tool_order = []   # tool_use ids in call order (for the quality checks)
    loaded_skills = [] # skill names from load_skill calls (thread-level reload check)

    for e in d.get("events", []):
        kind = e.get("kind")
        if kind == "commit":
            msg = e.get("message") or {}
            role = msg.get("role")
            for c in msg.get("content", []):
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "text" and role == "user":
                    txt = c.get("text", "")
                    if txt.strip():
                        all_user_text.append(txt.strip())
                        if user_query is None:
                            user_query = txt.strip()
                elif ctype == "text" and role == "assistant":
                    txt = c.get("text", "")
                    if txt.strip():
                        resp_texts.append(txt.strip())
                        transcript.append({"k": "resp", "t": txt})  # full, no truncation
                elif ctype == "tool_use":
                    tid = c.get("id")
                    inp = c.get("input", {})
                    rt = tools.setdefault(tid, {})
                    rt["name"] = c.get("name")
                    rt["input_size"] = len(json.dumps(inp, ensure_ascii=False)) if inp is not None else 0
                    rt["input_full"] = inp
                    tool_order.append(tid)
                    if c.get("name") == "load_skill" and isinstance(inp, dict) and inp.get("name"):
                        loaded_skills.append(inp.get("name"))
                    if c.get("name") == "show_widget" and isinstance(inp, dict):
                        rt["widget_type"] = inp.get("type")
                        params = inp.get("params") or {}
                        rt["widget_title"] = params.get("title") if isinstance(params, dict) else None
                    if c.get("name") == "open_page" and isinstance(inp, dict):
                        rt["page_type"] = inp.get("type")
                    if c.get("name") == "ask_user" and isinstance(inp, dict):
                        rt["question"] = inp.get("question")
                    titem = {"k": "tool", "name": c.get("name"),
                             "in": clip(json.dumps(inp, ensure_ascii=False), 2000) if inp else ""}
                    transcript.append(titem)
                    tool_item_by_id[tid] = titem
                elif ctype == "tool_result":
                    tid = c.get("id")
                    cont = c.get("content")
                    rt = tools.setdefault(tid, {})
                    rt["output_size"] = len(json.dumps(cont, ensure_ascii=False)) if cont is not None else 0
                    rt["output_full"] = clip(content_text(cont), 60000)
                    if c.get("is_error") is not None:
                        rt["is_error"] = bool(c.get("is_error"))
                    if not rt.get("name"):
                        rt["name"] = c.get("name")
                    if c.get("name") == "get_screen_context":
                        sc = parse_screen(cont)
                        if sc:
                            screens.append(sc)
                    ti = tool_item_by_id.get(tid)
                    if ti is not None:
                        ti["out"] = clip(content_text(cont), 2000)
                        if c.get("is_error"):
                            ti["err"] = True
                elif ctype == "reasoning":
                    rtext = c.get("text") or ""
                    if rtext.strip():
                        reasoning_steps += 1
                        transcript.append({"k": "reason", "t": rtext})  # full, no truncation
        elif kind == "tool_start":
            tid = e.get("id")
            rt = tools.setdefault(tid, {})
            if not rt.get("name"):
                rt["name"] = e.get("name")
        elif kind == "tool_end":
            tid = e.get("id")
            rt = tools.setdefault(tid, {})
            if not rt.get("name"):
                rt["name"] = e.get("name")
            if e.get("latency_ms") is not None:
                rt["latency_ms"] = e.get("latency_ms")
            if e.get("is_error") is not None:
                rt["is_error"] = bool(e.get("is_error"))
        elif kind == "reasoning":
            rtext = e.get("text") or ""
            if rtext.strip():
                reasoning_steps += 1
                transcript.append({"k": "reason", "t": rtext})  # full, no truncation
        elif kind == "client_tool_dispatch":
            tid = e.get("tool_use_id")
            if tid in tools:
                tools[tid]["dispatched"] = True
            args = e.get("args") or {}
            inter = {"tool": e.get("tool_name"), "ts_ms": e.get("ts_ms")}
            if e.get("tool_name") == "show_widget" and isinstance(args, dict):
                inter["widget_type"] = args.get("type")
                params = args.get("params") or {}
                inter["widget_title"] = params.get("title") if isinstance(params, dict) else None
            if e.get("tool_name") == "open_page" and isinstance(args, dict):
                inter["page_type"] = args.get("type")
            if e.get("tool_name") == "ask_user" and isinstance(args, dict):
                inter["question"] = args.get("question")
            interactions.append(inter)
        elif kind == "cancel_requested":
            cancelled = True

    # finalize tool list
    tool_list = []
    for tid, rt in tools.items():
        if not rt.get("name"):
            continue
        tool_list.append({
            "name": rt.get("name"),
            "input_size": rt.get("input_size", 0),
            "output_size": rt.get("output_size", 0),
            "latency_ms": rt.get("latency_ms"),
            "is_error": rt.get("is_error", False),
            "dispatched": rt.get("dispatched", False),
            "widget_type": rt.get("widget_type"),
            "widget_title": rt.get("widget_title"),
        })

    rec["user_query"] = user_query
    rec["all_user_text"] = all_user_text
    rec["reasoning_steps"] = reasoning_steps
    rec["cancelled"] = cancelled
    rec["tools"] = tool_list
    rec["interactions"] = interactions
    rec["n_tools"] = len(tool_list)
    rec["intent"] = classify_intent(user_query, [t["name"] for t in tool_list])
    rec["negative_reason"] = negative_reason(" ".join(all_user_text))
    rec["screen"] = screens[0] if screens else None
    rec["transcript"] = transcript

    # ---- quality / eval pass (per-turn); thread-level rules added later in main()
    raw_tool_calls = []
    for tid in tool_order:
        rt = tools.get(tid, {})
        raw_tool_calls.append({"name": rt.get("name"), "input": rt.get("input_full"),
                               "output": rt.get("output_full", ""), "is_error": rt.get("is_error", False)})
    tdt = ist_dt(started) if started else None
    rec["loaded_skills"] = loaded_skills
    eval_ctx = {
        "response": "\n\n".join(resp_texts),
        "user_text": " ".join(all_user_text),
        "query": user_query or "",
        "tool_calls": raw_tool_calls,
        "turn_date": tdt.date().isoformat() if tdt else None,
        "is_weekend": (tdt.weekday() >= 5) if tdt else False,
        "turn_minute_ist": (tdt.hour * 60 + tdt.minute) if tdt else None,
        "latency_ms": latency or 0,
        "tokens": {"input": usage.get("input_tokens", 0) or 0,
                   "output": usage.get("output_tokens", 0) or 0,
                   "reasoning": usage.get("reasoning_tokens", 0) or 0},
        "outcome": d.get("outcome"),
    }
    rec["violations"] = eval_checks.run_turn_checks(eval_ctx)
    # Stash number-grounding facts for the thread-level pass (main()), which owns
    # the cross-turn pool a single turn can't see. Underscore keys are not
    # serialized (build_payload picks explicit fields).
    rec["_num_facts"] = eval_checks.turn_number_facts(eval_ctx)
    return rec

# ----------------------------------------------------------------------------- personas

def load_personas():
    """user_id -> primary trading persona from trader_persona_june.csv (lowercased).
    Users absent from the CSV are tagged 'unclassified' at emission time."""
    path = os.path.join(ROOT, "trader_persona_june.csv")
    out = {}
    if not os.path.exists(path):
        print("WARN: trader_persona_june.csv not found — all users will be 'unclassified'")
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            uid = (row.get("user_id") or "").strip()
            p = (row.get("primary_persona") or "").strip().lower()
            if uid and p:
                out[uid] = p
    return out

# ----------------------------------------------------------------------------- main

def main():
    files = sorted(glob.glob(os.path.join(ROOT, "threads", "*", "*.json")))
    personas = load_personas()

    # Extract EVERY turn (internal + external). Internal = user_id in INTERNAL_IDS,
    # OR profile email on a company domain (aaritya.com / sahi.com). We then emit
    # the payload twice: external-only (default, dashboard_data.js) and all-users
    # (dashboard_data_all.js, lazy-loaded in the browser only when the scope toggle
    # is switched on).
    all_turns = []
    ext_turns = []
    ident = {}                 # user_id -> {"name","email"} from profile (ALL users)
    internal_users = set()
    internal_turns = 0
    for fp in files:
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        uid = d.get("user_id")
        email, full_name = parse_profile(d)
        dom = email.split("@")[-1].lower() if "@" in email else ""
        is_internal = (uid in INTERNAL_IDS) or (dom in INTERNAL_DOMAINS)
        if uid not in ident:
            ident[uid] = {"name": full_name or "", "email": email or ""}
        rec = extract_turn(d)
        if not rec:
            continue
        all_turns.append(rec)
        if is_internal:
            internal_users.add(uid)
            internal_turns += 1
        else:
            ext_turns.append(rec)

    # Thread-level quality check: NUMBER_UNGROUNDED_NO_FETCH (needs the whole-thread
    # number pool). Turn dicts are shared between all_turns and ext_turns, so ONE pass
    # covers both scopes (running it per scope would append duplicate violations).
    by_thread = defaultdict(list)
    for t in all_turns:
        by_thread[t["thread_id"]].append(t)
    for tl in by_thread.values():
        # Running number pool for the thread: every number any tool has returned,
        # plus every number the user has typed, accumulated in chronological order.
        thread_pool = set()
        for t in sorted(tl, key=lambda r: r["started_at_ms"]):
            # NUMBER_UNGROUNDED_NO_FETCH — likely number hallucination: the reply
            # quotes salient market numbers, the turn fetched NO data, AND the
            # numbers never appeared in any tool output / user message earlier in
            # the thread. Tier-B (review queue), NOT confirmed: the eval cannot
            # see the per-turn realtime_context block (live index levels, VIX,
            # portfolio value) injected into the model's prompt, so an index-level
            # or portfolio quote can be legitimately grounded there. A reviewer
            # confirms; the headline defect count is not inflated.
            nf = t.get("_num_facts") or {}
            if not nf.get("fetched") and not nf.get("screenshot"):
                fresh = [n for n in nf.get("resp_nums", [])
                         if n not in _GROUNDING_CONSTANTS
                         and not (n < 1000 and n % 100 == 0)   # bare round hundreds = generic
                         and not eval_checks.grounded_in_pool(n, thread_pool)]
                if fresh:
                    ex = ", ".join(f"{n:g}" for n in sorted(fresh, reverse=True)[:5])
                    t.setdefault("violations", []).append({
                        "rule": "NUMBER_UNGROUNDED_NO_FETCH",
                        "dim": "Source Grounding / Attribution",
                        "severity": "medium", "tier": "B",
                        "evidence": f"{len(fresh)} salient number(s) with no tool/user source "
                                    f"earlier in the thread and no fetch this turn "
                                    f"(may be realtime-context — review): e.g. {ex}"})
            # Feed this turn's tool numbers + user numbers into the running pool
            # AFTER checking, so a number must have been established earlier.
            thread_pool |= nf.get("pool", set())
            thread_pool |= nf.get("user_nums", set())

    data_ext = build_payload(ext_turns, ident, personas, "external", len(files), internal_users, internal_turns)
    data_all = build_payload(all_turns, ident, personas, "all", len(files), internal_users, internal_turns)
    _write(os.path.join(ROOT, "dashboard_data.js"), "DASHBOARD_DATA", data_ext)
    _write(os.path.join(ROOT, "dashboard_data_all.js"), "DASHBOARD_DATA_ALL", data_all)

    def flagged_a(turns):
        return sum(1 for t in turns if any(x.get("tier") == "A" for x in t.get("violations", [])))

    sz = os.path.getsize(os.path.join(ROOT, "dashboard_data.js")) / 1024
    sza = os.path.getsize(os.path.join(ROOT, "dashboard_data_all.js")) / 1024
    n_class = sum(1 for u in ident if personas.get(u))
    print("Wrote dashboard_data.js (external) + dashboard_data_all.js (all)")
    print(f"  external ... {len(set(t['user_id'] for t in ext_turns))} users / {len(ext_turns)} turns "
          f"· {flagged_a(ext_turns)} Tier-A flagged")
    print(f"  internal ... {len(internal_users)} users / {internal_turns} turns (excluded by default)")
    print(f"  all ........ {len(set(t['user_id'] for t in all_turns))} users / {len(all_turns)} turns "
          f"· {flagged_a(all_turns)} Tier-A flagged")
    print(f"  personas ... {n_class}/{len(ident)} users classified (rest 'unclassified')")
    print(f"  sizes ...... external {sz:.0f} KB · all {sza:.0f} KB")


def _write(path, varname, data):
    """Atomically write a `window.<varname> = {...};` data file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by build_dashboard.py — do not edit by hand.\n")
        f.write(f"window.{varname} = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    os.replace(tmp, path)  # atomic swap — a live server never sees a half-written file


def build_payload(turns, ident, personas, scope, files_count, internal_users, internal_turns):
    """Emit the raw per-turn payload for one scope ('external' or 'all').

    No aggregation happens here — assets/dashboard_calc.js (buildData) computes
    every dashboard metric in the browser from these records, so the global
    date-range / persona filters can recompute everything client-side."""
    turns = sorted(turns, key=lambda r: r["started_at_ms"])
    date_keys = sorted({ist_dt(t["started_at_ms"]).date().isoformat()
                        for t in turns if t["started_at_ms"]})
    # The full skill set is loaded into context on every turn (not invoked on
    # demand) and is identical across turns — ship it once, not per turn.
    skill_sets = {tuple(sorted(t["skills_loaded"])) for t in turns if t["skills_loaded"]}
    skills_available = sorted({s for t in turns for s in t["skills_loaded"]})
    now_ist = datetime.now(IST)

    out_turns = []
    for t in turns:
        tools = []
        for x in t["tools"]:
            e = {"name": x["name"], "input_size": x["input_size"], "output_size": x["output_size"]}
            if x.get("latency_ms") is not None:
                e["latency_ms"] = x["latency_ms"]
            if x.get("is_error"):
                e["is_error"] = True
            if x.get("dispatched"):
                e["dispatched"] = True
            if x.get("widget_type"):
                e["widget_type"] = x["widget_type"]
            if x.get("widget_title"):
                e["widget_title"] = x["widget_title"]
            tools.append(e)
        out_turns.append({
            "thread_id": t["thread_id"],
            "user_id": t["user_id"],
            "ts_ms": t["started_at_ms"],
            "latency_ms": t["latency_ms"],
            "outcome": t["outcome"],
            "stop_reason": t["stop_reason"],
            "cancelled": 1 if (t["cancelled"] or t["outcome"] == "cancelled") else 0,
            "tokens": t["tokens"],
            "tools": tools,
            "interactions": t["interactions"],
            "follow_ups": t["follow_ups"],
            "loaded_skills": t["loaded_skills"],
            "query": t["user_query"] or "",
            "user_text": t["all_user_text"],
            "reasoning_steps": t["reasoning_steps"],
            "intent": t["intent"],
            "negative_reason": t["negative_reason"],
            "screen": t["screen"],
            "transcript": t["transcript"],
            "violations": t.get("violations", []),
        })

    return {
        "meta": {
            "scope": scope,
            "has_all_scope": scope == "external",
            "internal_listed": len(INTERNAL_IDS),
            "internal_users_excluded": len(internal_users) if scope == "external" else 0,
            "internal_turns_excluded": internal_turns if scope == "external" else 0,
            "internal_users_included": 0 if scope == "external" else len(internal_users),
            "internal_turns_included": 0 if scope == "external" else internal_turns,
            "total_turn_files": files_count,
            "date_min": date_keys[0] if date_keys else None,
            "date_max": date_keys[-1] if date_keys else None,
            "timezone": "Asia/Kolkata (IST)",
            "generated_at": now_ist.strftime("%d %b %Y, %H:%M IST"),
            "generated_at_ms": int(now_ist.timestamp() * 1000),
            "skills_available": skills_available,
            "skills_constant": len(skill_sets) <= 1,
        },
        "pricing": PRICING,
        "client_tools": sorted(CLIENT_TOOLS),
        # Only ship identities for users actually in this scope's turns — keeps
        # internal-user names/emails out of the external payload.
        "ident": {uid: {"name": ident.get(uid, {}).get("name", ""),
                        "email": ident.get(uid, {}).get("email", ""),
                        "persona": personas.get(uid, "unclassified")}
                  for uid in {t["user_id"] for t in turns}},
        "turns": out_turns,
    }

if __name__ == "__main__":
    main()
