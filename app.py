import os
import json
import re
import time
import logging
import sqlite3
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, render_template_string

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GAMMA_BASE   = "https://gamma-api.polymarket.com"
PINN_BASE    = "https://guest.api.arcadia.pinnacle.com/0.1"
PINN_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

DB_PATH          = os.environ.get("DB_PATH", "/tmp/board.db")
MARKET_TTL_MIN   = int(os.environ.get("MARKET_TTL_MIN", "10"))
PINNACLE_TTL_MIN = int(os.environ.get("PINNACLE_TTL_MIN", "15"))
PORT             = int(os.environ.get("PORT", "5000"))

SPORTS = [
    {"key": "soccer",     "tag": "soccer",    "label": "Soccer",     "emoji": "⚽", "pinn_sport": 29},
    {"key": "basketball", "tag": "basketball", "label": "Basketball", "emoji": "🏀", "pinn_sport": 4},
    {"key": "baseball",   "tag": "baseball",   "label": "MLB",        "emoji": "⚾", "pinn_sport": 3},
    {"key": "nhl",        "tag": "nhl",        "label": "NHL",        "emoji": "🏒", "pinn_sport": 19},
    {"key": "tennis",     "tag": "tennis",     "label": "Tennis",     "emoji": "🎾", "pinn_sport": 33},
    {"key": "mma",        "tag": "mma",        "label": "MMA",        "emoji": "🥊", "pinn_sport": 22},
]

SKIP_KEYWORDS = [
    "exact score", "halftime", "corners", "player prop",
    "more markets", "bitcoin", "ethereum", "dogecoin",
    "up or down", "solana", "hyperliquid",
]

# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT NOT NULL, ts INTEGER NOT NULL)")
    con.commit()
    con.close()

def cache_get(key, ttl_min):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT data, ts FROM cache WHERE key=?", (key,)).fetchone()
    con.close()
    if row and (time.time() - row[1]) < ttl_min * 60:
        return json.loads(row[0])
    return None

def cache_set(key, data):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO cache (key, data, ts) VALUES (?,?,?)",
                (key, json.dumps(data, ensure_ascii=False), int(time.time())))
    con.commit()
    con.close()

# ── Pinnacle ──────────────────────────────────────────────────────────────────

def american_to_prob(v):
    try:
        n = float(v)
        if n > 0:
            return round(100 / (n + 100), 4)
        else:
            return round(abs(n) / (abs(n) + 100), 4)
    except Exception:
        return None

def fetch_json(url):
    r = requests.get(url, headers=PINN_HEADERS, timeout=12)
    r.raise_for_status()
    return r.json()

def build_pinnacle_index(sport_id):
    key = f"pinn_index_{sport_id}"
    cached = cache_get(key, PINNACLE_TTL_MIN)
    if cached:
        return cached

    try:
        leagues = fetch_json(f"{PINN_BASE}/sports/{sport_id}/leagues?all=false&brandId=0")
    except Exception as ex:
        log.error(f"Pinnacle leagues error: {ex}")
        return {}

    index = {}

    for league in leagues:
        lid = league.get("id")
        if not lid or league.get("matchupCount", 0) == 0:
            continue

        lkey = f"pinn_league_{lid}"
        league_data = cache_get(lkey, PINNACLE_TTL_MIN)
        if league_data is None:
            try:
                matchups = fetch_json(f"{PINN_BASE}/leagues/{lid}/matchups")
                markets  = fetch_json(f"{PINN_BASE}/leagues/{lid}/markets/straight")
                if not isinstance(matchups, list):
                    matchups = []
                if not isinstance(markets, list):
                    markets = []
            except Exception as ex:
                log.warning(f"League {lid} error: {ex}")
                continue

            mk_index = {}
            for m in markets:
                if m.get("period") == 0 and m.get("type") == "moneyline":
                    mid = m.get("matchupId")
                    if mid and mid not in mk_index:
                        mk_index[mid] = m

            league_data = {}
            for mu in matchups:
                if mu.get("type") != "matchup":
                    continue
                parts = mu.get("participants", [])
                if len(parts) < 2:
                    continue
                home_o = next((p for p in parts if p.get("alignment") == "home"), parts[0])
                away_o = next((p for p in parts if p.get("alignment") == "away"), parts[-1])
                home = home_o.get("name", "")
                away = away_o.get("name", "")
                if not home or not away:
                    continue

                mk = mk_index.get(mu["id"])
                if not mk:
                    continue

                prices = mk.get("prices", [])
                h = d = a = None
                if len(prices) == 3:
                    desig = {p.get("designation", ""): p["price"] for p in prices}
                    if "home" in desig:
                        h = american_to_prob(desig["home"])
                        d = american_to_prob(desig.get("draw"))
                        a = american_to_prob(desig["away"])
                    else:
                        h = american_to_prob(prices[0]["price"])
                        d = american_to_prob(prices[2]["price"])
                        a = american_to_prob(prices[1]["price"])
                elif len(prices) == 2:
                    h = american_to_prob(prices[0]["price"])
                    a = american_to_prob(prices[1]["price"])

                if h is None:
                    continue

                start = mu.get("startTime") or ""
                league_data[str(mu["id"])] = {"home": home, "away": away, "start": start, "h": h, "d": d, "a": a}

            cache_set(lkey, league_data)

        for mid, match in league_data.items():
            k1 = norm(match["home"]) + "|" + norm(match["away"])
            k2 = norm(match["away"]) + "|" + norm(match["home"])
            index[k1] = match
            index[k2] = {"home": match["away"], "away": match["home"],
                         "h": match["a"], "d": match["d"], "a": match["h"], "start": match["start"]}

    log.info(f"Pinnacle index sport {sport_id}: {len(index)//2} matches")
    cache_set(key, index)
    return index

