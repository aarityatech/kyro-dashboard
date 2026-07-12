#!/usr/bin/env python3
"""
eval_checks.py — automated quality-eval rules for the Kyro dashboard.

Each rule is a pure function over one turn's data (no LLM). Rules are split into:
  TIER A — deterministic, high-precision. Counted as confirmed defects (headline).
  TIER B — heuristic. Surfaced as a "review queue" (candidates a human/LLM confirms),
           NOT counted in the headline flagged metric.

run_turn_checks(ctx) -> list[ {rule, dim, severity, tier, evidence} ]

ctx keys:
  response        full assistant text (str)
  user_text       all user text this turn (str)
  query           opening user query (str)
  tool_calls      ordered list of {name, input(dict|None), output(str, full), is_error(bool)}
  turn_date       'YYYY-MM-DD' in IST
  is_weekend      bool
  latency_ms, tokens{input,output,reasoning}, outcome

Thread-level NUMBER_UNGROUNDED_NO_FETCH is computed in build_dashboard.py (needs the whole thread).
Stdlib only.
"""
import json
import re
from datetime import datetime

# ----- dimension names (must match evaluation_dimensions_augmented.csv) -----
D_FRESH = "Data Freshness & Market-Clock Grounding"
D_SOURCE = "Source Grounding / Attribution"
D_SELF = "Self-Consistency / Reconciliation"
D_ACTION = "Action Authorization & Capability Honesty"
D_TOOL = "Tool Outcome Correctness"
D_LOOP = "Loop / Non-termination"
D_PRES = "Presentation"
D_LAT = "Latency breach"
D_VERB = "Verbosity"

LATENCY_BUDGET_MS = 60000

# live / present-tense market language (used only when data is from a prior day)
LIVE_RX = re.compile(
    r"\b(right now|currently|at the moment|as we speak|in real[- ]?time|as it happens|"
    r"is trading|trading at|is rallying|rallying|witnessing|holds firm|holding steady|"
    r"heating up|firing on all cylinders|is up|is down|is hovering|hovering|is pushing|"
    r"knocking on|today|live option chain|live market|right now)\b", re.I)

# tool-call syntax / internal identifiers leaking into user text
LEAK_RX = re.compile(
    r"\b(show_widget|open_page|ask_user|show_order_form|get_screen_context|scan_instruments|"
    r"get_live_option_chain|get_historical_candles|get_market_commentary|compute_margin|"
    r"explore_strategies|get_realized_pnl|chart_[a-z_]+|load_skill|get_positions|get_holdings)\s*\(", re.I)

LATEX_RX = re.compile(r"(\\frac|\\times|\\cdot|\$\$|\\\[|\\\]|\\text\{|\\sqrt|\\sum_)")

SUCCESS_RX = re.compile(
    r"(I['’]ve (opened|navigated|prepared|set up|taken you|pulled up|loaded)|"
    r"navigated you|is now (open|ready|live|full[- ]?screen)|now (open|ready) (on|for)|"
    r"I have (opened|navigated|prepared)|screen is now|chart is now)", re.I)

# tool error signatures that indicate a malformed call (vs a legit 'no data' answer)
PARAM_ERR_RX = re.compile(
    r"(end time is required|specify either|missing field|unknown field|invalid type|"
    r"could not compile expressions|unknown function|unknown tool|unsupported tag|"
    r"invalid sigil|pass one of|invalid argument|expected a string|invalid arguments)", re.I)

GREEK_HEADER_RX = re.compile(r"\|\s*(delta|gamma|theta|vega)\b", re.I)
GREEK_KEY_RX = re.compile(r'"(delta|gamma|theta|vega)"\s*:', re.I)

# as-of timestamp keys (NOT valid_till / expiry, which are forward-looking)
TS_RX = re.compile(r'"(analysed_at|time|timestamp|news_time|last_trade_time)"\s*:\s*"'
                   r'(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})')

PNL_TOOLS = {"get_realized_pnl", "get_trade_history"}
DISPATCH_TOOLS = {"open_page", "show_order_form", "show_widget"}

