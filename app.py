import os
import json
import time
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, request, render_template_string

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GAMMA_BASE      = "https://gamma-api.polymarket.com"
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH         = os.environ.get("DB_PATH", "/tmp/board.db")
CACHE_TTL_MIN   = int(os.environ.get("CACHE_TTL_MIN", "10"))
PORT            = int(os.environ.get("PORT", "5000"))

SPORTS = [
    {"key": "soccer",     "tag": "soccer",     "label": "Soccer",     "emoji": "⚽"},
    {"key": "basketball", "tag": "basketball",  "label": "Basketball", "emoji": "🏀"},
    {"key": "baseball",   "tag": "baseball",    "label": "MLB",        "emoji": "⚾"},
    {"key": "nhl",        "tag": "nhl",         "label": "NHL",        "emoji": "🏒"},
    {"key": "tennis",     "tag": "tennis",      "label": "Tennis",     "emoji": "🎾"},
    {"key": "mma",        "tag": "mma",         "label": "MMA",        "emoji": "🥊"},
]

SKIP_KEYWORDS = [
    "exact score", "halftime", "corners", "player prop",
    "more markets", "bitcoin", "ethereum", "dogecoin",
    "up or down", "solana", "hyperliquid",
]

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS market_cache (
            sport TEXT NOT NULL,
            data  TEXT NOT NULL,
            ts    INTEGER NOT NULL,
            PRIMARY KEY (sport)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bookmaker_cache (
            slug  TEXT NOT NULL,
            data  TEXT NOT NULL,
            ts    INTEGER NOT NULL,
            PRIMARY KEY (slug)
        )
    """)
    con.commit()
    con.close()

def cache_get(table, key_col, key_val):
    ttl_sec = CACHE_TTL_MIN * 60
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        f"SELECT data, ts FROM {table} WHERE {key_col} = ?", (key_val,)
    ).fetchone()
    con.close()
    if row and (time.time() - row[1]) < ttl_sec:
        return json.loads(row[0])
    return None

def cache_set(table, key_col, key_val, data):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({key_col}, data, ts) VALUES (?, ?, ?)",
        (key_val, json.dumps(data, ensure_ascii=False), int(time.time()))
    )
    con.commit()
    con.close()

# ── Polymarket fetch ──────────────────────────────────────────────────────────

def fetch_events_raw(tag, limit=100):
    url = f"{GAMMA_BASE}/events"
    params = {
        "tag_slug": tag,
        "limit": limit,
        "order": "startDate",
        "ascending": "false",
        "active": "true",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def parse_event(e):
    title = e.get("title", "")
    tl = title.lower()
    if any(k in tl for k in SKIP_KEYWORDS):
        return None

    markets = e.get("markets") or []
    ml = [m for m in markets if m.get("sportsMarketType") == "moneyline"]

    home = draw = away = None

    def yes_price(m):
        raw = m.get("outcomePrices") or "[]"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return 0.0
        if isinstance(raw, list) and raw:
            return float(raw[0])
        return 0.0

    if e.get("negRisk") and len(ml) == 3:
        for m in ml:
            q = (m.get("question") or "").lower()
            p = yes_price(m)
            vol = float(m.get("volume") or 0)
            if "draw" in q or "tie" in q:
                draw = {"price": p, "vol": vol}
            elif home is None:
                home = {"price": p, "vol": vol}
            else:
                away = {"price": p, "vol": vol}
    elif ml:
        m = ml[0]
        p = yes_price(m)
        home = {"price": p, "vol": float(m.get("volume") or 0)}

    if home is None:
        return None

    total_vol = sum(float(m.get("volume") or 0) for m in markets)
    liq = float(e.get("liquidityClob") or e.get("liquidity") or 0)

    return {
        "id":        str(e.get("id", "")),
        "title":     title,
        "slug":      e.get("slug", ""),
        "startDate": e.get("startDate") or e.get("eventDate") or "",
        "liquidity": round(liq, 2),
        "volume":    round(total_vol, 2),
        "is3way":    bool(home and draw and away),
        "home":      home,
        "draw":      draw,
        "away":      away,
    }

def get_markets(sport_tag):
    cached = cache_get("market_cache", "sport", sport_tag)
    if cached:
        return cached

    try:
        raw = fetch_events_raw(sport_tag)
    except Exception as ex:
        log.error(f"fetch error {sport_tag}: {ex}")
        return {"error": str(ex), "events": []}

    events = []
    for e in raw:
        parsed = parse_event(e)
        if parsed:
            events.append(parsed)

    events.sort(key=lambda x: x["liquidity"], reverse=True)
    result = {"events": events, "fetched_at": datetime.now(timezone.utc).isoformat()}
    cache_set("market_cache", "sport", sport_tag, result)
    return result

# ── Bookmaker odds via Claude API ─────────────────────────────────────────────

def fetch_bookmaker_odds(match_title, slug):
    cached = cache_get("bookmaker_cache", "slug", slug)
    if cached:
        return cached

    if not ANTHROPIC_KEY:
        return {"error": "ANTHROPIC_API_KEY not set"}

    system = (
        "Sports odds assistant. Search for current bookmaker odds and return ONLY a JSON object. "
        "No markdown, no explanation, no code fences.\n\n"
        "Return format: {\"home\": 0.45, \"draw\": 0.28, \"away\": 0.27, \"source\": \"Pinnacle\"}\n\n"
        "Rules:\n"
        "- All values are IMPLIED PROBABILITY (0.0–1.0)\n"
        "- Decimal odds (e.g. 2.20) → 1/2.20 = 0.455\n"
        "- American odds (+320) → 100/(320+100) = 0.238\n"
        "- If no draw (NHL, NBA, tennis, MMA): omit draw field entirely\n"
        "- source = bookmaker name you found (Pinnacle preferred)\n"
        "- If not found: {\"error\": \"not found\"}\n"
        "- ONLY valid JSON, nothing else"
    )

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "system": system,
        "messages": [{
            "role": "user",
            "content": f'Find current bookmaker odds for: "{match_title}". Search Pinnacle or Bet365. Return JSON only.'
        }]
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
    }

    try:
        r = requests.post(ANTHROPIC_URL, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()

        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        full_text = " ".join(text_blocks)

        import re
        m = re.search(r'\{[^{}]+\}', full_text)
        if m:
            result = json.loads(m.group())
            cache_set("bookmaker_cache", "slug", slug, result)
            return result

        return {"error": "parse failed", "raw": full_text[:200]}
    except Exception as ex:
        log.error(f"bookmaker fetch error: {ex}")
        return {"error": str(ex)}

# ── Flask routes ──────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})

@app.route("/api/markets")
def api_markets():
    tag = request.args.get("sport", "soccer")
    valid_tags = {s["tag"] for s in SPORTS}
    if tag not in valid_tags:
        return jsonify({"error": "unknown sport"}), 400
    return jsonify(get_markets(tag))

@app.route("/api/bookmaker", methods=["POST"])
def api_bookmaker():
    body = request.get_json(silent=True) or {}
    title = body.get("title", "")
    slug  = body.get("slug", "")
    if not title or not slug:
        return jsonify({"error": "title and slug required"}), 400
    result = fetch_bookmaker_odds(title, slug)
    return jsonify(result)

@app.route("/")
def index():
    sports_json = json.dumps(SPORTS)
    return render_template_string(HTML_TEMPLATE, sports_json=sports_json)

# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Sports Board</title>
<style>
:root {
  --bg: #0d0f12;
  --bg2: #151820;
  --bg3: #1c2028;
  --border: #2a2d35;
  --text: #e2e4e9;
  --muted: #6b7280;
  --dim: #3d4148;
  --blue: #3b82f6;
  --blue-dim: #1e3a5f;
  --green: #22c55e;
  --green-dim: #14321e;
  --red: #ef4444;
  --red-dim: #3b1212;
  --amber: #f59e0b;
  --font-mono: 'SF Mono', 'Fira Code', 'Consolas', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 13px;
  min-height: 100vh;
}

header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}
.header-left { display: flex; align-items: center; gap: 12px; }
.logo { font-size: 16px; font-weight: 700; letter-spacing: -0.02em; color: var(--text); }
.logo span { color: var(--blue); }
.badge { font-size: 10px; background: var(--blue-dim); color: var(--blue);
  padding: 2px 7px; border-radius: 3px; letter-spacing: 0.05em; }
.header-right { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 11px; }
#last-updated { color: var(--muted); }
#refresh-btn {
  background: var(--bg3); border: 1px solid var(--border); color: var(--text);
  padding: 5px 12px; border-radius: 4px; cursor: pointer; font-family: var(--font-mono);
  font-size: 11px; display: flex; align-items: center; gap: 5px;
}
#refresh-btn:hover { background: var(--border); }
#refresh-btn.spinning svg { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  flex-wrap: wrap;
}
.tabs { display: flex; gap: 4px; }
.tab {
  font-family: var(--font-mono); font-size: 11px; padding: 5px 10px;
  border-radius: 4px; border: 1px solid var(--border); background: transparent;
  color: var(--muted); cursor: pointer; letter-spacing: 0.02em; white-space: nowrap;
}
.tab:hover { background: var(--bg3); color: var(--text); }
.tab.active { background: var(--blue); border-color: var(--blue); color: #fff; }

.sort-wrap { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.sort-wrap label { color: var(--muted); font-size: 11px; }
select {
  font-family: var(--font-mono); font-size: 11px; padding: 5px 8px;
  border-radius: 4px; border: 1px solid var(--border); background: var(--bg3);
  color: var(--text); cursor: pointer;
}

.stats-bar {
  display: flex; gap: 0;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.stat {
  flex: 1; padding: 10px 20px;
  border-right: 1px solid var(--border);
}
.stat:last-child { border-right: none; }
.stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.07em; margin-bottom: 4px; }
.stat-val { font-size: 18px; font-weight: 700; color: var(--text); }

.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
thead th {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--muted); font-weight: 500; padding: 8px 14px;
  border-bottom: 1px solid var(--border); background: var(--bg2);
  text-align: left; white-space: nowrap; position: sticky; top: 0; z-index: 1;
}
th.r, td.r { text-align: right; }
th.c, td.c { text-align: center; }
tbody tr { border-bottom: 1px solid var(--border); transition: background 0.1s; }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: var(--bg2); }
td { padding: 9px 14px; vertical-align: middle; }

.match-name { font-weight: 600; font-size: 12px; color: var(--text); margin-bottom: 2px;
  max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.match-meta { font-size: 10px; color: var(--muted); display: flex; gap: 8px; }
.countdown { color: var(--amber); }

.odds-cell { display: flex; gap: 6px; align-items: center; font-size: 12px; }
.odds-home { color: #60a5fa; font-weight: 600; }
.odds-draw { color: var(--muted); }
.odds-away { color: #f87171; font-weight: 600; }
.odds-sep  { color: var(--dim); }

.bk-cell { font-size: 11px; }
.fetch-btn {
  font-family: var(--font-mono); font-size: 10px; padding: 3px 8px;
  border: 1px solid var(--border); background: transparent; color: var(--muted);
  border-radius: 3px; cursor: pointer; white-space: nowrap;
}
.fetch-btn:hover { background: var(--bg3); color: var(--text); }
.fetch-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.diff-badge {
  display: inline-block; font-size: 10px; padding: 1px 6px;
  border-radius: 3px; font-weight: 600; letter-spacing: 0.02em;
}
.diff-pos { background: var(--green-dim); color: var(--green); }
.diff-neg { background: var(--red-dim); color: var(--red); }
.diff-neu { background: var(--bg3); color: var(--muted); }

.vol-val { color: var(--muted); }
.liq-wrap { display: flex; align-items: center; gap: 6px; justify-content: flex-end; }
.liq-bar { height: 3px; border-radius: 2px; background: var(--blue); }

.link-btn {
  color: var(--blue); text-decoration: none; font-size: 14px;
  opacity: 0.7; display: block; text-align: center;
}
.link-btn:hover { opacity: 1; }

.status-row td { text-align: center; color: var(--muted); padding: 40px; }
.err-text { color: var(--red); }

.bk-source { font-size: 9px; color: var(--dim); margin-top: 1px; }
.bk-odds-line { display: flex; gap: 4px; }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <div class="logo">PM<span>Board</span></div>
    <div class="badge">LIVE</div>
  </div>
  <div class="header-right">
    <span>Updated: <span id="last-updated">—</span></span>
    <button id="refresh-btn" onclick="loadData()">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
      </svg>
      Refresh
    </button>
  </div>
</header>

<div class="toolbar">
  <div class="tabs" id="tabs"></div>
  <div class="sort-wrap">
    <label>Sort</label>
    <select id="sort-select" onchange="renderTable()">
      <option value="liquidity">Liquidity ↓</option>
      <option value="volume">Volume ↓</option>
      <option value="time">Time ↑</option>
      <option value="diff">Diff ↓</option>
    </select>
  </div>
</div>

<div class="stats-bar">
  <div class="stat"><div class="stat-label">Markets</div><div class="stat-val" id="s-count">—</div></div>
  <div class="stat"><div class="stat-label">Total Liquidity</div><div class="stat-val" id="s-liq">—</div></div>
  <div class="stat"><div class="stat-label">Total Volume</div><div class="stat-val" id="s-vol">—</div></div>
  <div class="stat"><div class="stat-label">3-Way Markets</div><div class="stat-val" id="s-3way">—</div></div>
</div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th style="width:28%">Match</th>
        <th style="width:18%">Polymarket</th>
        <th style="width:18%" class="r">Bookmaker</th>
        <th style="width:9%" class="c">Diff (H)</th>
        <th style="width:10%" class="r">Volume</th>
        <th style="width:11%" class="r">Liquidity</th>
        <th style="width:6%" class="c">Link</th>
      </tr>
    </thead>
    <tbody id="tbody">
      <tr class="status-row"><td colspan="7">Loading markets…</td></tr>
    </tbody>
  </table>
</div>

<script>
const SPORTS = {{ sports_json|safe }};
const PM_BASE = 'https://polymarket.com/event';

let currentTag = 'soccer';
let allEvents = [];
let bkCache = {};
let maxLiq = 1;

function fmt$(v) {
  if (!v || isNaN(v)) return '—';
  if (v >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
  if (v >= 1e3) return '$' + Math.round(v/1e3) + 'k';
  return '$' + Math.round(v);
}

function fmtCents(v) {
  if (v == null || isNaN(v)) return '—';
  return Math.round(v * 100) + '¢';
}

function fmtTime(s) {
  if (!s) return '—';
  const d = new Date(s), now = new Date();
  const h = Math.round((d - now) / 36e5);
  if (isNaN(h)) return '—';
  if (h < 0) return 'started';
  if (h < 24) return 'T-' + h + 'h';
  return 'T-' + Math.floor(h/24) + 'd';
}

function fmtDate(s) {
  if (!s) return '';
  return new Date(s).toISOString().slice(5,16).replace('T',' ') + 'Z';
}

function diffBadge(pm, bk) {
  if (pm == null || bk == null) return '<span class="diff-badge diff-neu">—</span>';
  const d = Math.round((pm - bk) * 100);
  if (Math.abs(d) < 1) return '<span class="diff-badge diff-neu">±0</span>';
  const cls = d > 0 ? 'diff-pos' : 'diff-neg';
  return `<span class="diff-badge ${cls}">${d > 0 ? '+' : ''}${d}¢</span>`;
}

function bkCell(ev) {
  const c = bkCache[ev.slug];
  if (!c) {
    return `<button class="fetch-btn" id="btn-${ev.slug}" onclick="fetchBk('${ev.slug}','${ev.title.replace(/'/g,"\\'")}')">⟳ fetch odds</button>`;
  }
  if (c.error) return `<span style="color:#ef4444;font-size:10px">${c.error}</span>`;
  const parts = [];
  if (c.home != null) parts.push(`<span class="odds-home">${fmtCents(c.home)}</span>`);
  if (c.draw != null) parts.push(`<span class="odds-draw">${fmtCents(c.draw)}</span>`);
  if (c.away != null) parts.push(`<span class="odds-away">${fmtCents(c.away)}</span>`);
  return `<div class="bk-odds-line">${parts.join('<span class="odds-sep">/</span>')}</div>
    <div class="bk-source">${c.source || ''}</div>`;
}

function diffCell(ev) {
  const c = bkCache[ev.slug];
  if (!c || c.error) return '<span class="diff-badge diff-neu">—</span>';
  const pmHome = ev.home ? ev.home.price : null;
  return diffBadge(pmHome, c.home);
}

function sortEvents(arr) {
  const by = document.getElementById('sort-select').value;
  const sorted = [...arr];
  if (by === 'liquidity') sorted.sort((a,b) => b.liquidity - a.liquidity);
  else if (by === 'volume') sorted.sort((a,b) => b.volume - a.volume);
  else if (by === 'time') sorted.sort((a,b) => new Date(a.startDate) - new Date(b.startDate));
  else if (by === 'diff') {
    sorted.sort((a,b) => {
      const ca = bkCache[a.slug], cb = bkCache[b.slug];
      const da = ca && !ca.error && a.home ? Math.abs(a.home.price - (ca.home||0)) : -1;
      const db = cb && !cb.error && b.home ? Math.abs(b.home.price - (cb.home||0)) : -1;
      return db - da;
    });
  }
  return sorted;
}

function renderTable() {
  const tbody = document.getElementById('tbody');
  if (!allEvents.length) return;
  maxLiq = Math.max(...allEvents.map(e => e.liquidity), 1);
  const sorted = sortEvents(allEvents);

  tbody.innerHTML = sorted.slice(0, 80).map(ev => {
    const barW = Math.max(2, Math.round(ev.liquidity / maxLiq * 70));
    const pmOdds = ev.is3way
      ? `<span class="odds-home">${fmtCents(ev.home?.price)}</span><span class="odds-sep">/</span>`+
        `<span class="odds-draw">${fmtCents(ev.draw?.price)}</span><span class="odds-sep">/</span>`+
        `<span class="odds-away">${fmtCents(ev.away?.price)}</span>`
      : ev.home
        ? `<span class="odds-home">${fmtCents(ev.home?.price)}</span>`
        : '—';

    const shortTitle = ev.title.length > 46 ? ev.title.slice(0,43) + '…' : ev.title;

    return `<tr>
      <td>
        <div class="match-name" title="${ev.title}">${shortTitle}</div>
        <div class="match-meta">
          <span>${fmtDate(ev.startDate)}</span>
          <span class="countdown">${fmtTime(ev.startDate)}</span>
          ${ev.is3way ? '<span style="color:#6b7280">3-way</span>' : ''}
        </div>
      </td>
      <td><div class="odds-cell">${pmOdds}</div></td>
      <td class="r">${bkCell(ev)}</td>
      <td class="c">${diffCell(ev)}</td>
      <td class="r"><span class="vol-val">${fmt$(ev.volume)}</span></td>
      <td class="r">
        <div class="liq-wrap">
          <span class="vol-val">${fmt$(ev.liquidity)}</span>
          <div class="liq-bar" style="width:${barW}px"></div>
        </div>
      </td>
      <td class="c">
        <a class="link-btn" href="${PM_BASE}/${ev.slug}" target="_blank" title="Open on Polymarket">↗</a>
      </td>
    </tr>`;
  }).join('');
}

function updateStats() {
  const totalLiq = allEvents.reduce((s,e) => s + e.liquidity, 0);
  const totalVol = allEvents.reduce((s,e) => s + e.volume, 0);
  const way3 = allEvents.filter(e => e.is3way).length;
  document.getElementById('s-count').textContent = allEvents.length;
  document.getElementById('s-liq').textContent = fmt$(totalLiq);
  document.getElementById('s-vol').textContent = fmt$(totalVol);
  document.getElementById('s-3way').textContent = way3;
  document.getElementById('last-updated').textContent = new Date().toISOString().slice(11,19) + ' UTC';
}

async function loadData() {
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('spinning');
  document.getElementById('tbody').innerHTML = '<tr class="status-row"><td colspan="7">Fetching from Polymarket…</td></tr>';

  try {
    const r = await fetch(`/api/markets?sport=${currentTag}`);
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    allEvents = data.events || [];
    updateStats();
    renderTable();
  } catch(err) {
    document.getElementById('tbody').innerHTML =
      `<tr class="status-row"><td colspan="7" class="err-text">Error: ${err.message}</td></tr>`;
  }
  btn.classList.remove('spinning');
}

async function fetchBk(slug, title) {
  const btn = document.getElementById('btn-' + slug);
  if (btn) { btn.disabled = true; btn.textContent = '…'; }

  try {
    const r = await fetch('/api/bookmaker', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, title })
    });
    bkCache[slug] = await r.json();
  } catch(e) {
    bkCache[slug] = { error: 'network error' };
  }
  renderTable();
}

function initTabs() {
  const container = document.getElementById('tabs');
  SPORTS.forEach((s, i) => {
    const btn = document.createElement('button');
    btn.className = 'tab' + (i === 0 ? ' active' : '');
    btn.textContent = s.emoji + ' ' + s.label;
    btn.onclick = () => {
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentTag = s.tag;
      bkCache = {};
      loadData();
    };
    container.appendChild(btn);
  });
}

initTabs();
loadData();
</script>
</body>
</html>
"""

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, debug=False)

init_db()