PREFIXES = ['ca ', 'fk ', 'fc ', 'ac ', 'as ', 'sc ', 'cd ', 'cf ', 'rc ',
            'ud ', 'sd ', 'ad ', 'sk ', 'bk ', 'nk ', 'gd ', 'sl ', 'ss ',
            'us ', 'sv ', 'vfb ', 'vfl ', 'rb ', 'rcd ', 'tsv ', 'afc ']

def norm(name):
    s = name.lower()
    s = re.sub(r'[^\w\s]', '', s)
    for p in PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    return s.strip()

def word_overlap(a, b):
    wa = set(w for w in a.split() if len(w) > 2)
    wb = set(w for w in b.split() if len(w) > 2)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))

def find_pinnacle(title, index):
    sep = ' vs. ' if ' vs. ' in title else ' vs '
    parts = title.split(sep, 1)
    if len(parts) != 2:
        return None
    ph = norm(parts[0])
    pa = norm(parts[1])

    exact = index.get(ph + "|" + pa)
    if exact:
        return exact

    best, best_score = None, 0.0
    for match in index.values():
        score = (word_overlap(ph, norm(match["home"])) + word_overlap(pa, norm(match["away"]))) / 2
        if score > best_score:
            best_score = score
            best = match
    return best if best_score >= 0.5 else None

# ── Polymarket ────────────────────────────────────────────────────────────────

def yes_price(m):
    raw = m.get("outcomePrices") or "[]"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return 0.0
    return float(raw[0]) if raw else 0.0

def parse_event(e):
    title = e.get("title", "")
    if any(k in title.lower() for k in SKIP_KEYWORDS):
        return None
    markets = e.get("markets") or []
    ml = [m for m in markets if m.get("sportsMarketType") == "moneyline"]
    home = draw = away = None

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
        raw = m.get("outcomePrices") or "[]"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        prices = [float(p) for p in raw] if raw else []
        vol = float(m.get("volume") or 0)
        home = {"price": prices[0] if prices else 0.0, "vol": vol}
        if len(prices) >= 2:
            away = {"price": prices[1], "vol": vol}

    if home is None:
        return None

    return {
        "id":        str(e.get("id", "")),
        "title":     title,
        "slug":      e.get("slug", ""),
        "startDate": e.get("startDate") or e.get("eventDate") or "",
        "liquidity": round(float(e.get("liquidityClob") or e.get("liquidity") or 0), 2),
        "volume":    round(sum(float(m.get("volume") or 0) for m in markets), 2),
        "is3way":    bool(home and draw and away),
        "home": home, "draw": draw, "away": away,
    }

