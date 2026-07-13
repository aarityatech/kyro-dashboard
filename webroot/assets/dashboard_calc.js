// dashboard_calc.js — client-side aggregation for the Kyro analytics dashboard.
//
// build_dashboard.py ships raw per-turn records:
//   window.DASHBOARD_DATA = { meta, pricing, client_tools, ident, turns }
// and buildData(raw, turns) below computes every dashboard aggregate from any
// subset of those turns. This is what lets the global date-range and persona
// filters recompute all metrics in the browser.
//
// It is a faithful port of the former build_data() in build_dashboard.py —
// section order and output shape match the original so index.html renderers
// are unchanged.

(function () {
'use strict';

const IST_OFFSET_MS = 5.5 * 3600 * 1000;   // Asia/Kolkata
const DOW_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// Target fleet size for the "scaled to N users" cost projection on the Tokens tab.
// Change this one number to re-scale the planning estimate.
const TARGET_USERS = 25000;

// ---------------------------------------------------------------- time helpers
// Shift the epoch by +5:30 and read UTC fields => IST calendar parts.
function istParts(ms) {
  const d = new Date(ms + IST_OFFSET_MS);
  return {
    day: d.toISOString().slice(0, 10),
    month: d.toISOString().slice(0, 7),
    hour: d.getUTCHours(),
    minutes: d.getUTCHours() * 60 + d.getUTCMinutes(),
    dow: (d.getUTCDay() + 6) % 7,            // 0 = Monday (python weekday())
    shifted: d,
  };
}

// ISO week id ("2026-W24") + its Monday date, from an IST-shifted Date.
function isoWeekInfo(sh) {
  const d = Date.UTC(sh.getUTCFullYear(), sh.getUTCMonth(), sh.getUTCDate());
  const dayNum = (new Date(d).getUTCDay() + 6) % 7;
  const monday = d - dayNum * 86400000;
  const thursday = monday + 3 * 86400000;
  const isoYear = new Date(thursday).getUTCFullYear();
  const jan4 = Date.UTC(isoYear, 0, 4);
  const firstMonday = jan4 - ((new Date(jan4).getUTCDay() + 6) % 7) * 86400000;
  const week = 1 + Math.round((monday - firstMonday) / 604800000);
  return {
    wk: isoYear + '-W' + String(week).padStart(2, '0'),
    label: new Date(monday).toISOString().slice(0, 10),
  };
}

// ---------------------------------------------------------------- math helpers
const r1 = v => Math.round(v * 10) / 10;
const r2 = v => Math.round(v * 100) / 100;
const r4 = v => Math.round(v * 1e4) / 1e4;
const r5 = v => Math.round(v * 1e5) / 1e5;
const sum = a => a.reduce((x, y) => x + y, 0);

// Linear-interpolation percentile. p in [0,1]. s must be sorted ascending.
function pct(s, p) {
  if (!s.length) return 0;
  if (s.length === 1) return s[0];
  const k = (s.length - 1) * p, f = Math.floor(k), c = Math.ceil(k);
  if (f === c) return s[k];
  return s[f] * (c - k) + s[c] * (k - f);
}

function stats(vals) {
  if (!vals.length) return { n: 0, mean: 0, p50: 0, p90: 0, p99: 0, max: 0, min: 0, sum: 0 };
  const s = [...vals].sort((a, b) => a - b), tot = sum(s);
  return {
    n: s.length, mean: r1(tot / s.length),
    p50: r1(pct(s, 0.5)), p90: r1(pct(s, 0.9)), p99: r1(pct(s, 0.99)),
    max: r1(s[s.length - 1]), min: r1(s[0]), sum: r1(tot),
  };
}

// Bucket vals into ranges defined by edges (labels.length === edges.length+1).
function histogram(vals, edges, labels) {
  const counts = labels.map(() => 0);
  for (const v of vals) {
    let placed = false;
    for (let i = 0; i < edges.length; i++) {
      if (v <= edges[i]) { counts[i]++; placed = true; break; }
    }
    if (!placed) counts[counts.length - 1]++;
  }
  return labels.map((label, i) => ({ label, count: counts[i] }));
}

// Counter — mostCommon() sorts by count desc, ties keep insertion order
// (stable sort), matching python Counter.most_common.
class Counter extends Map {
  add(k, n = 1) { this.set(k, (this.get(k) || 0) + n); }
  mostCommon(n) {
    const a = [...this.entries()].sort((x, y) => y[1] - x[1]);
    return n ? a.slice(0, n) : a;
  }
}

const clip = (s, n) => { s = s || ''; return s.length > n ? s.slice(0, n) + ' …[truncated]' : s; };

// ---------------------------------------------------------------- query tokenizer
const STOPWORDS = new Set(`
a an the and or but if then else for to of in on at by with from into about as is are
am was were be been being do does did doing have has had having i me my we our you your
he she it they them his her its their this that these those what which who whom whose
will would shall should can could may might must not no yes so just very too also more
most some any all each every other such only own same than how when where why s t re ve
ll d m o y ain don isn aren wasn weren hasn haven hadn doesn didn won wouldn shouldn
couldn mustn please give show tell want need get got make let know see look use using
mine ok okay hi hello hey thanks thank pls plz kindly wanna gonna
`.trim().split(/\s+/));

function tokenize(text) {
  const words = (text || '').toLowerCase().match(/[a-z][a-z'&/]+/g) || [];
  const out = [];
  for (let w of words) {
    w = w.replace(/^['&/]+/, '').replace(/['&/]+$/, '');
    if (w.length < 3 || STOPWORDS.has(w)) continue;
    out.push(w);
  }
  return out;
}

const SCREEN_RX = /analy[sz]e?\s+(this|the|my)?\s*screen/;

const LAT_EDGES = [2, 5, 10, 20, 30, 60];
const LAT_LABELS = ['0-2s', '2-5s', '5-10s', '10-20s', '20-30s', '30-60s', '60s+'];

// =============================================================== buildData
// raw     — the full payload written by build_dashboard.py
// turns   — the (possibly filtered) subset of raw.turns to aggregate
// history — same population as turns but WITHOUT the date filter (persona/scope
//           still applied). Metrics that need data outside the selected range —
//           new/returning splits, trailing-7d stickiness, M1 cohorts, thread
//           start dates — are computed from history, then only rows inside the
//           selected range are emitted. Omitting it falls back to turns.
function buildData(raw, turnsIn, historyIn) {
  const history = historyIn || turnsIn;
  const identMap = raw.ident || {};
  const CLIENT_TOOLS = new Set(raw.client_tools || []);
  const PRICING = raw.pricing || { model: '?', input_per_m: 0, cached_input_per_m: 0, output_per_m: 0 };
  const personaOf = uid => ((identMap[uid] || {}).persona) || 'unclassified';

  const turns = [...turnsIn].sort((a, b) => a.ts_ms - b.ts_ms);
  const usersSeen = new Set(turns.map(t => t.user_id));

  // ---------- group by thread
  const threads = new Map();
  for (const t of turns) {
    if (!threads.has(t.thread_id)) threads.set(t.thread_id, []);
    threads.get(t.thread_id).push(t);
  }

  // ---------- daily / hourly / dow
  const daily = new Map();
  const hourly = new Counter(), dow = new Counter();
  const userDays = new Map();                // user_id -> Set(day)
  for (const t of turns) {
    if (!t.ts_ms) continue;
    const P = istParts(t.ts_ms), day = P.day;
    if (!userDays.has(t.user_id)) userDays.set(t.user_id, new Set());
    userDays.get(t.user_id).add(day);
    let Dd = daily.get(day);
    if (!Dd) {
      Dd = { turns: 0, users: new Set(), tokens: 0, input: 0, output: 0,
             cache_read: 0, cache_write: 0, reasoning: 0, tool_calls: 0 };
      daily.set(day, Dd);
    }
    Dd.turns++; Dd.users.add(t.user_id);
    const tk = t.tokens;
    Dd.input += tk.input; Dd.output += tk.output;
    Dd.cache_read += tk.cache_read; Dd.cache_write += tk.cache_write;
    Dd.reasoning += tk.reasoning;
    Dd.tokens += tk.input + tk.output;
    Dd.tool_calls += t.tools.length;
    hourly.add(P.hour); dow.add(P.dow);
  }
  // Thread start day comes from full history so a thread that began before the
  // selected range isn't re-counted as "started" inside it.
  const threadFirstDay = new Map();          // thread_id -> [ms, day]
  for (const t of history) {
    if (!t.ts_ms) continue;
    const th = t.thread_id;
    if (!threadFirstDay.has(th) || t.ts_ms < threadFirstDay.get(th)[0]) threadFirstDay.set(th, [t.ts_ms, istParts(t.ts_ms).day]);
  }
  const threadsStartedByDay = new Counter();
  for (const [, day] of threadFirstDay.values()) if (daily.has(day)) threadsStartedByDay.add(day);

  const sortedDays = [...daily.keys()].sort();
  const dailySeries = sortedDays.map(day => {
    const Dd = daily.get(day);
    return { date: day, turns: Dd.turns, threads_started: threadsStartedByDay.get(day) || 0,
             dau: Dd.users.size, tokens: Dd.tokens, input: Dd.input, output: Dd.output,
             cache_read: Dd.cache_read, cache_write: Dd.cache_write,
             reasoning: Dd.reasoning, tool_calls: Dd.tool_calls };
  });

  // ---------- per-user
  const userAgg = new Map();
  for (const t of turns) {
    let u = userAgg.get(t.user_id);
    if (!u) {
      u = { turns: 0, threads: new Set(), tokens: 0, tool_calls: 0,
            errors: 0, cancelled: 0, latencies: [], intents: new Counter() };
      userAgg.set(t.user_id, u);
    }
    u.turns++; u.threads.add(t.thread_id);
    u.tokens += t.tokens.input + t.tokens.output;
    u.tool_calls += t.tools.length;
    u.errors += t.tools.filter(x => x.is_error).length;
    if (t.cancelled) u.cancelled++;
    if (t.latency_ms) u.latencies.push(t.latency_ms);
    u.intents.add(t.intent);
  }
  const usersTable = [];
  for (const [uid, u] of userAgg) {
    const info = identMap[uid] || {};
    const lat = [...u.latencies].sort((a, b) => a - b);
    usersTable.push({
      user_id: uid, name: info.name || '', email: info.email || '', persona: personaOf(uid),
      turns: u.turns, threads: u.threads.size,
      active_days: (userDays.get(uid) || new Set()).size,
      tokens: u.tokens, tool_calls: u.tool_calls, errors: u.errors, cancelled: u.cancelled,
      avg_latency_ms: lat.length ? Math.round(sum(lat) / lat.length) : 0,
      p90_latency_ms: lat.length ? Math.round(pct(lat, 0.9)) : 0,
      top_intent: u.intents.size ? u.intents.mostCommon(1)[0][0] : '-',
    });
  }
  usersTable.sort((a, b) => b.turns - a.turns);

  // ---------- queries
  const typedQueries = [];
  let screenContext = 0;
  const allQueryRecords = [], negativeRecords = [], queryLengths = [];
  const wordCounter = new Counter(), intentCounter = new Counter();
  for (const t of turns) {
    const q = t.query || '';
    intentCounter.add(t.intent);
    const isScreen = SCREEN_RX.test(q.toLowerCase());
    const userDisp = (identMap[t.user_id] || {}).name || String(t.user_id || '').slice(0, 8);
    allQueryRecords.push({
      text: q, user: userDisp, user_id: t.user_id, thread: t.thread_id, ts_ms: t.ts_ms,
      intent: t.intent, outcome: t.outcome, n_tools: t.tools.length,
      detail: t.transcript, violations: t.violations || [],
    });
    if (isScreen) screenContext++;
    else if (q.trim()) {
      typedQueries.push(q.trim());
      queryLengths.push(q.split(/\s+/).filter(Boolean).length);
      for (const w of tokenize(q)) wordCounter.add(w);
    }
    if (t.negative_reason) {
      negativeRecords.push({ text: q, user: userDisp, user_id: t.user_id, ts_ms: t.ts_ms,
                             reason: t.negative_reason, outcome: t.outcome });
    }
  }
  const norm = new Counter();
  for (const q of typedQueries) norm.add(q.trim().toLowerCase().replace(/\s+/g, ' '));
  const topQueries = norm.mostCommon(25).map(([text, count]) => ({ text, count }));
  const wordcloud = wordCounter.mostCommon(120);

  // ---------- conversations
  const depth = new Counter();
  for (const tl of threads.values()) depth.add(tl.length);
  const depthHist = [...depth.entries()].sort((a, b) => a[0] - b[0])
    .map(([k, v]) => ({ turns: k, count: v }));
  const turnsPerThread = [...threads.values()].map(tl => tl.length);

  const threadsTable = [];
  for (const [th, tl] of threads) {
    const tls = [...tl].sort((a, b) => a.ts_ms - b.ts_ms);
    const uid = tls[0].user_id, info = identMap[uid] || {};
    const samples = [], seen = new Set();
    for (const t of tls) {
      const qq = (t.query || '').trim();
      if (!qq) continue;
      const key = qq.toLowerCase().slice(0, 80);
      if (seen.has(key)) continue;
      seen.add(key);
      samples.push(qq.length <= 160 ? qq : qq.slice(0, 160) + ' …');
      if (samples.length >= 3) break;
    }
    // full user<->assistant exchange (text only — no tools / reasoning)
    const exchange = [];
    for (const t of tls) {
      for (const ut of (t.user_text || [])) if (ut && ut.trim()) exchange.push({ r: 'u', t: clip(ut.trim(), 4000) });
      for (const it of (t.transcript || [])) if (it.k === 'resp' && (it.t || '').trim()) exchange.push({ r: 'a', t: clip(it.t.trim(), 4000) });
    }
    threadsTable.push({ thread_id: th, user_id: uid, name: info.name || '',
                        turns: tl.length, start_ms: tls[0].ts_ms, samples, exchange });
  }
  threadsTable.sort((a, b) => (b.turns - a.turns) || ((b.start_ms || 0) - (a.start_ms || 0)));

  let followupTotal = 0, turnsWithFollowups = 0;
  const followupTypes = new Counter(), followupLabels = new Counter();
  for (const t of turns) {
    const fu = t.follow_ups || [];
    followupTotal += fu.length;
    if (fu.length) turnsWithFollowups++;
    for (const f of fu) { followupTypes.add(f.type); if (f.label) followupLabels.add(f.label); }
  }

  // ---------- interactions (client dispatches)
  const interByTool = new Counter(), widgetTypes = new Counter(), widgetTitles = new Counter(),
        openPageTypes = new Counter(), askQuestions = new Counter();
  let chartActions = 0, askUserTotal = 0;
  for (const t of turns) for (const it of (t.interactions || [])) {
    const tool = it.tool;
    interByTool.add(tool);
    if (tool === 'show_widget') {
      if (it.widget_type) widgetTypes.add(it.widget_type);
      if (it.widget_title) widgetTitles.add(it.widget_title);
    } else if (tool === 'open_page') {
      if (it.page_type) openPageTypes.add(it.page_type);
    } else if (tool === 'ask_user') {
      askUserTotal++;
      if (it.question) askQuestions.add(it.question);
    } else if (tool && tool.startsWith('chart_')) chartActions++;
  }

  // ---------- pages where Kyro is used (get_screen_context)
  const pageCounter = new Counter(), pageDesc = new Map(), pageSubtabs = new Map();
  let turnsWithScreen = 0;
  for (const t of turns) {
    const sc = t.screen;
    if (!sc) continue;
    turnsWithScreen++;
    pageCounter.add(sc.id);
    if (!pageDesc.has(sc.id) && sc.desc) pageDesc.set(sc.id, sc.desc);
    if (sc.sub_tab) {
      if (!pageSubtabs.has(sc.id)) pageSubtabs.set(sc.id, new Counter());
      pageSubtabs.get(sc.id).add(sc.sub_tab);
    }
  }

  // ---------- outcomes
  const outcomeCounter = new Counter(), stopCounter = new Counter();
  let cancelledTurns = 0, erroredTurns = 0;
  for (const t of turns) {
    outcomeCounter.add(t.outcome);
    stopCounter.add(t.stop_reason);
    if (t.cancelled) cancelledTurns++;
    if (t.outcome === 'errored') erroredTurns++;
  }

  // ---------- performance
  const latencies = turns.filter(t => t.latency_ms).map(t => t.latency_ms);
  const latencyHist = histogram(latencies.map(l => l / 1000), LAT_EDGES, LAT_LABELS);
  const latencySeries = turns.filter(t => t.latency_ms)
    .map(t => ({ ts_ms: t.ts_ms, latency_ms: t.latency_ms, tokens: t.tokens.input + t.tokens.output }));
  const reasoningTurns = turns.filter(t => t.reasoning_steps > 0).length;
  const avgReasoningSteps = turns.length ? r2(sum(turns.map(t => t.reasoning_steps)) / turns.length) : 0;

  // ---------- tokens
  const tokTotals = { input: 0, output: 0, cache_read: 0, cache_write: 0, reasoning: 0 };
  for (const t of turns) for (const k of Object.keys(tokTotals)) tokTotals[k] += t.tokens[k];
  tokTotals.total = tokTotals.input + tokTotals.output;
  // cache_read is a SUBSET of input -> hit rate = cache_read / input
  const cacheHitRatio = tokTotals.input ? r1(tokTotals.cache_read / tokTotals.input * 100) : 0;
  const perTurnTokens = turns.map(t => t.tokens.input + t.tokens.output);
  const tokenHist = histogram(perTurnTokens, [50000, 100000, 150000, 200000, 300000, 500000],
    ['<50k', '50-100k', '100-150k', '150-200k', '200-300k', '300-500k', '500k+']);

  // ---------- tools
  const toolCalls = [];
  for (const t of turns) for (const x of t.tools) toolCalls.push(x);
  const toolsInvoked = new Counter();
  for (const x of toolCalls) toolsInvoked.add(x.name);
  const perName = new Map();
  for (const x of toolCalls) {
    let P = perName.get(x.name);
    if (!P) { P = { calls: 0, errors: 0, latencies: [], inputs: [], outputs: [] }; perName.set(x.name, P); }
    P.calls++;
    if (x.is_error) P.errors++;
    if (x.latency_ms != null) P.latencies.push(x.latency_ms);
    if (x.input_size) P.inputs.push(x.input_size);
    if (x.output_size) P.outputs.push(x.output_size);
  }
  const toolTable = [];
  for (const [name, P] of perName) {
    const lat = [...P.latencies].sort((a, b) => a - b);
    toolTable.push({
      name, calls: P.calls, errors: P.errors,
      error_rate: P.calls ? r1(P.errors / P.calls * 100) : 0,
      lat_p50: lat.length ? Math.round(pct(lat, 0.5)) : 0,
      lat_p90: lat.length ? Math.round(pct(lat, 0.9)) : 0,
      lat_mean: lat.length ? Math.round(sum(lat) / lat.length) : 0,
      lat_max: lat.length ? lat[lat.length - 1] : 0,
      in_avg: P.inputs.length ? Math.round(sum(P.inputs) / P.inputs.length) : 0,
      // reduce(), not Math.max(...spread) — a tool with >~125k samples would blow the call stack
      in_max: P.inputs.reduce((a, b) => Math.max(a, b), 0),
      out_avg: P.outputs.length ? Math.round(sum(P.outputs) / P.outputs.length) : 0,
      out_max: P.outputs.reduce((a, b) => Math.max(a, b), 0),
      out_total: sum(P.outputs),
    });
  }
  toolTable.sort((a, b) => b.calls - a.calls);
  const toolsPerTurn = turns.map(t => t.tools.length);
  const toolsPerTurnHist = histogram(toolsPerTurn, [0, 1, 2, 4, 6, 10],
    ['0', '1', '2', '3-4', '5-6', '7-10', '10+']);

  const allLat = [...latencies].sort((a, b) => a - b);

  // ---------- estimated cost (cache_read is a subset of input; thinking billed as output)
  const freshInput = Math.max(0, tokTotals.input - tokTotals.cache_read);
  const cInput = freshInput / 1e6 * PRICING.input_per_m;
  const cCached = tokTotals.cache_read / 1e6 * PRICING.cached_input_per_m;
  const cOutput = (tokTotals.output + tokTotals.reasoning) / 1e6 * PRICING.output_per_m;
  const cTotal = cInput + cCached + cOutput;
  const cNocache = tokTotals.input / 1e6 * PRICING.input_per_m + cOutput;
  const nUsers = usersSeen.size || 1, nDays = sortedDays.length || 1, nTurns = turns.length || 1;
  const cost = {
    model: PRICING.model,
    rates: { input: PRICING.input_per_m, cached_input: PRICING.cached_input_per_m, output: PRICING.output_per_m },
    total: r4(cTotal), input: r4(cInput), cached: r4(cCached), output: r4(cOutput),
    per_turn: r5(cTotal / nTurns), per_day: r4(cTotal / nDays), per_user: r4(cTotal / nUsers),
    without_caching: r4(cNocache), cache_savings: r4(cNocache - cTotal),
    cache_savings_pct: cNocache ? r1((cNocache - cTotal) / cNocache * 100) : 0,
  };

  // ---------- quality (Tier A confirmed + Tier B review queue)
  const qRule = new Counter(), qRuleMeta = {}, qDimA = new Counter(), qDimB = new Counter();
  const sevCounts = new Counter(), perUserFlags = new Counter(), perDayFlags = new Counter();
  let flaggedA = 0, candB = 0;
  for (const t of turns) {
    const vs = t.violations || [];
    if (!vs.length) continue;
    const hasA = vs.some(x => x.tier === 'A'), hasB = vs.some(x => x.tier === 'B');
    if (hasA) flaggedA++;
    if (hasB) candB++;
    const day = t.ts_ms ? istParts(t.ts_ms).day : null;
    if (hasA) { perUserFlags.add(t.user_id); if (day) perDayFlags.add(day); }
    for (const x of vs) {
      qRule.add(x.rule);
      qRuleMeta[x.rule] = { dim: x.dim, severity: x.severity, tier: x.tier };
      if (x.tier === 'A') { qDimA.add(x.dim); sevCounts.add(x.severity); }
      else qDimB.add(x.dim);
    }
  }
  const qbUser = usersTable.map(ut => ({
    name: ut.name, user_id: ut.user_id, turns: ut.turns,
    flags: perUserFlags.get(ut.user_id) || 0,
    per100: ut.turns ? r1((perUserFlags.get(ut.user_id) || 0) / ut.turns * 100) : 0,
  })).sort((a, b) => (b.flags - a.flags) || (b.per100 - a.per100));
  const quality = {
    total_turns: turns.length,
    flagged_turns: flaggedA,
    clean_pct: turns.length ? r1((turns.length - flaggedA) / turns.length * 100) : 0,
    tierB_candidate_turns: candB,
    severity: { high: sevCounts.get('high') || 0, medium: sevCounts.get('medium') || 0, low: sevCounts.get('low') || 0 },
    by_dimension: qDimA.mostCommon().map(([dimension, count]) => ({ dimension, count })),
    by_dimension_tierB: qDimB.mostCommon().map(([dimension, count]) => ({ dimension, count })),
    by_rule: qRule.mostCommon().map(([rule, count]) => ({ rule, count, ...qRuleMeta[rule] })),
    by_user: qbUser,
    by_day: dailySeries.map(d => ({ date: d.date, flagged: perDayFlags.get(d.date) || 0, turns: d.turns })),
  };

  // ---------- engagement / retention / projection
  const MKT_OPEN = 9 * 60 + 15, MKT_CLOSE = 15 * 60 + 30;    // 09:15, 15:30 IST
  const slotCounts = { 'Pre-market': 0, 'During market': 0, 'Post-market': 0, 'Weekend': 0 };
  for (const t of turns) {
    if (!t.ts_ms) continue;
    const P = istParts(t.ts_ms);
    if (P.dow >= 5) slotCounts['Weekend']++;
    else slotCounts[P.minutes < MKT_OPEN ? 'Pre-market' : (P.minutes < MKT_CLOSE ? 'During market' : 'Post-market')]++;
  }
  const daySlots = ['Pre-market', 'During market', 'Post-market', 'Weekend']
    .map(slot => ({ slot, count: slotCounts[slot] }));

  // First-seen / weekly / monthly bookkeeping is computed over FULL history so
  // a narrowed date range doesn't relabel long-time users as "new", truncate
  // the trailing stickiness window, or reset the M1 cohorts. Only rows inside
  // the selected range (rangeWeeks / rangeMonths / sortedDays) are emitted.
  const userFirstDay = new Map(), userFirstWeek = new Map(), userFirstMonth = new Map();
  const userMonths = new Map(), weekUsers = new Map(), weekLabel = {};
  const histDayUsers = new Map();            // day -> Set(user) over full history
  for (const t of history) {
    if (!t.ts_ms) continue;
    const P = istParts(t.ts_ms), uid = t.user_id;
    const wkInfo = isoWeekInfo(P.shifted), wk = wkInfo.wk, mo = P.month, day = P.day;
    if (!userFirstDay.has(uid) || day < userFirstDay.get(uid)) userFirstDay.set(uid, day);
    if (!userFirstWeek.has(uid) || wk < userFirstWeek.get(uid)) userFirstWeek.set(uid, wk);
    if (!userFirstMonth.has(uid) || mo < userFirstMonth.get(uid)) userFirstMonth.set(uid, mo);
    if (!userMonths.has(uid)) userMonths.set(uid, new Set());
    userMonths.get(uid).add(mo);
    if (!weekUsers.has(wk)) weekUsers.set(wk, new Set());
    weekUsers.get(wk).add(uid);
    weekLabel[wk] = wkInfo.label;
    if (!histDayUsers.has(day)) histDayUsers.set(day, new Set());
    histDayUsers.get(day).add(uid);
  }
  const rangeWeeks = new Set(), rangeMonths = new Set();
  for (const t of turns) {
    if (!t.ts_ms) continue;
    const P = istParts(t.ts_ms);
    rangeWeeks.add(isoWeekInfo(P.shifted).wk);
    rangeMonths.add(P.month);
  }

  const dauSplit = sortedDays.map(day => {
    const uu = daily.get(day).users;
    let nw = 0;
    for (const u of uu) if (userFirstDay.get(u) === day) nw++;
    return { date: day, new: nw, existing: uu.size - nw, dau: uu.size };
  });

  // Weeks are rendered only if the selected range touches them, but each WAU
  // value counts the FULL week's actives — a range starting mid-week doesn't
  // understate that week's WAU or mark all of its users "new".
  const wauSplit = [...weekUsers.keys()].filter(wk => rangeWeeks.has(wk)).sort().map(wk => {
    const uu = weekUsers.get(wk);
    let nw = 0;
    for (const u of uu) if (userFirstWeek.get(u) === wk) nw++;
    return { week: wk, label: weekLabel[wk] || wk, new: nw, existing: uu.size - nw, wau: uu.size };
  });
  const peakWau = wauSplit.reduce((a, w) => Math.max(a, w.wau), 0);

  // DAU/WAU stickiness — daily actives ÷ trailing-7d actives. The trailing
  // window reads full-history day sets so the first days of a selected range
  // aren't computed against a truncated window (which pinned them to 100%).
  const histDays = [...histDayUsers.keys()].sort();
  const histEpoch = histDays.map(d => Date.parse(d + 'T00:00:00Z') / 86400000);
  const dayEpoch = sortedDays.map(d => Date.parse(d + 'T00:00:00Z') / 86400000);
  const stickiness = sortedDays.map((day, i) => {
    const win = new Set();
    for (let j = histDays.length - 1; j >= 0; j--) {
      const diff = dayEpoch[i] - histEpoch[j];
      if (diff < 0) continue;
      if (diff > 6) break;
      for (const u of histDayUsers.get(histDays[j])) win.add(u);
    }
    const dau = daily.get(day).users.size;
    return { date: day, stickiness: win.size ? r1(dau / win.size * 100) : 0 };
  });
  const stickinessAvg = stickiness.length ? r1(sum(stickiness.map(s => s.stickiness)) / stickiness.length) : 0;

  const newThreadRate = sortedDays.map(day => {
    const dau = daily.get(day).users.size, nt = threadsStartedByDay.get(day) || 0;
    return { date: day, rate: dau ? r2(nt / dau) : 0, threads: nt, dau };
  });
  const newThreadRateAvg = newThreadRate.length ? r2(sum(newThreadRate.map(x => x.rate)) / newThreadRate.length) : 0;

  const nDaysE = daily.size || 1;
  let totalUserActiveDays = 0;
  for (const v of userDays.values()) totalUserActiveDays += v.size;
  totalUserActiveDays = totalUserActiveDays || 1;
  const avgTurnsPerDay = r1(turns.length / nDaysE);
  const avgTurnsPerUserPerDay = r2(turns.length / totalUserActiveDays);

  // M1 monthly cohort retention — cohorts and next-month activity come from
  // full history (a cohort is "users first seen that month", ever; retention
  // may resolve in a month after the selected range). Only cohort months the
  // selected range touches are rendered.
  const nextMonth = mo => {
    const y = +mo.slice(0, 4), mm = +mo.slice(5, 7);
    return mm === 12 ? (y + 1) + '-01' : y + '-' + String(mm + 1).padStart(2, '0');
  };
  const monthsPresent = [...new Set([...userMonths.values()].flatMap(s => [...s]))].sort();
  const m1Cohorts = [...rangeMonths].sort().map(mo => {
    const cohort = [...userFirstMonth.entries()].filter(([, fm]) => fm === mo).map(([u]) => u);
    const nm = nextMonth(mo), hasNext = monthsPresent.includes(nm);
    const retained = hasNext ? cohort.filter(u => userMonths.get(u).has(nm)).length : null;
    return { cohort: mo, size: cohort.length, m1_retained: retained,
             m1_pct: (hasNext && cohort.length) ? r1(retained / cohort.length * 100) : null };
  });
  const elig = m1Cohorts.filter(c => c.m1_pct != null);
  const eligSize = sum(elig.map(c => c.size));
  const m1Overall = eligSize ? r1(sum(elig.map(c => c.m1_retained)) / eligSize * 100) : null;

  const ntTotal = turns.length || 1;
  const toolInvokedPct = r1(turns.filter(t => t.tools.length > 0).length / ntTotal * 100);
  const dataToolInvokedPct = r1(turns.filter(t => t.tools.some(x => !CLIENT_TOOLS.has(x.name))).length / ntTotal * 100);
  const skillInvokedPct = r1(turns.filter(t => (t.loaded_skills || []).length).length / ntTotal * 100);

  // latency excluding ask_user turns (removes human-reply wait)
  const usedAsk = t => t.tools.some(x => x.name === 'ask_user') || (t.interactions || []).some(it => it.tool === 'ask_user');
  const latNoAsk = turns.filter(t => t.latency_ms && !usedAsk(t)).map(t => t.latency_ms);
  const latencyNoAskStats = stats(latNoAsk);
  const latencyNoAskHist = histogram(latNoAsk.map(l => l / 1000), LAT_EDGES, LAT_LABELS);
  const askUserTurns = turns.filter(usedAsk).length;

  const tokensPerDay = tokTotals.total / nDaysE;
  // Per-user-per-active-day economics — the basis for scaling to a larger fleet.
  const tokPerUserDay = (nUsers && nDaysE) ? tokTotals.total / nUsers / nDaysE : 0;
  const costPerUserDay = (nUsers && nDaysE) ? cost.total / nUsers / nDaysE : 0;
  const projection = {
    basis_days: nDaysE,
    tokens_per_day: Math.round(tokensPerDay),
    cost_per_day: cost.per_day,
    proj_30d_tokens: Math.round(tokensPerDay * 30),
    proj_30d_cost: r2(cost.per_day * 30),
    proj_90d_tokens: Math.round(tokensPerDay * 90),
    proj_90d_cost: r2(cost.per_day * 90),
    // What the same per-user usage intensity would cost at a target fleet size.
    // Holds current per-user tokens/day constant and scales the user count only.
    scaled: {
      target_users: TARGET_USERS,
      current_users: nUsers,
      multiplier: nUsers ? r1(TARGET_USERS / nUsers) : 0,
      tokens_per_user_per_day: Math.round(tokPerUserDay),
      cost_per_user_per_month: r4(costPerUserDay * 30),
      tokens_per_day: Math.round(tokPerUserDay * TARGET_USERS),
      cost_per_day: r2(costPerUserDay * TARGET_USERS),
      proj_30d_tokens: Math.round(tokPerUserDay * TARGET_USERS * 30),
      proj_30d_cost: r2(costPerUserDay * TARGET_USERS * 30),
      proj_90d_tokens: Math.round(tokPerUserDay * TARGET_USERS * 90),
      proj_90d_cost: r2(costPerUserDay * TARGET_USERS * 90),
      proj_365d_cost: r2(costPerUserDay * TARGET_USERS * 365),
    },
  };

  // ---------- personas (per-persona rollup for the Personas tab)
  const pAgg = new Map();
  for (const t of turns) {
    const p = personaOf(t.user_id);
    let a = pAgg.get(p);
    if (!a) {
      a = { persona: p, users: new Set(), turns: 0, threads: new Set(), tokens: 0,
            tool_calls: 0, errors: 0, cancelled: 0, flagged: 0, negative: 0,
            latencies: [], intents: new Counter(), days: new Set() };
      pAgg.set(p, a);
    }
    a.users.add(t.user_id); a.turns++; a.threads.add(t.thread_id);
    a.tokens += t.tokens.input + t.tokens.output;
    a.tool_calls += t.tools.length;
    a.errors += t.tools.filter(x => x.is_error).length;
    if (t.cancelled) a.cancelled++;
    if ((t.violations || []).some(x => x.tier === 'A')) a.flagged++;
    if (t.negative_reason) a.negative++;
    if (t.latency_ms) a.latencies.push(t.latency_ms);
    a.intents.add(t.intent);
    if (t.ts_ms) a.days.add(istParts(t.ts_ms).day);
  }
  const personasTable = [...pAgg.values()].map(a => {
    const lat = [...a.latencies].sort((x, y) => x - y);
    return {
      persona: a.persona, users: a.users.size, turns: a.turns, threads: a.threads.size,
      tokens: a.tokens, tool_calls: a.tool_calls, errors: a.errors, cancelled: a.cancelled,
      flagged: a.flagged, flag_per100: a.turns ? r1(a.flagged / a.turns * 100) : 0,
      negative: a.negative,
      turns_per_user: a.users.size ? r1(a.turns / a.users.size) : 0,
      tokens_per_user: a.users.size ? Math.round(a.tokens / a.users.size) : 0,
      avg_latency_ms: lat.length ? Math.round(sum(lat) / lat.length) : 0,
      p90_latency_ms: lat.length ? Math.round(pct(lat, 0.9)) : 0,
      active_days: a.days.size,
      top_intent: a.intents.size ? a.intents.mostCommon(1)[0][0] : '-',
      intents: a.intents.mostCommon().map(([intent, count]) => ({ intent, count })),
    };
  }).sort((x, y) => y.turns - x.turns);
  const classifiedUsers = [...usersSeen].filter(u => personaOf(u) !== 'unclassified').length;

  // ---------- assemble (same shape build_data() used to emit)
  const staticMeta = raw.meta || {};
  return {
    meta: {
      ...staticMeta,
      users_with_data: usersSeen.size,
      filtered_turns: turns.length,
      filtered_threads: threads.size,
      date_min: sortedDays[0] || null,
      date_max: sortedDays[sortedDays.length - 1] || null,
      n_days: sortedDays.length,
    },
    kpis: {
      conversations: threads.size,
      turns: turns.length,
      users: usersSeen.size,
      avg_dau: dailySeries.length ? r1(sum(dailySeries.map(d => d.dau)) / dailySeries.length) : 0,
      peak_dau: dailySeries.reduce((a, d) => Math.max(a, d.dau), 0),
      total_tokens: tokTotals.total,
      total_tool_calls: toolCalls.length,
      client_interactions: sum([...interByTool.values()]),
      errored_turns: erroredTurns,
      cancelled_turns: cancelledTurns,
      avg_turns_per_thread: threads.size ? r2(turns.length / threads.size) : 0,
      median_latency_ms: allLat.length ? Math.round(pct(allLat, 0.5)) : 0,
      likes: null,
      dislikes: null,
    },
    activity: {
      daily: dailySeries,
      hourly: Array.from({ length: 24 }, (_, h) => ({ hour: h, count: hourly.get(h) || 0 })),
      dow: DOW_NAMES.map((day, i) => ({ day, count: dow.get(i) || 0 })),
    },
    users: usersTable,
    queries: {
      top: topQueries,
      wordcloud,
      intents: intentCounter.mostCommon().map(([intent, count]) => ({ intent, count })),
      length_hist: histogram(queryLengths, [2, 5, 10, 20, 40], ['1-2', '3-5', '6-10', '11-20', '21-40', '40+']),
      typed_vs_screen: { typed: typedQueries.length, screen_context: screenContext },
      negative: negativeRecords.sort((a, b) => a.ts_ms - b.ts_ms),
      all: allQueryRecords,
      total_typed: typedQueries.length,
    },
    conversations: {
      depth_hist: depthHist,
      threads_table: threadsTable,
      avg_turns_per_thread: turnsPerThread.length ? r2(sum(turnsPerThread) / turnsPerThread.length) : 0,
      max_depth: turnsPerThread.reduce((a, b) => Math.max(a, b), 0),
      single_turn_threads: turnsPerThread.filter(x => x === 1).length,
      followups: {
        total_offered: followupTotal,
        turns_with_followups: turnsWithFollowups,
        pct_turns: turns.length ? r1(turnsWithFollowups / turns.length * 100) : 0,
        by_type: followupTypes.mostCommon().map(([type, count]) => ({ type, count })),
        top_labels: followupLabels.mostCommon(15).map(([label, count]) => ({ label, count })),
      },
    },
    interactions: {
      by_tool: interByTool.mostCommon().map(([tool, count]) => ({ tool, count })),
      widgets: widgetTypes.mostCommon().map(([type, count]) => ({ type, count })),
      widget_titles: widgetTitles.mostCommon(20).map(([title, count]) => ({ title, count })),
      open_pages: openPageTypes.mostCommon(15).map(([type, count]) => ({ type, count })),
      ask_user_total: askUserTotal,
      top_questions: askQuestions.mostCommon(12).map(([q, count]) => ({ q, count })),
      chart_actions: chartActions,
      pages: pageCounter.mostCommon().map(([id, count]) => ({
        id, count, desc: pageDesc.get(id) || '',
        sub_tabs: pageSubtabs.has(id) ? pageSubtabs.get(id).mostCommon().map(([tab, c]) => ({ tab, count: c })) : [],
      })),
      pages_coverage: turnsWithScreen,
    },
    outcomes: {
      by_outcome: outcomeCounter.mostCommon().map(([outcome, count]) => ({ outcome, count })),
      by_stop_reason: stopCounter.mostCommon().map(([reason, count]) => ({ reason: reason == null || reason === '' ? 'null' : reason, count })),
      cancelled: cancelledTurns,
      errored: erroredTurns,
    },
    performance: {
      latency_stats: stats(latencies),
      latency_hist: latencyHist,
      latency_series: latencySeries,
      reasoning_turns: reasoningTurns,
      avg_reasoning_steps: avgReasoningSteps,
      latency_no_ask_stats: latencyNoAskStats,
      latency_no_ask_hist: latencyNoAskHist,
      no_ask_turns: latNoAsk.length,
      ask_user_turns: askUserTurns,
    },
    tokens: {
      totals: tokTotals,
      per_day: dailySeries.map(d => ({ date: d.date, input: d.input, output: d.output,
                                       cache_read: d.cache_read, reasoning: d.reasoning })),
      per_turn_hist: tokenHist,
      per_turn_stats: stats(perTurnTokens),
      cache_hit_ratio: cacheHitRatio,
      cost,
      projection,
    },
    tools: {
      invoked: toolsInvoked.mostCommon(30).map(([name, count]) => ({ name, count })),
      table: toolTable,
      per_turn_hist: toolsPerTurnHist,
      per_turn_stats: stats(toolsPerTurn),
      skills_available: staticMeta.skills_available || [],
      skills_constant: staticMeta.skills_constant !== false,
      total_unique_tools: perName.size,
    },
    quality,
    engagement: {
      day_slots: daySlots,
      dau_split: dauSplit,
      wau_split: wauSplit,
      peak_wau: peakWau,
      stickiness,
      stickiness_avg: stickinessAvg,
      new_thread_rate: newThreadRate,
      new_thread_rate_avg: newThreadRateAvg,
      avg_turns_per_day: avgTurnsPerDay,
      avg_turns_per_user_per_day: avgTurnsPerUserPerDay,
      tool_invoked_pct: toolInvokedPct,
      data_tool_invoked_pct: dataToolInvokedPct,
      skill_invoked_pct: skillInvokedPct,
      m1: { by_cohort: m1Cohorts, overall_pct: m1Overall, eligible_cohorts: elig.length },
    },
    personas: {
      table: personasTable,
      classified_users: classifiedUsers,
      total_users: usersSeen.size,
    },
  };
}

window.KyroCalc = { buildData, istParts };
})();