# NSE/BSE continuous session in IST, as minute-of-day. Before the open the
# previous close is the freshest tick, so "as-of yesterday" is not stale.
MARKET_OPEN_MIN = 9 * 60 + 15    # 09:15
MARKET_CLOSE_MIN = 15 * 60 + 30  # 15:30


def _max_asof_date(tool_calls):
    """Newest 'as-of' date across all tool outputs, or None."""
    best = None
    for tc in tool_calls:
        for m in TS_RX.finditer(tc.get("output") or ""):
            d = m.group(2)
            if best is None or d > best:
                best = d
    return best


def _is_ok_false(out):
    o = (out or "")
    return ('"ok":false' in o.replace(" ", "") or '"ok": false' in o
            or '"error"' in o[:400] and '"ok":true' not in o.replace(" ", "")) and not o.startswith("[duplicate")


# ---------------- number grounding helpers (Tier B) ----------------
NUM_RX = re.compile(r"₹?\s?(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?\s?(L|Cr|cr|k|K|%)?")

def _norm_numbers(text):
    """Return a set of normalized numeric values mentioned in text (scaled by L/Cr/k)."""
    out = set()
    for m in NUM_RX.finditer(text or ""):
        intp = m.group(1).replace(",", "")
        frac = m.group(2) or ""
        suf = (m.group(3) or "").lower()
        try:
            v = float(intp + ("." + frac if frac else ""))
        except ValueError:
            continue
        if suf == "l":
            v *= 1e5
        elif suf == "cr":
            v *= 1e7
        elif suf == "k":
            v *= 1e3
        # percentages are usually derived — keep separately (skip from grounding)
        if suf == "%":
            continue
        out.add(round(v, 2))
    return out


def _salient(v):
    # price/level-looking magnitudes worth grounding; skip tiny counts
    return 100 <= v <= 1e8


def _grounded(v, pool):
    for p in pool:
        if p == 0:
            continue
        if abs(v - p) <= max(0.5, abs(p) * 0.01):   # within 1%
            return True
        # the model often shows OI/units as lakhs/crores of the raw value
        for k in (1e5, 1e7, 1e3):
            if abs(v - p / k) <= max(0.5, abs(p / k) * 0.01) or abs(v - p * k) <= max(0.5, abs(p * k) * 0.01):
                return True
        # index S/R levels get rounded to the nearest 50/100 strike
        for step in (50, 100):
            if abs(v - round(p / step) * step) <= max(0.5, abs(p) * 0.001):
                return True
    return False


# Tools that return authoritative market/account numbers into the model's
# context. A turn with none of these "fetched no data" — a salient number in
# the reply is then genuinely ungrounded (hallucinated from priors), not a
# derivation the heuristic simply can't trace.
_NON_DATA_TOOLS = {
    "show_widget", "open_page", "show_order_form", "show_position_sl_tp_form",
    "show_basket_order_form", "show_feature_intro", "ask_user", "load_skill",
    "get_screen_context", "update_soul", "chart_configure", "chart_add_indicators",
    "chart_remove_indicators", "chart_modify_indicator", "chart_apply_template",
    "chart_save_template", "chart_add_drawing",
}


def _augment_pool_with_deltas(pool):
    """Add pairwise differences of large pool values (OI-scale) so the model's
    correct `curr − prev` OI-change subtractions read as grounded. Bounded to
    large values only (≥10k) so small price levels don't cross-match into noise."""
    big = sorted(p for p in pool if p >= 10_000)
    if len(big) > 60:            # keep O(n²) bounded on huge chains
        big = big[-60:]
    extra = set()
    for i, a in enumerate(big):
        for b in big[i + 1:]:
            extra.add(round(abs(a - b), 2))
    return pool | extra