def get_markets(sport_tag):
    key = f"pm_{sport_tag}"
    cached = cache_get(key, MARKET_TTL_MIN)
    if cached:
        return cached

    sport_info = next((s for s in SPORTS if s["tag"] == sport_tag), None)
    pinn_sport = sport_info["pinn_sport"] if sport_info else None

    try:
        r = requests.get(f"{GAMMA_BASE}/events",
                         params={"tag_slug": sport_tag, "limit": 100,
                                 "order": "startDate", "ascending": "false", "active": "true"},
                         timeout=15)
        r.raise_for_status()
        raw = r.json()
    except Exception as ex:
        return {"error": str(ex), "events": []}

    events = [parse_event(e) for e in raw]
    events = [e for e in events if e]
    events.sort(key=lambda x: x["liquidity"], reverse=True)

    pinn_index = {}
    if pinn_sport:
        try:
            pinn_index = build_pinnacle_index(pinn_sport)
        except Exception as ex:
            log.warning(f"Pinnacle build error: {ex}")

    matched = 0
    for ev in events:
        if not pinn_index:
            break
        match = find_pinnacle(ev["title"], pinn_index)
        if match:
            ev["pinnacle"] = {
                "home": match["h"], "draw": match["d"], "away": match["a"],
                "matched_title": f"{match['home']} vs {match['away']}",
            }
            if ev["home"] and match["h"] is not None:
                ev["diff_home"] = round((ev["home"]["price"] - match["h"]) * 100, 1)
            matched += 1

    result = {
        "events": events,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "pinnacle_matched": matched,
    }
    cache_set(key, result)
    return result

# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/api/refresh-pinnacle", methods=["POST"])
def refresh_pinnacle():
    sport = request.json.get("sport", "soccer") if request.json else "soccer"
    sport_info = next((s for s in SPORTS if s["tag"] == sport), None)
    if not sport_info:
        return jsonify({"error": "unknown sport"}), 400
    pinn_sport = sport_info["pinn_sport"]
    # Clear pinnacle index cache
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM cache WHERE key LIKE 'pinn_%'")
    con.execute("DELETE FROM cache WHERE key LIKE 'pm_%'")
    con.commit()
    con.close()
    log.info(f"Pinnacle cache cleared for rebuild")
    return jsonify({"status": "cleared"})