def turn_number_facts(ctx):
    """Extract the number-grounding facts a thread-level pass needs (it owns the
    cross-turn pool that a single turn can't see). Returns:
      resp_nums  — salient numbers quoted in the reply (minus user-echoed ones)
      pool       — numbers this turn's data-tool outputs contributed
      fetched    — did a data-returning tool run this turn?
      screenshot — was get_screen_context(screenshot) used? (vision-grounded)
    """
    tcs = ctx.get("tool_calls") or []
    pool = set()
    fetched = False
    for tc in tcs:
        if tc.get("name") not in _NON_DATA_TOOLS:
            fetched = True
            pool |= _norm_numbers(tc.get("output") or "")
    screenshot = any(
        tc.get("name") == "get_screen_context" and "screenshot" in str(tc.get("input") or {})
        for tc in tcs
    )
    user_nums = _norm_numbers(ctx.get("user_text") or "")
    resp_nums = [n for n in _norm_numbers(ctx.get("response") or "")
                 if _salient(n) and not _grounded(n, user_nums)]
    return {"resp_nums": resp_nums, "pool": pool, "user_nums": user_nums,
            "fetched": fetched, "screenshot": screenshot}


def grounded_in_pool(v, pool):
    """Public wrapper for the number-grounding match (delta/lakh/round aware).
    Used by the thread-level pass in build_dashboard.py."""
    return _grounded(v, _augment_pool_with_deltas(pool) if pool else pool)