@app.route("/api/markets")
def api_markets():
    tag = request.args.get("sport", "soccer")
    if tag not in {s["tag"] for s in SPORTS}:
        return jsonify({"error": "unknown sport"}), 400
    return jsonify(get_markets(tag))

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, sports_json=json.dumps(SPORTS))

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Sports Board</title>
<style>
:root{--bg:#0d0f12;--bg2:#151820;--bg3:#1c2028;--border:#2a2d35;--text:#e2e4e9;--muted:#6b7280;--dim:#3d4148;--blue:#3b82f6;--blue-dim:#1e3a5f;--green:#22c55e;--green-dim:#14321e;--red:#ef4444;--red-dim:#3b1212;--amber:#f59e0b;--mono:'SF Mono','Fira Code','Consolas',monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;min-height:100vh}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border);background:var(--bg2)}
.logo{font-size:16px;font-weight:700;letter-spacing:-.02em}.logo span{color:var(--blue)}
.badge{font-size:10px;background:var(--blue-dim);color:var(--blue);padding:2px 7px;border-radius:3px;letter-spacing:.05em;margin-left:10px}
.header-right{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:11px}
#rbtn{background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:5px 12px;border-radius:4px;cursor:pointer;font-family:var(--mono);font-size:11px}
#rbtn:hover{background:var(--border)}
.toolbar{display:flex;align-items:center;gap:10px;padding:10px 20px;border-bottom:1px solid var(--border);background:var(--bg2);flex-wrap:wrap}
.tabs{display:flex;gap:4px}
.tab{font-family:var(--mono);font-size:11px;padding:5px 10px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer}
.tab:hover{background:var(--bg3);color:var(--text)}.tab.active{background:var(--blue);border-color:var(--blue);color:#fff}
.sort-wrap{margin-left:auto;display:flex;align-items:center;gap:8px}
select{font-family:var(--mono);font-size:11px;padding:5px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);cursor:pointer}
.stats-bar{display:flex;border-bottom:1px solid var(--border)}
.stat{flex:1;padding:10px 20px;border-right:1px solid var(--border)}.stat:last-child{border-right:none}
.stat-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.stat-val{font-size:18px;font-weight:700}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
thead th{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:500;padding:8px 12px;border-bottom:1px solid var(--border);background:var(--bg2);text-align:left;white-space:nowrap;position:sticky;top:0;z-index:1}
th.r,td.r{text-align:right}th.c,td.c{text-align:center}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
tbody tr:last-child{border-bottom:none}tbody tr:hover{background:var(--bg2)}
td{padding:9px 12px;vertical-align:middle}
.mn{font-weight:600;font-size:12px;max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mm{font-size:10px;color:var(--muted);margin-top:2px}.cd{color:var(--amber);margin-left:6px}
.odds{display:flex;gap:5px;align-items:center;font-size:12px}
.oh{color:#60a5fa;font-weight:600}.od{color:var(--muted)}.oa{color:#f87171;font-weight:600}.sp{color:var(--dim)}
.diff{display:inline-block;font-size:11px;padding:2px 7px;border-radius:3px;font-weight:600;min-width:42px;text-align:center}
.dp{background:var(--green-dim);color:var(--green)}.dn{background:var(--red-dim);color:var(--red)}.dz{background:var(--bg3);color:var(--muted)}
.vol{color:var(--muted);font-size:11px}
.lw{display:flex;align-items:center;gap:6px;justify-content:flex-end}
.lb{height:3px;border-radius:2px;background:var(--blue)}
.lnk{color:var(--blue);text-decoration:none;font-size:16px;opacity:.6;display:block;text-align:center}
.lnk:hover{opacity:1}
.ps{font-size:9px;color:var(--dim);margin-top:1px}
.sr td{text-align:center;color:var(--muted);padding:40px}
.er{color:var(--red)}
</style>
</head>
<body>
<header>
  <div style="display:flex;align-items:center">
    <div class="logo">PM<span>Board</span></div>
    <div class="badge">LIVE</div>
  </div>
  <div class="header-right">
    <span>Updated: <span id="lu">—</span></span>
    <button id="rbtn" onclick="load()">⟳ Refresh</button>
    <button id="pbtn" onclick="refreshPinnacle()" style="background:var(--bg3);border:1px solid var(--border);color:var(--amber);padding:5px 12px;border-radius:4px;cursor:pointer;font-family:var(--mono);font-size:11px;margin-left:4px">⟳ Pinnacle</button>
  </div>
</header>
<div class="toolbar">
  <div class="tabs" id="tabs"></div>
  <div class="sort-wrap">
    <select id="ss" onchange="render()">
      <option value="liquidity">Liquidity ↓</option>
      <option value="volume">Volume ↓</option>
      <option value="diff">Diff ↓</option>
      <option value="time">Time ↑</option>
    </select>
  </div>
</div>
<div class="stats-bar">
  <div class="stat"><div class="stat-label">Markets</div><div class="stat-val" id="sc">—</div></div>
  <div class="stat"><div class="stat-label">Total Liquidity</div><div class="stat-val" id="sl">—</div></div>
  <div class="stat"><div class="stat-label">Total Volume</div><div class="stat-val" id="sv">—</div></div>
  <div class="stat"><div class="stat-label">Pinnacle matched</div><div class="stat-val" id="sp">—</div></div>
</div>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th style="width:26%">Match</th>
        <th style="width:17%">Polymarket</th>
        <th style="width:17%">Pinnacle</th>
        <th style="width:9%" class="c">Diff (H)</th>
        <th style="width:10%" class="r">Volume</th>
        <th style="width:11%" class="r">Liquidity</th>
        <th style="width:10%" class="c">Link</th>
      </tr>
    </thead>
    <tbody id="tb"><tr class="sr"><td colspan="7">Loading…</td></tr></tbody>
  </table>
</div>
<script>
const SPORTS={{ sports_json|safe }};
const PM='https://polymarket.com/event';
let tag='soccer',evts=[],maxL=1;
const f$=v=>{if(!v||isNaN(v))return'—';if(v>=1e6)return'$'+(v/1e6).toFixed(1)+'M';if(v>=1e3)return'$'+Math.round(v/1e3)+'k';return'$'+Math.round(v)};
const fC=v=>v==null?'—':Math.round(v*100)+'¢';
const fT=s=>{if(!s)return'—';const h=Math.round((new Date(s)-new Date())/36e5);if(isNaN(h))return'—';if(h<0)return'started';if(h<24)return'T-'+h+'h';return'T-'+Math.floor(h/24)+'d'};
const fD=s=>s?new Date(s).toISOString().slice(5,16).replace('T',' ')+'Z':'';
function db(d){if(d==null)return'<span class="diff dz">—</span>';if(Math.abs(d)<0.5)return'<span class="diff dz">±0</span>';return`<span class="diff ${d>0?'dp':'dn'}">${d>0?'+':''}${d.toFixed(1)}¢</span>`}
function oc(h,d,a){if(h==null)return'—';let s=`<span class="oh">${fC(h)}</span>`;if(d!=null)s+=`<span class="sp">/</span><span class="od">${fC(d)}</span>`;s+=`<span class="sp">/</span><span class="oa">${fC(a)}</span>`;return`<div class="odds">${s}</div>`}
function sortE(arr){const by=document.getElementById('ss').value,s=[...arr];if(by==='liquidity')s.sort((a,b)=>b.liquidity-a.liquidity);else if(by==='volume')s.sort((a,b)=>b.volume-a.volume);else if(by==='time')s.sort((a,b)=>new Date(a.startDate)-new Date(b.startDate));else if(by==='diff')s.sort((a,b)=>Math.abs(b.diff_home||0)-Math.abs(a.diff_home||0));return s}
function render(){
  const tb=document.getElementById('tb');
  if(!evts.length){tb.innerHTML='<tr class="sr"><td colspan="7">No markets</td></tr>';return}
  maxL=Math.max(...evts.map(e=>e.liquidity),1);
  tb.innerHTML=sortE(evts).slice(0,80).map(ev=>{
    const bw=Math.max(2,Math.round(ev.liquidity/maxL*70));
    const t=ev.title.length>42?ev.title.slice(0,39)+'…':ev.title;
    const pm_h=ev.home?.price,pm_d=ev.draw?.price,pm_a=ev.away?.price;
    const pin=ev.pinnacle;
    return`<tr>
      <td><div class="mn" title="${ev.title}">${t}</div><div class="mm">${fD(ev.startDate)}<span class="cd">${fT(ev.startDate)}</span>${ev.is3way?' <span style="color:var(--dim)">3W</span>':''}</div></td>
      <td>${oc(pm_h,pm_d,pm_a)}</td>
      <td>${pin?`<div>${oc(pin.home,pin.draw,pin.away)}<div class="ps">${(pin.matched_title||'Pinnacle').slice(0,30)}</div></div>`:'<span style="color:var(--dim);font-size:11px">no match</span>'}</td>
      <td class="c">${db(ev.diff_home??null)}</td>
      <td class="r"><span class="vol">${f$(ev.volume)}</span></td>
      <td class="r"><div class="lw"><span class="vol">${f$(ev.liquidity)}</span><div class="lb" style="width:${bw}px"></div></div></td>
      <td class="c"><a class="lnk" href="${PM}/${ev.slug}" target="_blank">↗</a></td>
    </tr>`;
  }).join('');
}
function stats(data){
  const e=data.events||[];
  document.getElementById('sc').textContent=e.length;
  document.getElementById('sl').textContent=f$(e.reduce((s,x)=>s+x.liquidity,0));
  document.getElementById('sv').textContent=f$(e.reduce((s,x)=>s+x.volume,0));
  document.getElementById('sp').textContent=(data.pinnacle_matched||0)+' / '+e.length;
  document.getElementById('lu').textContent=new Date().toISOString().slice(11,19)+' UTC';
}
async function load(){
  const btn=document.getElementById('rbtn');
  btn.textContent='⟳ ...';
  document.getElementById('tb').innerHTML='<tr class="sr"><td colspan="7">Fetching…</td></tr>';
  try{
    const r=await fetch(`/api/markets?sport=${tag}`);
    const data=await r.json();
    if(data.error)throw new Error(data.error);
    evts=data.events||[];
    stats(data);render();
  }catch(e){document.getElementById('tb').innerHTML=`<tr class="sr"><td colspan="7" class="er">Error: ${e.message}</td></tr>`}
  btn.textContent='⟳ Refresh';
}
function initTabs(){
  const c=document.getElementById('tabs');
  SPORTS.forEach((s,i)=>{
    const b=document.createElement('button');
    b.className='tab'+(i===0?' active':'');
    b.textContent=s.emoji+' '+s.label;
    b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');tag=s.tag;load()};
    c.appendChild(b);
  });
}
async function refreshPinnacle(){
  const btn=document.getElementById('pbtn');
  btn.textContent='⟳ ...';
  try{
    await fetch('/api/refresh-pinnacle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sport:tag})});
    await load();
  }catch(e){alert('Error: '+e.message)}
  btn.textContent='⟳ Pinnacle';
}
initTabs();load();
</script>
</body>
</html>"""

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, debug=False)

init_db()