def run_turn_checks(ctx):
    v = []
    resp = ctx.get("response") or ""
    tcs = ctx.get("tool_calls") or []

    def add(rule, dim, sev, tier, ev):
        v.append({"rule": rule, "dim": dim, "severity": sev, "tier": tier, "evidence": ev[:240]})

    # ============================= TIER A =============================

    # 1. STALE_AS_LIVE — freshest data is from a prior calendar day, narrated live.
    # Only genuine INTRADA​Y staleness counts: before the 09:15 IST open (and on
    # weekends), the previous session's close IS the freshest data, so quoting it
    # is correct, not stale — that pre-market/weekend case was ~all of the false
    # positives. Fire only when the market was actually open at turn time.
    asof = _max_asof_date(tcs)
    tmin = ctx.get("turn_minute_ist")
    market_open_at_turn = (
        not ctx.get("is_weekend")
        and tmin is not None
        and MARKET_OPEN_MIN <= tmin <= MARKET_CLOSE_MIN
    )
    if (asof and ctx.get("turn_date") and asof < ctx["turn_date"]
            and market_open_at_turn and LIVE_RX.search(resp)):
        mlive = LIVE_RX.search(resp)
        add("STALE_AS_LIVE", D_FRESH, "high", "A",
            f"data as-of {asof} but turn is {ctx['turn_date']} during market hours; "
            f"live phrasing “{mlive.group(0)}”")

    # 2. PNL_WRONG_DATE — realized-pnl/trade-history date arg is implausible
    # for this turn. The original check flagged ANY date != turn_date, which
    # mis-fired on three documented-correct call shapes (~75% of Jun–Jul flags):
    #   • month anchors: date=1st + complete_month=true ("this month" convention)
    #   • month-chunked walks (FY / lifetime: Apr 1 + May 1 + Jun 1 in parallel)
    #   • recent explicit windows (yesterday / last week / rolling 30d)
    # Now only genuinely suspect dates flag:
    #   • future dates                            → Tier A high
    #   • >13 months back and the user never
    #     mentioned that year (wrong-year
    #     hallucination, e.g. 2025-07-23 for
    #     "summarize my trades today")            → Tier A high
    #   • single-day anchor >35d back without
    #     complete_month (stale-anchor smell)     → Tier B review queue
    utext = (ctx.get("user_text") or "").lower()
    for tc in tcs:
        if tc.get("name") in PNL_TOOLS and isinstance(tc.get("input"), dict):
            d = tc["input"].get("date")
            turn = ctx.get("turn_date")
            if not (isinstance(d, str) and re.match(r"\d{4}-\d{2}-\d{2}", d) and turn):
                continue
            d = d[:10]
            try:
                delta = (datetime.strptime(turn, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days
            except ValueError:
                continue
            month_anchor = bool(tc["input"].get("complete_month")) and d.endswith("-01")
            year_mentioned = d[:4] in utext or "last year" in utext or "previous year" in utext \
                or re.search(r"\bfy\s?\d{2}", utext)
            if delta < 0:
                add("PNL_WRONG_DATE", D_FRESH, "high", "A",
                    f"{tc['name']} queried FUTURE date={d}; turn date is {turn}")
                break
            cross_year = d[:4] != turn[:4]
            if not month_anchor and not year_mentioned and (delta > 396 or (cross_year and delta > 90)):
                add("PNL_WRONG_DATE", D_FRESH, "high", "A",
                    f"{tc['name']} queried date={d}, {delta}d before turn {turn} — likely hallucinated year")
                break
            if delta > 35 and not month_anchor and not year_mentioned:
                add("PNL_WRONG_DATE", D_FRESH, "high", "B",
                    f"{tc['name']} queried single-day date={d} ({delta}d before turn {turn}) without complete_month — stale anchor?")
                break

    # 3. DEDUP loops — harness blocked an identical (tool,args) re-issue. Split by
    # whether the ORIGINAL call the retry duplicated had errored or succeeded:
    #   • after error   — model re-fired the SAME args after a failure (identical
    #                     args can't fix the error → a wasted retry loop)
    #   • after success — pure redundant re-issue of a call that already worked
    err_dups, ok_dups = [], []
    for i, tc in enumerate(tcs):
        if not (tc.get("output") or "").startswith("[duplicate"):
            continue
        name = tc.get("name")
        # the original = nearest earlier same-name call that isn't itself a dup marker
        orig = None
        for j in range(i - 1, -1, -1):
            p = tcs[j]
            if p.get("name") == name and not (p.get("output") or "").startswith("[duplicate"):
                orig = p
                break
        out = (orig.get("output") or "") if orig else ""
        errored = bool(orig and (orig.get("is_error") or PARAM_ERR_RX.search(out) or _is_ok_false(out)))
        (err_dups if errored else ok_dups).append(name)
    if err_dups:
        add("DEDUP_RETRY_AFTER_ERROR", D_LOOP, "high", "A",
            f"{len(err_dups)} identical re-issue(s) of a call that ERRORED: {', '.join(sorted(set(err_dups)))}")
    if ok_dups:
        add("DEDUP_RETRY_AFTER_SUCCESS", D_LOOP, "medium", "A",
            f"{len(ok_dups)} identical re-issue(s) of a call that ALREADY SUCCEEDED: {', '.join(sorted(set(ok_dups)))}")

    # 4. TOOL_PARAM_ERROR — malformed-call error signatures
    perr = []
    for tc in tcs:
        out = tc.get("output") or ""
        if out.startswith("[duplicate"):
            continue
        if (tc.get("is_error") or PARAM_ERR_RX.search(out)) and PARAM_ERR_RX.search(out):
            sigm = PARAM_ERR_RX.search(out)
            perr.append(f"{tc.get('name')}: {sigm.group(0)}")
    if perr:
        add("TOOL_PARAM_ERROR", D_TOOL, "high", "A", "; ".join(perr[:4]))

    # 5. EMPTY_ERRORED_TURN — turn errored with no user-facing text
    if ctx.get("outcome") == "errored" and not resp.strip():
        add("EMPTY_ERRORED_TURN", D_TOOL, "medium", "A",
            f"outcome=errored, empty response after {len(tcs)} tool call(s)")

    # 6. LEAKED_TOOL_SYNTAX — internal tool-call syntax in user text
    ml = LEAK_RX.search(resp)
    if ml:
        add("LEAKED_TOOL_SYNTAX", D_PRES, "high", "A", f"reply contains “{ml.group(0)}…”")

    # 7. LATEX_LEAK — raw LaTeX on a chat surface
    mlx = LATEX_RX.search(resp)
    if mlx:
        add("LATEX_LEAK", D_PRES, "low", "A", f"raw LaTeX token “{mlx.group(0)}”")

    # 8. DUP_TEXT_BLOCK — same substantial paragraph repeated in one reply
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", resp) if len(p.strip()) > 60]
    seen = {}
    for p in paras:
        seen[p] = seen.get(p, 0) + 1
    rep = [p for p, c in seen.items() if c >= 2]
    if rep:
        add("DUP_TEXT_BLOCK", D_PRES, "high", "A", f"paragraph emitted ×{seen[rep[0]]}: “{rep[0][:90]}…”")

    # 9. OVER_BUDGET_LATENCY — wall-clock over budget, EXCLUDING turns that used
    # ask_user: those block on the human to tap a chip, so the wall-clock is
    # mostly user think-time, not agent slowness — not an actionable latency defect.
    used_ask_user = any(tc.get("name") == "ask_user" for tc in tcs)
    if (ctx.get("latency_ms") or 0) > LATENCY_BUDGET_MS and not used_ask_user:
        add("OVER_BUDGET_LATENCY", D_LAT, "high", "A", f"{ctx['latency_ms']/1000:.0f}s wall-clock (budget {LATENCY_BUDGET_MS/1000:.0f}s)")

    # (OVER_BUDGET_TOKENS removed — input-token count is a cost signal, not a
    # response-quality defect; it inflated the Tier-A count without pointing at
    # anything a reviewer could act on.)

    # 11. ASKUSER_TIMEOUT_THEN_ACTION — acted after an unanswered confirmation
    timeout_idx = None
    for i, tc in enumerate(tcs):
        if tc.get("name") == "ask_user" and "did not respond in time" in (tc.get("output") or "").lower():
            timeout_idx = i
            break
    if timeout_idx is not None:
        acted = [tc.get("name") for tc in tcs[timeout_idx + 1:]
                 if tc.get("name") in ("show_order_form", "open_page")]
        if acted:
            add("ASKUSER_TIMEOUT_THEN_ACTION", D_ACTION, "low", "A",
                f"ask_user timed out, then dispatched {', '.join(sorted(set(acted)))}")
        elif SUCCESS_RX.search(resp) or "you selected" in resp.lower() or "your selection" in resp.lower():
            add("ASKUSER_TIMEOUT_THEN_ACTION", D_ACTION, "low", "A",
                "ask_user timed out but reply asserts a user choice/action")

    # 12. SUCCESS_ON_FAILURE — success phrasing while a dispatch returned ok:false/error
    failed_disp = [tc.get("name") for tc in tcs
                   if tc.get("name") in DISPATCH_TOOLS and _is_ok_false(tc.get("output"))]
    if failed_disp and SUCCESS_RX.search(resp):
        add("SUCCESS_ON_FAILURE", D_TOOL, "high", "A",
            f"reply claims success but {', '.join(sorted(set(failed_disp)))} returned ok:false")

    # 13. TOPN_CAP — scan matched N but fewer shown, no 'of N / more' disclosure
    matched = 0
    for tc in tcs:
        if tc.get("name") == "scan_instruments":
            mm = re.search(r'"matched"\s*:\s*(\d+)', tc.get("output") or "")
            if mm:
                matched = max(matched, int(mm.group(1)))
    if matched > 0:
        shown = 0
        for tc in tcs:
            if tc.get("name") == "show_widget" and isinstance(tc.get("input"), dict):
                ids = (tc["input"].get("params") or {}).get("instrument_ids")
                if isinstance(ids, list):
                    shown = max(shown, len(ids))
        disclosed = bool(re.search(r"\bof\s+\d+\b|\bmore\b|showing\s+\d+|\b\d+\s+matches?\b", resp, re.I))
        if shown and matched > shown and not disclosed:
            add("TOPN_CAP", D_VERB, "medium", "A",
                f"scan matched {matched} but showed {shown} with no 'of {matched}' disclosure")

    # 14. GREEKS_NO_SOURCE — Delta/Gamma column rendered but no greek field in any tool output
    if GREEK_HEADER_RX.search(resp):
        has_greek = any(GREEK_KEY_RX.search(tc.get("output") or "") for tc in tcs)
        if not has_greek:
            add("GREEKS_NO_SOURCE", D_SOURCE, "high", "A",
                "reply shows a Greeks column but no tool output contains delta/gamma/theta/vega")

    # ============================= TIER B (review queue) =============================

    # 15. NUMBER_NOT_GROUNDED — salient price/level numbers absent from this
    # turn's tool outputs. Only meaningful when the turn fetched data (else the
    # numbers may legitimately come from earlier in the thread — that genuine-
    # hallucination case is caught thread-level in build_dashboard.py, which has
    # the full conversation pool). Pool is augmented with large-value deltas so
    # correct `curr − prev` OI-change subtractions read as grounded; threshold
    # raised to 3 to cut derived-number noise. Stays Tier-B (review queue).
    tool_pool = set()
    for tc in tcs:
        if tc.get("name") not in _NON_DATA_TOOLS:
            tool_pool |= _norm_numbers(tc.get("output") or "")
    if tool_pool:
        tool_pool = _augment_pool_with_deltas(tool_pool)
        user_nums = _norm_numbers(ctx.get("user_text") or "")
        resp_nums = [n for n in _norm_numbers(resp)
                     if _salient(n) and not _grounded(n, user_nums)]
        ungrounded = [n for n in resp_nums if not _grounded(n, tool_pool)]
        if len(ungrounded) >= 3:
            ex = ", ".join(f"{n:g}" for n in sorted(ungrounded, reverse=True)[:5])
            add("NUMBER_NOT_GROUNDED", D_SOURCE, "high", "B",
                f"{len(ungrounded)} salient numbers not in this turn's tool output "
                f"(likely derived — verify): e.g. {ex}")

    # 18. TOOL_ERROR_THEN_RECOVERED — a call errored, then a LATER call to the SAME
    # tool with CORRECTED (different) args succeeded, in the same turn. Not a
    # final-answer defect — it's self-correction — but the wasted first attempt is
    # worth tracking as a prompt/schema signal (which tools the model gets wrong
    # first, then fixes). Distinct from the DEDUP rules, where the retry re-used
    # IDENTICAL args and was blocked. Tier B / low (informational, positive-ish).
    recovered = set()
    by_name = {}
    for tc in tcs:
        out = tc.get("output") or ""
        if out.startswith("[duplicate"):
            continue   # blocked identical re-issue — that's the DEDUP rules' territory
        name = tc.get("name")
        errored = bool(tc.get("is_error") or PARAM_ERR_RX.search(out) or _is_ok_false(out))
        try:
            args = json.dumps(tc.get("input"), sort_keys=True, default=str)
        except (TypeError, ValueError):
            args = str(tc.get("input"))
        by_name.setdefault(name, []).append((errored, args))
    for name, calls in by_name.items():
        err_args = set()
        for errored, args in calls:
            if errored:
                err_args.add(args)
            elif err_args and args not in err_args:   # success with corrected args, after an error
                recovered.add(name)
                break
    if recovered:
        add("TOOL_ERROR_THEN_RECOVERED", D_TOOL, "medium", "B",
            f"errored then succeeded on a corrected retry: {', '.join(sorted(recovered))}")

    # 16. SUM_NE_TOTAL — a markdown table's 'total' row != sum of the column above it
    rows = [ln for ln in resp.splitlines() if ln.strip().startswith("|")]
    if len(rows) >= 3:
        grid = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
        ncol = max((len(r) for r in grid), default=0)
        for col in range(ncol):
            colnums, total_row = [], None
            for r in grid:
                if col >= len(r):
                    continue
                first = r[0].lower()
                cell = r[col]
                nums = _norm_numbers(cell)
                if re.search(r"\b(total|कुल|sum|grand)\b", first) and nums:
                    total_row = max(nums)
                elif nums and not re.search(r"[-—]{2,}|:?-+:?", cell):
                    colnums.append(max(nums))
            if total_row and len(colnums) >= 2:
                s = sum(colnums)
                if s > 0 and abs(s - total_row) > max(1.0, total_row * 0.02):
                    add("SUM_NE_TOTAL", D_SELF, "medium", "B",
                        f"table column sums to {s:g} but a total row states {total_row:g}")
                    break

    # 17. SR_LEVEL_NOT_IN_DATA — support/resistance number not matching oi_support/oi_resistance
    oi_levels = set()
    for tc in tcs:
        for key in ("oi_support", "oi_resistance", "max_pain"):
            for mm in re.finditer(r'"' + key + r'"\s*:\s*([\d.]+)', tc.get("output") or ""):
                try:
                    oi_levels.add(round(float(mm.group(1))))
                except ValueError:
                    pass
    if oi_levels:
        claimed = set()
        for mm in re.finditer(r"\b(support|resistance)\b[^.\n]{0,40}?₹?\s?(\d{2,3}(?:,\d{3})*|\d{3,6})",
                              resp, re.I):
            try:
                claimed.add(round(float(mm.group(2).replace(",", ""))))
            except ValueError:
                pass
        bad = [c for c in claimed if c >= 100 and not any(abs(c - o) <= max(1, o * 0.01) for o in oi_levels)]
        if bad:
            add("SR_LEVEL_NOT_IN_DATA", D_SOURCE, "low", "B",
                f"S/R level(s) {sorted(bad)[:4]} not in tool oi_support/oi_resistance {sorted(oi_levels)[:4]}")

    return _annotate_known_fixes(
        v,
        " ".join((tc.get("output") or "")[:400] for tc in tcs),
        ctx.get("turn_date"),
    )


# ----- known-fix annotation --------------------------------------------------
# Error signatures whose fix already exists in a tagged release. Stamped onto
# each matching violation as `fix_status` so the dashboard separates
# "broken at HEAD" from "fixed, awaiting deploy" — during Jun–Jul, most
# Tool-Outcome flags were the latter and read as open bugs.
KNOWN_FIXES = [
    (re.compile(r"unknown function: pivots"),
     "mdp #328 (pivots wired into compile) — verify marketdata prod deploy"),
    (re.compile(r"end time is required"), "sage v26.7.5 (#363: count mode anchors end=now)"),
    (re.compile(r'expected a sequence'), "sage v26.7.5 (#363: flat from.tags coercion)"),
    (re.compile(r"unit variant, expected newtype variant"), "sage v26.7.8 (#370: alert indicator arg)"),
    (re.compile(r"missing field `sequence`"), "sage v26.7.5 (#363: compute_margin top-level alias)"),
    (re.compile(r"Kill switch not set"), "sage v26.7.5 (#363: 404 → enabled:false)"),
    (re.compile(r"get_market_summary|get_ra_commentary|get_realized_pnl_trades"),
     "icp prompt/skill purge of stale tool names (Jul 8, unreleased)"),
]


# Behavioral rules addressed by a prompt release rather than a code fix:
# flags BEFORE the release date get a fix_status; flags on/after it are the
# residual tail the release didn't close. Corpus check (Jun 12 – Jul 7):
# LEAKED_TOOL_SYNTAX 7→1 and DUP_TEXT_BLOCK 12→3 across the Jul 1 boundary.
PROMPT_RELEASE_FIXES = {
    "LEAKED_TOOL_SYNTAX": ("2026-07-02", "kyro prompt release #361 (Jul 1): TOOLS ARE INVOKED, NEVER TYPED law"),
    "DUP_TEXT_BLOCK": ("2026-07-02", "kyro prompt release #361 (Jul 1): widget-renders-once / never re-state laws"),
}


def _annotate_known_fixes(violations, tool_outputs="", turn_date=None):
    for viol in violations:
        hay = viol.get("evidence") or ""
        if viol.get("rule") == "TOOL_PARAM_ERROR":
            # evidence keeps only the short signature — match the raw
            # tool outputs for this rule (the violation IS the outputs).
            hay = f"{hay} {tool_outputs}"
        for rx, fixed in KNOWN_FIXES:
            if rx.search(hay):
                viol["fix_status"] = f"fixed in {fixed}"
                break
        if "fix_status" not in viol and turn_date:
            cutoff_note = PROMPT_RELEASE_FIXES.get(viol.get("rule"))
            if cutoff_note and turn_date < cutoff_note[0]:
                viol["fix_status"] = f"addressed by {cutoff_note[1]}"
    return violations
