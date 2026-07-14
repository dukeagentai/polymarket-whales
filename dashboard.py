#!/usr/bin/env python3
"""
🐋 Polymarket Whale Tracker — web dashboard.

Simple Flask UI over the whale database (SQLite locally, Postgres on Railway).

    python dashboard.py               # dev server on :8000
    gunicorn dashboard:app            # production (Railway web service)
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, render_template_string, request, redirect, jsonify

import db
from db import WhaleTrade, Wallet, WatchedAddress, WatchedTrade, Market

app = Flask(__name__)

Session = db.init_db()

DATA_API = os.getenv("POLYMARKET_API_URL", "https://data-api.polymarket.com")

# address -> (monotonic_ts, positions); short TTL so page loads don't hammer the API
_positions_cache: dict = {}
_POSITIONS_TTL = 120
_POSITIONS_CACHE_CAP = 500


def fetch_positions(address: str, limit: int = 100) -> list:
    """Live open positions for a wallet from the data-api, TTL-cached.
    Fallback path only — used on /wallet/<address> for wallets the
    sync_positions.py job doesn't cover (unwatched wallets with no synced
    rows yet). Watched wallets and the /watchlist page read the DB instead
    (see db.get_wallet_positions / db.positions_summary_db) so page loads
    don't block on one HTTP call per wallet.
    Serves stale data if the API errors."""
    now = time.monotonic()
    hit = _positions_cache.get(address)
    if hit and now - hit[0] < _POSITIONS_TTL:
        return hit[1]
    try:
        resp = requests.get(f"{DATA_API}/positions",
                            params={"user": address, "limit": limit}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        positions = data if isinstance(data, list) else []
    except Exception:
        positions = hit[1] if hit else []
    while len(_positions_cache) >= _POSITIONS_CACHE_CAP:
        _positions_cache.pop(next(iter(_positions_cache)))
    _positions_cache[address] = (now, positions)
    return positions


def _normalize_position(p) -> dict:
    """Normalize a position to the data-api's camelCase shape, whether it
    came from a live call (dict) or the synced wallet_positions table
    (db.WalletPosition row)."""
    if isinstance(p, dict):
        return p
    return {
        "title": p.title, "outcome": p.outcome, "size": p.size,
        "avgPrice": p.avg_price, "curPrice": p.cur_price,
        "currentValue": p.current_value, "cashPnl": p.cash_pnl,
        "percentPnl": p.percent_pnl, "conditionId": p.condition_id,
    }


def positions_summary(positions: list) -> dict:
    """Collapse a positions list into {value, pnl, count}."""
    return {
        "value": sum(p.get("currentValue") or 0 for p in positions),
        "pnl": sum(p.get("cashPnl") or 0 for p in positions),
        "count": len(positions),
    }

CSS = """
  :root {
    --page: #f9f9f7; --surface: #fcfcfb;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6; --yes: #006300; --no: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --page: #0d0d0d; --surface: #1a1a19;
      --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
      --series-1: #3987e5; --yes: #0ca30c; --no: #d03b3b;
    }
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--page); color: var(--ink);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px; max-width: 1100px; margin: 0 auto;
  }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .sub { color: var(--ink-2); margin-bottom: 24px; font-size: 13px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .tile .label { font-size: 12px; color: var(--ink-2); }
  .tile .value { font-size: 26px; font-weight: 650; margin-top: 2px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 24px; }
  .card h2 { font-size: 14px; font-weight: 600; margin-bottom: 12px; }
  .card h2 .hint { color: var(--muted); font-weight: 400; }

  .chart { display: flex; align-items: flex-end; gap: 2px; height: 140px; border-bottom: 1px solid var(--baseline); }
  .bar-slot { flex: 1; display: flex; align-items: flex-end; height: 100%; position: relative; }
  .bar { width: 100%; background: var(--series-1); border-radius: 4px 4px 0 0; min-height: 1px; }
  .bar-slot .tip {
    display: none; position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%);
    background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
    padding: 4px 8px; font-size: 12px; white-space: nowrap; z-index: 2;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
  }
  .bar-slot:hover .tip { display: block; }
  .bar-slot:hover .bar { opacity: 0.85; }
  .xlabels { display: flex; gap: 2px; margin-top: 4px; }
  .xlabels span { flex: 1; text-align: center; font-size: 10px; color: var(--muted); }

  form.filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  input, select {
    background: var(--surface); color: var(--ink); border: 1px solid var(--baseline);
    border-radius: 8px; padding: 7px 10px; font: inherit; font-size: 13px;
  }
  button {
    background: var(--series-1); color: #fff; border: 0; border-radius: 8px;
    padding: 7px 14px; font: inherit; font-size: 13px; cursor: pointer;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--ink-2); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--baseline); }
  td { padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .yes { color: var(--yes); font-weight: 600; }
  .no { color: var(--no); font-weight: 600; }
  .tag { font-size: 12px; color: var(--ink-2); }
  .addr { font-family: ui-monospace, monospace; font-size: 12px; }
  .empty { color: var(--muted); padding: 24px; text-align: center; }
  .scroll { overflow-x: auto; }
  nav { display: flex; gap: 16px; margin-bottom: 20px; font-size: 13px; }
  nav a { color: var(--ink-2); text-decoration: none; font-weight: 600; }
  nav a:hover { color: var(--ink); }
  .badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 1px 8px;
           border-radius: 999px; background: var(--yes); color: #fff;
           margin-left: 6px; white-space: nowrap; }
  .btn-danger { background: var(--no); padding: 4px 10px; font-size: 12px; }
  .warnbar { background: var(--no); color: #fff; border-radius: 10px;
             padding: 10px 16px; margin-bottom: 20px; font-weight: 600; font-size: 13px; }
  .muted { color: var(--muted); font-size: 12px; }
  form.inline { display: inline; }
  .poslist { margin: 0; padding: 0; list-style: none; }
  .poslist li { margin: 2px 0; }
  .pager { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; font-size: 13px; }
  .pager a { color: var(--series-1); text-decoration: none; font-weight: 600; }
"""


def page(title: str, body: str) -> str:
    """Wrap a page body with the shared head, CSS, and nav."""
    return ("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>""" + title + """</title>
<style>""" + CSS + """</style>
</head>
<body>
  <nav><a href="/">🐋 Live feed</a><a href="/watchlist">👀 Watchlist</a><a href="/leaderboard">🏆 Leaderboard</a><a href="/markets">🗂️ Markets</a></nav>
  {% if tracker and tracker.alive == False %}
  <div class="warnbar">⚠️ Tracker is down — no poll for
    {{ (tracker.stale_seconds / 60) | round(0) | int }} min
    (last: {{ tracker.last_poll_at.strftime("%Y-%m-%d %H:%M UTC") }}).
    New trades are not being recorded.</div>
  {% endif %}
""" + body + """
</body>
</html>""")


TEMPLATE = page("🐋 Polymarket Whales", """
  <h1>🐋 Polymarket Whales</h1>
  <p class="sub">Whale trades ≥ threshold, live from the tracker · auto-refreshes every 60s</p>

  <div class="tiles">
    <div class="tile"><div class="label">Whale trades</div><div class="value">{{ "{:,}".format(stats.total_trades) }}</div></div>
    <div class="tile"><div class="label">Total whale volume</div><div class="value">${{ "{:,.0f}".format(stats.total_volume) }}</div></div>
    <div class="tile"><div class="label">Unique wallets</div><div class="value">{{ "{:,}".format(stats.unique_wallets) }}</div></div>
    <div class="tile"><div class="label">Biggest trade</div><div class="value">${{ "{:,.0f}".format(stats.biggest_trade) }}</div></div>
  </div>

  <div class="card">
    <h2>Whale volume — last 24h <span class="hint">(hourly, UTC)</span></h2>
    {% if hourly_max > 0 %}
    <div class="chart">
      {% for h in hourly %}
      <div class="bar-slot">
        <div class="bar" style="height: {{ (h.volume / hourly_max * 100) | round(1) }}%"></div>
        <div class="tip">{{ h.label }}:00 · ${{ "{:,.0f}".format(h.volume) }} · {{ h.count }} trade{{ "s" if h.count != 1 }}</div>
      </div>
      {% endfor %}
    </div>
    <div class="xlabels">
      {% for h in hourly %}<span>{{ h.label if loop.index0 % 4 == 0 else "" }}</span>{% endfor %}
    </div>
    {% else %}
    <div class="empty">No whale trades recorded in the last 24 hours.</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Recent whale trades</h2>
    <form class="filters" method="get">
      <input type="text" name="q" placeholder="Filter by market keyword…" value="{{ q }}">
      <select name="category">
        <option value="">All categories</option>
        {% for c in categories %}
        <option value="{{ c }}" {{ "selected" if c == category }}>{{ c }}</option>
        {% endfor %}
      </select>
      <input type="text" name="wallet" placeholder="Wallet 0x…" value="{{ wallet }}">
      <button type="submit">Filter</button>
    </form>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Time (UTC)</th><th>Market</th><th>Category</th><th>Side</th>
        <th class="num">Amount</th><th class="num">Price</th><th>Wallet</th><th>Result</th>
      </tr></thead>
      <tbody>
      {% for t in trades %}
      <tr>
        <td class="num">{{ t.traded_at.strftime("%m-%d %H:%M") if t.traded_at else "—" }}</td>
        <td>{% if t.condition_id %}<a href="/market/{{ t.condition_id }}" style="color: inherit">{{ t.market_title }}</a>{% else %}{{ t.market_title }}{% endif %}</td>
        <td>{{ t.category or "—" }}</td>
        <td class="{{ 'yes' if t.side == 'YES' else 'no' }}">{{ t.side }}</td>
        <td class="num">${{ "{:,.0f}".format(t.amount_usd) }}</td>
        <td class="num">{{ "%.3f" | format(t.price) }}</td>
        <td class="addr">{% if t.wallet %}<a href="/wallet/{{ t.wallet }}" style="color: inherit">{% if t.wallet in watched_set %}👀 {% endif %}{{ t.wallet[:6] ~ "…" ~ t.wallet[-4:] }}</a>{% else %}—{% endif %}</td>
        <td>{% if t.result == 'WIN' %}<span class="yes">✅ WIN</span>{% elif t.result == 'LOSS' %}<span class="no">❌ LOSS</span>{% else %}<span class="muted">—</span>{% endif %}</td>
      </tr>
      {% else %}
      <tr><td colspan="8" class="empty">No whale trades yet — start the tracker: <code>python main.py</code></td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
    <div class="pager">
      {% if page > 1 %}<a href="?{{ pager_qs }}page={{ page - 1 }}">← Newer</a>{% else %}<span class="muted">← Newer</span>{% endif %}
      <span class="muted">page {{ page }}{% if total_pages %} of {{ total_pages }}{% endif %}</span>
      {% if has_next %}<a href="?{{ pager_qs }}page={{ page + 1 }}">Older →</a>{% else %}<span class="muted">Older →</span>{% endif %}
    </div>
  </div>

  <div class="card">
    <h2>Top whale wallets</h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Wallet</th><th>Tag</th><th class="num">Trades</th>
        <th class="num">Total volume</th><th>Record</th><th class="num">Realized P&L</th>
        <th>Last seen (UTC)</th>
      </tr></thead>
      <tbody>
      {% for w in wallets %}
      {% set r = wallet_records.get(w.address, {}) %}
      <tr>
        <td class="addr"><a href="/wallet/{{ w.address }}" style="color: inherit">{% if w.address in watched_set %}👀 {% endif %}{{ w.address[:10] ~ "…" ~ w.address[-6:] }}</a></td>
        <td class="tag">{{ w.tag or "—" }}</td>
        <td class="num">{{ w.trade_count }}</td>
        <td class="num">${{ "{:,.0f}".format(w.total_usd) }}</td>
        <td class="tag">{% if r.get("wins", 0) + r.get("losses", 0) > 0 %}{{ r.get("wins", 0) }}W–{{ r.get("losses", 0) }}L ({{ "{:.0%}".format(r["win_rate"]) }}){% else %}—{% endif %}</td>
        <td class="num {{ 'yes' if r.get('realized_pnl', 0) >= 0 else 'no' }}">{{ "{:+,.0f}".format(r.get("realized_pnl", 0)) }}</td>
        <td class="num">{{ w.last_seen.strftime("%m-%d %H:%M") if w.last_seen else "—" }}</td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">No wallets tracked yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
""")


WATCHLIST_TEMPLATE = page("👀 Watchlist — Polymarket Whales", """
  <h1>👀 Watchlist</h1>
  <p class="sub">Follow specific wallets — every Polymarket trade they make is recorded and alerted, any size · auto-refreshes every 60s</p>

  <div class="card">
    <h2>Watched addresses <span class="hint">({{ addresses|length }})</span></h2>
    <p class="muted">{{ positions_synced_note }}</p>
    <form class="filters" method="post" action="/watchlist/add">
      <input type="text" name="address" placeholder="0x wallet address…" size="46"
             required pattern="0x[0-9a-fA-F]{40}" title="Full 0x wallet address (42 characters)">
      <input type="text" name="label" placeholder="Label (optional), e.g. Sharp Sam" size="28">
      <button type="submit">＋ Watch address</button>
    </form>
    {% if error %}<p class="muted">⚠️ {{ error }}</p>{% endif %}
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Address</th><th>Label</th><th class="num">Trades</th>
        <th class="num">Volume</th><th class="num">Open value</th>
        <th class="num">Unrealized P&L</th><th>Record</th><th class="num">Realized P&L</th>
        <th>Last trade (UTC)</th><th>Added</th><th></th>
      </tr></thead>
      <tbody>
      {% for a in addresses %}
      {% set s = stats.get(a.address, {}) %}
      {% set p = pnl.get(a.address, {}) %}
      {% set r = records.get(a.address, {}) %}
      <tr>
        <td class="addr"><a href="/wallet/{{ a.address }}" style="color: inherit">{{ a.address }}</a></td>
        <td class="tag">{{ a.label or "—" }}</td>
        <td class="num">{{ s.get("trades", 0) }}</td>
        <td class="num">${{ "{:,.0f}".format(s.get("volume", 0)) }}</td>
        <td class="num">${{ "{:,.0f}".format(p.get("value", 0)) }}</td>
        <td class="num {{ 'yes' if p.get('pnl', 0) >= 0 else 'no' }}">{{ "{:+,.0f}".format(p.get("pnl", 0)) }}</td>
        <td class="tag">{% if r.get("wins", 0) + r.get("losses", 0) > 0 %}{{ r.get("wins", 0) }}W–{{ r.get("losses", 0) }}L ({{ "{:.0%}".format(r["win_rate"]) }}){% else %}—{% endif %}</td>
        <td class="num {{ 'yes' if r.get('realized_pnl', 0) >= 0 else 'no' }}">{{ "{:+,.0f}".format(r.get("realized_pnl", 0)) }}</td>
        <td class="num">{{ s["last_traded"].strftime("%m-%d %H:%M") if s.get("last_traded") else "—" }}</td>
        <td class="num">{{ a.added_at.strftime("%Y-%m-%d") if a.added_at else "—" }}</td>
        <td><form class="inline" method="post" action="/watchlist/remove">
          <input type="hidden" name="address" value="{{ a.address }}">
          <button class="btn-danger" type="submit">Remove</button>
        </form></td>
      </tr>
      {% else %}
      <tr><td colspan="11" class="empty">No watched addresses yet — paste a wallet above to start tracking it.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Market convergence <span class="hint">— markets your watched wallets entered, most crowded first</span></h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Market</th><th class="num">Watched wallets</th><th>Positions</th>
        <th class="num">Total wagered</th><th>Last trade (UTC)</th>
      </tr></thead>
      <tbody>
      {% for m in convergence %}
      <tr>
        <td>{% if m.condition_id %}<a href="/market/{{ m.condition_id }}" style="color: inherit">{{ m.title }}</a>{% else %}{{ m.title }}{% endif %}{% if m.consensus %}<span class="badge">⚡ ALL SAME SIDE</span>{% endif %}</td>
        <td class="num">{{ m.wallet_count }}</td>
        <td>
          <ul class="poslist">
          {% for p in m.positions %}
            <li>
              <span class="{{ 'yes' if p.outcome.upper() == 'YES' else ('no' if p.outcome.upper() == 'NO' else '') }}">{{ p.outcome }} ({{ p.side }})</span>
              · {{ p.wallets|length }} wallet{{ "s" if p.wallets|length != 1 }}
              · ${{ "{:,.0f}".format(p.total) }}
              <span class="muted">{{ p.names }}</span>
            </li>
          {% endfor %}
          </ul>
        </td>
        <td class="num">${{ "{:,.0f}".format(m.total) }}</td>
        <td class="num">{{ m.last_traded.strftime("%m-%d %H:%M") if m.last_traded else "—" }}</td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="empty">No trades from watched wallets yet — they'll appear here as soon as the tracker sees one.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Unknown wallets worth a look <span class="hint">— top feed wallets not on your watchlist</span></h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Wallet</th><th>Tag</th><th class="num">Whale trades</th>
        <th class="num">Whale volume</th><th>Last seen (UTC)</th><th></th>
      </tr></thead>
      <tbody>
      {% for w in unwatched %}
      <tr>
        <td class="addr"><a href="/wallet/{{ w.address }}" style="color: inherit">{{ w.address[:10] ~ "…" ~ w.address[-6:] }}</a></td>
        <td class="tag">{{ w.tag or "—" }}</td>
        <td class="num">{{ w.trade_count }}</td>
        <td class="num">${{ "{:,.0f}".format(w.total_usd) }}</td>
        <td class="num">{{ w.last_seen.strftime("%m-%d %H:%M") if w.last_seen else "—" }}</td>
        <td><form class="inline" method="post" action="/watchlist/add">
          <input type="hidden" name="address" value="{{ w.address }}">
          <button type="submit" title="Add to watchlist">👀 Watch</button>
        </form></td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="empty">No unwatched wallets in the feed yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Recent trades by watched wallets</h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Time (UTC)</th><th>Wallet</th><th>Market</th><th>Position</th>
        <th class="num">Amount</th><th class="num">Price</th><th>Result</th>
      </tr></thead>
      <tbody>
      {% for t in recent %}
      <tr>
        <td class="num">{{ t.traded_at.strftime("%m-%d %H:%M") if t.traded_at else "—" }}</td>
        <td class="addr">{{ labels.get(t.address, t.address[:6] ~ "…" ~ t.address[-4:]) }}</td>
        <td>{% if t.condition_id %}<a href="/market/{{ t.condition_id }}" style="color: inherit">{{ t.market_title }}</a>{% else %}{{ t.market_title }}{% endif %}</td>
        <td><span class="{{ 'yes' if (t.outcome or '').upper() == 'YES' else ('no' if (t.outcome or '').upper() == 'NO' else '') }}">{{ t.outcome }} ({{ t.side }})</span></td>
        <td class="num">${{ "{:,.2f}".format(t.amount_usd) }}</td>
        <td class="num">{{ "%.3f" | format(t.price) }}</td>
        <td>{% if t.result == 'WIN' %}<span class="yes">✅ WIN</span>{% elif t.result == 'LOSS' %}<span class="no">❌ LOSS</span>{% else %}<span class="muted">—</span>{% endif %}</td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">Nothing yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
""")


LEADERBOARD_TEMPLATE = page("🏆 Leaderboard — Polymarket Whales", """
  <h1>🏆 Whale leaderboard</h1>
  <p class="sub">Top wallets by whale-trade volume · trailing {{ days }} days</p>

  <div class="card">
    <h2>
      <a href="?days=7" style="{{ 'font-weight:700' if days == 7 else 'color:var(--muted)' }}">7 days</a> ·
      <a href="?days=30" style="{{ 'font-weight:700' if days == 30 else 'color:var(--muted)' }}">30 days</a>
    </h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th class="num">#</th><th>Wallet</th><th>Tag</th><th class="num">Trades</th>
        <th class="num">Volume</th><th class="num">Biggest</th><th>Record</th>
        <th class="num">Realized P&L</th><th>Last active (UTC)</th><th></th>
      </tr></thead>
      <tbody>
      {% for w in board %}
      {% set r = records.get(w.address, {}) %}
      <tr>
        <td class="num">{{ loop.index }}</td>
        <td class="addr"><a href="/wallet/{{ w.address }}" style="color: inherit">{{ w.address[:10] ~ "…" ~ w.address[-6:] }}</a></td>
        <td class="tag">{{ w.tag or "—" }}</td>
        <td class="num">{{ w.trades }}</td>
        <td class="num">${{ "{:,.0f}".format(w.volume) }}</td>
        <td class="num">${{ "{:,.0f}".format(w.biggest) }}</td>
        <td class="tag">{% if r.get("wins", 0) + r.get("losses", 0) > 0 %}{{ r.get("wins", 0) }}W–{{ r.get("losses", 0) }}L ({{ "{:.0%}".format(r["win_rate"]) }}){% else %}—{% endif %}</td>
        <td class="num {{ 'yes' if r.get('realized_pnl', 0) >= 0 else 'no' }}">{{ "{:+,.0f}".format(r.get("realized_pnl", 0)) }}</td>
        <td class="num">{{ w.last_traded.strftime("%m-%d %H:%M") if w.last_traded else "—" }}</td>
        <td>{% if w.address not in watched %}
          <form class="inline" method="post" action="/watchlist/add">
            <input type="hidden" name="address" value="{{ w.address }}">
            <button type="submit" title="Add to watchlist">👀 Watch</button>
          </form>
        {% else %}<span class="muted">watching</span>{% endif %}</td>
      </tr>
      {% else %}
      <tr><td colspan="10" class="empty">No whale trades in this window yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
""")


WALLET_TEMPLATE = page("Wallet — Polymarket Whales", """
  <h1 class="addr" style="font-size:18px">{{ address }}</h1>
  <p class="sub">
    {{ tag or "No tag yet" }}
    {% if watch_label is not none %}· 👀 watched{% if watch_label %} as “{{ watch_label }}”{% endif %}{% endif %}
  </p>

  {% if watch_label is none %}
  <form class="filters" method="post" action="/watchlist/add">
    <input type="hidden" name="address" value="{{ address }}">
    <input type="text" name="label" placeholder="Label (optional)" size="24">
    <button type="submit">👀 Add to watchlist</button>
  </form>
  {% endif %}

  <div class="tiles">
    <div class="tile"><div class="label">Whale trades</div><div class="value">{{ "{:,}".format(trade_count) }}</div></div>
    <div class="tile"><div class="label">Whale volume</div><div class="value">${{ "{:,.0f}".format(total_usd) }}</div></div>
    <div class="tile"><div class="label">Record</div><div class="value" style="font-size:18px">{% if record.wins + record.losses > 0 %}{{ record.wins }}W–{{ record.losses }}L <span class="muted" style="font-size:13px">({{ "{:.0%}".format(record.win_rate) }})</span>{% else %}—{% endif %}</div></div>
    <div class="tile"><div class="label">Realized P&L</div><div class="value {{ 'yes' if record.realized_pnl >= 0 else 'no' }}" style="font-size:18px">{{ "{:+,.0f}".format(record.realized_pnl) }}</div></div>
    <div class="tile"><div class="label">First seen</div><div class="value" style="font-size:16px">{{ first_seen.strftime("%Y-%m-%d") if first_seen else "—" }}</div></div>
    <div class="tile"><div class="label">Last seen</div><div class="value" style="font-size:16px">{{ last_seen.strftime("%Y-%m-%d %H:%M") if last_seen else "—" }}</div></div>
  </div>

  <div class="card">
    <h2>Open positions <span class="hint">{% if positions_synced_at %}synced {{ positions_synced_at.strftime("%m-%d %H:%M UTC") }}{% else %}live from Polymarket{% endif %} · unrealized P&L</span></h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Market</th><th>Outcome</th><th class="num">Shares</th>
        <th class="num">Avg price</th><th class="num">Now</th>
        <th class="num">Value</th><th class="num">P&L</th>
      </tr></thead>
      <tbody>
      {% for p in positions %}
      <tr>
        <td>{{ p.title }}</td>
        <td class="{{ 'yes' if p.outcome == 'Yes' else ('no' if p.outcome == 'No' else '') }}">{{ p.outcome }}</td>
        <td class="num">{{ "{:,.0f}".format(p.size or 0) }}</td>
        <td class="num">{{ "%.3f" | format(p.avgPrice or 0) }}</td>
        <td class="num">{{ "%.3f" | format(p.curPrice or 0) }}</td>
        <td class="num">${{ "{:,.0f}".format(p.currentValue or 0) }}</td>
        <td class="num {{ 'yes' if (p.cashPnl or 0) >= 0 else 'no' }}">{{ "{:+,.0f}".format(p.cashPnl or 0) }} ({{ "{:+,.0f}".format(p.percentPnl or 0) }}%)</td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">No open positions.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Top markets <span class="hint">by this wallet's whale volume</span></h2>
    <div class="scroll">
    <table>
      <thead><tr><th>Market</th><th class="num">Trades</th><th class="num">Volume</th><th>Last trade (UTC)</th></tr></thead>
      <tbody>
      {% for m in markets %}
      <tr>
        <td>{% if m.condition_id %}<a href="/market/{{ m.condition_id }}" style="color: inherit">{{ m.market }}</a>{% else %}{{ m.market }}{% endif %}</td>
        <td class="num">{{ m.trades }}</td>
        <td class="num">${{ "{:,.0f}".format(m.volume) }}</td>
        <td class="num">{{ m.last_traded.strftime("%m-%d %H:%M") if m.last_traded else "—" }}</td>
      </tr>
      {% else %}
      <tr><td colspan="4" class="empty">No whale trades recorded for this wallet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Recent whale trades</h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Time (UTC)</th><th>Market</th><th>Side</th>
        <th class="num">Amount</th><th class="num">Price</th><th>Result</th>
      </tr></thead>
      <tbody>
      {% for t in trades %}
      <tr>
        <td class="num">{{ t.traded_at.strftime("%m-%d %H:%M") if t.traded_at else "—" }}</td>
        <td>{% if t.condition_id %}<a href="/market/{{ t.condition_id }}" style="color: inherit">{{ t.market_title }}</a>{% else %}{{ t.market_title }}{% endif %}</td>
        <td class="{{ 'yes' if t.side == 'YES' else 'no' }}">{{ t.side }}</td>
        <td class="num">${{ "{:,.0f}".format(t.amount_usd) }}</td>
        <td class="num">{{ "%.3f" | format(t.price) }}</td>
        <td>{% if t.result == 'WIN' %}<span class="yes">✅ WIN</span>{% elif t.result == 'LOSS' %}<span class="no">❌ LOSS</span>{% else %}<span class="muted">—</span>{% endif %}</td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="empty">Nothing yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
""")


MARKETS_TEMPLATE = page("🗂️ Markets — Polymarket Whales", """
  <h1>🗂️ Markets</h1>
  <p class="sub">Every market we've recorded a trade in · open first</p>

  <div class="card">
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Market</th><th>Category</th><th>Status</th>
        <th class="num">Watched wallets</th><th class="num">Whale volume</th><th>End date</th>
      </tr></thead>
      <tbody>
      {% for m in markets %}
      <tr>
        <td><a href="/market/{{ m.condition_id }}" style="color: inherit">{{ m.title }}</a></td>
        <td>{{ m.category or "—" }}</td>
        <td>{% if m.resolved %}<span class="tag">✅ {{ m.winning_outcome }}</span>{% else %}<span class="tag">🟢 Open</span>{% endif %}</td>
        <td class="num">{{ m.watched_count }}</td>
        <td class="num">${{ "{:,.0f}".format(m.volume) }}</td>
        <td class="num">{{ m.end_date.strftime("%Y-%m-%d") if m.end_date else "—" }}</td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="empty">No markets recorded yet — they're populated as the tracker sees trades.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
""")


MARKET_TEMPLATE = page("Market — Polymarket Whales", """
  <h1 style="font-size:18px">{{ market.title or market.condition_id }}</h1>
  <p class="sub">
    {{ market.category or "Uncategorized" }}
    {% if market.resolved %}· <span class="yes">✅ Resolved: {{ market.winning_outcome }}</span>{% else %}· <span class="tag">🟢 Open</span>{% endif %}
    {% if market.end_date %}· ends {{ market.end_date.strftime("%Y-%m-%d") }}{% endif %}
  </p>

  <div class="card">
    <h2>Watched wallets in this market <span class="hint">({{ participants.watched|length }})</span></h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Wallet</th><th>Label</th><th class="num">Trades</th>
        <th>Positions</th><th class="num">Total</th><th>Last trade (UTC)</th>
      </tr></thead>
      <tbody>
      {% for p in participants.watched %}
      <tr>
        <td class="addr"><a href="/wallet/{{ p.address }}" style="color: inherit">{{ p.address[:10] ~ "…" ~ p.address[-6:] }}</a></td>
        <td class="tag">{{ p.label_or_tag or "—" }}</td>
        <td class="num">{{ p.trades }}</td>
        <td>{% for k, v in p.outcomes.items() %}<div><span class="{{ 'yes' if k.upper().startswith('YES') else ('no' if k.upper().startswith('NO') else '') }}">{{ k }}</span> — ${{ "{:,.0f}".format(v) }}</div>{% endfor %}</td>
        <td class="num">${{ "{:,.0f}".format(p.volume) }}</td>
        <td class="num">{{ p.last_traded.strftime("%m-%d %H:%M") if p.last_traded else "—" }}</td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="empty">No watched wallets in this market.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Other wallets <span class="hint">({{ participants.unknown|length }})</span></h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Wallet</th><th>Tag</th><th class="num">Trades</th>
        <th>Positions</th><th class="num">Total</th><th>Last trade (UTC)</th><th></th>
      </tr></thead>
      <tbody>
      {% for p in participants.unknown %}
      <tr>
        <td class="addr"><a href="/wallet/{{ p.address }}" style="color: inherit">{{ p.address[:10] ~ "…" ~ p.address[-6:] }}</a></td>
        <td class="tag">{{ p.label_or_tag or "—" }}</td>
        <td class="num">{{ p.trades }}</td>
        <td>{% for k, v in p.outcomes.items() %}<div><span class="{{ 'yes' if k.upper().startswith('YES') else ('no' if k.upper().startswith('NO') else '') }}">{{ k }}</span> — ${{ "{:,.0f}".format(v) }}</div>{% endfor %}</td>
        <td class="num">${{ "{:,.0f}".format(p.volume) }}</td>
        <td class="num">{{ p.last_traded.strftime("%m-%d %H:%M") if p.last_traded else "—" }}</td>
        <td><form class="inline" method="post" action="/watchlist/add">
          <input type="hidden" name="address" value="{{ p.address }}">
          <button type="submit" title="Add to watchlist">👀 Watch</button>
        </form></td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">No other wallets recorded in this market.</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
""")


def hourly_volume(session, hours: int = 24) -> list:
    """Bucket whale volume per hour over the trailing window."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=hours - 1)
    rows = (
        session.query(WhaleTrade.traded_at, WhaleTrade.amount_usd)
        .filter(WhaleTrade.traded_at >= start.replace(tzinfo=None))
        .all()
    )
    buckets = {start + timedelta(hours=i): {"volume": 0.0, "count": 0}
               for i in range(hours)}
    for traded_at, amount in rows:
        if traded_at is None:
            continue
        key = traded_at.replace(minute=0, second=0, microsecond=0,
                                tzinfo=timezone.utc)
        if key in buckets:
            buckets[key]["volume"] += amount or 0
            buckets[key]["count"] += 1
    return [
        {"label": f"{k.hour:02d}", "volume": v["volume"], "count": v["count"]}
        for k, v in sorted(buckets.items())
    ]


@app.route("/")
def index():
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503

    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    wallet = request.args.get("wallet", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    page_size = 100

    with Session() as session:
        query = session.query(WhaleTrade).order_by(WhaleTrade.traded_at.desc())
        if q:
            query = query.filter(WhaleTrade.market_title.ilike(f"%{q}%"))
        if category:
            query = query.filter(WhaleTrade.category == category)
        if wallet:
            query = query.filter(WhaleTrade.wallet == wallet.lower())
        total_count = query.order_by(None).count()
        trades = query.offset((page - 1) * page_size).limit(page_size).all()

        categories = [
            c[0] for c in session.query(WhaleTrade.category)
            .filter(WhaleTrade.category != "").distinct().order_by(WhaleTrade.category)
        ]
        wallets = (
            session.query(Wallet).order_by(Wallet.total_usd.desc()).limit(20).all()
        )
        wallet_records = {w.address: db.wallet_record(session, w.address) for w in wallets}
        watched_set = {a.address for a in db.get_watched_addresses(session)}
        stats = db.get_stats(session)
        hourly = hourly_volume(session)
        tracker = db.tracker_health(session)

    hourly_max = max((h["volume"] for h in hourly), default=0)
    total_pages = (total_count + page_size - 1) // page_size
    pager_qs = "".join(f"{k}={v}&" for k, v in
                       (("q", q), ("category", category), ("wallet", wallet)) if v)
    return render_template_string(
        TEMPLATE,
        stats=type("S", (), stats),
        trades=trades,
        wallets=wallets,
        wallet_records=wallet_records,
        watched_set=watched_set,
        categories=categories,
        hourly=hourly,
        hourly_max=hourly_max,
        q=q, category=category, wallet=wallet,
        page=page, total_pages=total_pages, has_next=page < total_pages,
        pager_qs=pager_qs,
        tracker=tracker,
    )


ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}$")
CONDITION_ID_RE = re.compile(r"0x[0-9a-fA-F]+$")


@app.route("/markets")
def markets_page():
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503
    with Session() as session:
        markets = db.markets_index(session)
        tracker = db.tracker_health(session)
    return render_template_string(MARKETS_TEMPLATE, markets=markets, tracker=tracker)


@app.route("/market/<condition_id>")
def market_page(condition_id):
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503
    condition_id = condition_id.strip().lower()
    if not CONDITION_ID_RE.fullmatch(condition_id):
        return "Invalid market condition ID.", 404
    with Session() as session:
        market = session.get(Market, condition_id)
        if market is None:
            return "Market not found — no trades recorded for this condition ID.", 404
        participants = db.market_participants(session, condition_id)
        tracker = db.tracker_health(session)
    return render_template_string(
        MARKET_TEMPLATE, market=market, participants=participants, tracker=tracker,
    )


@app.route("/leaderboard")
def leaderboard():
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503
    days = 30 if request.args.get("days") == "30" else 7
    with Session() as session:
        board = db.wallet_leaderboard(session, days=days)
        watched = {a.address for a in db.get_watched_addresses(session)}
        records = {w["address"]: db.wallet_record(session, w["address"]) for w in board}
        tracker = db.tracker_health(session)
    return render_template_string(
        LEADERBOARD_TEMPLATE, board=board, days=days, watched=watched,
        records=records, tracker=tracker,
    )


@app.route("/wallet/<address>")
def wallet_page(address):
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503
    address = address.strip().lower()
    if not ADDRESS_RE.fullmatch(address):
        return "Invalid wallet address.", 404

    with Session() as session:
        w = session.get(Wallet, address)
        watch = session.get(WatchedAddress, address)
        markets = db.wallet_market_breakdown(session, address)
        trades = (
            session.query(WhaleTrade)
            .filter(WhaleTrade.wallet == address)
            .order_by(WhaleTrade.traded_at.desc())
            .limit(100)
            .all()
        )
        record = db.wallet_record(session, address)
        db_positions = db.get_wallet_positions(session, address)
        tracker = db.tracker_health(session)

    if db_positions:
        # Synced by sync_positions.py — no live API call needed.
        positions = [_normalize_position(p) for p in db_positions]
        positions_synced_at = db_positions[0].synced_at
    elif watch is None:
        # Unwatched wallet the sync job doesn't cover — live call is the
        # only option, and per-wallet pages are low-traffic enough to afford it.
        positions = fetch_positions(address)
        positions_synced_at = None
    else:
        # Watched, but sync_positions.py hasn't run yet.
        positions = []
        positions_synced_at = None

    return render_template_string(
        WALLET_TEMPLATE,
        address=address,
        tag=w.tag if w else "",
        trade_count=w.trade_count if w else len(trades),
        total_usd=w.total_usd if w else sum(t.amount_usd or 0 for t in trades),
        first_seen=w.first_seen if w else None,
        last_seen=w.last_seen if w else None,
        watch_label=(watch.label or "") if watch else None,
        markets=markets,
        trades=trades,
        record=record,
        positions=positions,
        positions_synced_at=positions_synced_at,
        tracker=tracker,
    )


@app.route("/watchlist")
def watchlist():
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503

    with Session() as session:
        addresses = db.get_watched_addresses(session)
        stats = db.watched_address_stats(session)
        convergence = db.market_convergence(session)
        tracker = db.tracker_health(session)
        recent = (
            session.query(WatchedTrade)
            .join(WatchedAddress, WatchedAddress.address == WatchedTrade.address)
            .order_by(WatchedTrade.traded_at.desc())
            .limit(50)
            .all()
        )
        # Position value / unrealized P&L per watched wallet, read from the
        # table sync_positions.py keeps fresh — a single grouped query
        # instead of one live API call per wallet (that's what made this
        # page lag as the watchlist grew).
        pnl = db.positions_summary_db(session, [a.address for a in addresses])
        # Win/loss record + realized P&L from settled trades, per watched wallet
        records = {a.address: db.wallet_record(session, a.address) for a in addresses}
        unwatched = db.unwatched_top_wallets(session, limit=10)

    positions_synced_at = max(
        (v["synced_at"] for v in pnl.values() if v.get("synced_at")), default=None)
    if positions_synced_at is None:
        positions_synced_note = "Positions not synced yet — run python sync_positions.py"
    else:
        age = positions_synced_at
        if age.tzinfo is None:
            age = age.replace(tzinfo=timezone.utc)
        mins = int((datetime.now(timezone.utc) - age).total_seconds() / 60)
        positions_synced_note = f"Positions synced {mins} min ago"

    # Friendly display names: label if set, else shortened address
    labels = {
        a.address: (a.label or f"{a.address[:6]}…{a.address[-4:]}")
        for a in addresses
    }
    for m in convergence:
        for p in m["positions"]:
            p["names"] = ", ".join(
                labels.get(w, f"{w[:6]}…{w[-4:]}") for w in p["wallets"]
            )

    return render_template_string(
        WATCHLIST_TEMPLATE,
        addresses=addresses,
        stats=stats,
        convergence=convergence,
        recent=recent,
        labels=labels,
        pnl=pnl,
        records=records,
        unwatched=unwatched,
        positions_synced_note=positions_synced_note,
        error=request.args.get("error", ""),
        tracker=tracker,
    )


@app.route("/watchlist/add", methods=["POST"])
def watchlist_add():
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503
    address = request.form.get("address", "").strip().lower()
    label = request.form.get("label", "").strip()
    if not ADDRESS_RE.fullmatch(address):
        return redirect("/watchlist?error=Invalid+address+—+expected+a+full+0x…+wallet+(42+chars)")
    with Session() as session:
        db.add_watched_address(session, address, label)
    return redirect("/watchlist")


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove():
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503
    address = request.form.get("address", "").strip().lower()
    with Session() as session:
        db.remove_watched_address(session, address)
    return redirect("/watchlist")


def _iso(dt):
    """None-safe datetime -> ISO 8601 string, for JSON responses."""
    return dt.isoformat() if dt else None


@app.route("/api/stats")
def api_stats():
    if Session is None:
        return jsonify({"error": "Database unavailable"}), 503
    with Session() as session:
        stats = db.get_stats(session)
        tracker = db.tracker_health(session)
    return jsonify({
        **stats,
        "tracker_alive": tracker["alive"],
        "tracker_last_poll": _iso(tracker["last_poll_at"]),
    })


@app.route("/api/trades")
def api_trades():
    if Session is None:
        return jsonify({"error": "Database unavailable"}), 503
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    wallet = request.args.get("wallet", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    limit = min(500, request.args.get("limit", 100, type=int))

    with Session() as session:
        query = session.query(WhaleTrade).order_by(WhaleTrade.traded_at.desc())
        if q:
            query = query.filter(WhaleTrade.market_title.ilike(f"%{q}%"))
        if category:
            query = query.filter(WhaleTrade.category == category)
        if wallet:
            query = query.filter(WhaleTrade.wallet == wallet.lower())
        total = query.order_by(None).count()
        trades = query.offset((page - 1) * limit).limit(limit).all()

    return jsonify({
        "page": page, "limit": limit, "total": total,
        "trades": [
            {
                "trade_id": t.trade_id, "condition_id": t.condition_id,
                "market_title": t.market_title, "category": t.category,
                "side": t.side, "price": t.price, "amount_usd": t.amount_usd,
                "wallet": t.wallet, "traded_at": _iso(t.traded_at),
                "result": t.result or "",
            }
            for t in trades
        ],
    })


@app.route("/api/watchlist")
def api_watchlist():
    if Session is None:
        return jsonify({"error": "Database unavailable"}), 503
    with Session() as session:
        addresses = db.get_watched_addresses(session)
        stats = db.watched_address_stats(session)
        pnl = db.positions_summary_db(session, [a.address for a in addresses])
        records = {a.address: db.wallet_record(session, a.address) for a in addresses}

    return jsonify({
        "watchlist": [
            {
                "address": a.address, "label": a.label or "",
                "added_at": _iso(a.added_at),
                "trades": stats.get(a.address, {}).get("trades", 0),
                "volume": stats.get(a.address, {}).get("volume", 0.0),
                "last_traded": _iso(stats.get(a.address, {}).get("last_traded")),
                "open_value": pnl.get(a.address, {}).get("value", 0.0),
                "unrealized_pnl": pnl.get(a.address, {}).get("pnl", 0.0),
                "positions_synced_at": _iso(pnl.get(a.address, {}).get("synced_at")),
                "wins": records.get(a.address, {}).get("wins", 0),
                "losses": records.get(a.address, {}).get("losses", 0),
                "win_rate": records.get(a.address, {}).get("win_rate"),
                "realized_pnl": records.get(a.address, {}).get("realized_pnl", 0.0),
            }
            for a in addresses
        ],
    })


@app.route("/api/market/<condition_id>")
def api_market(condition_id):
    if Session is None:
        return jsonify({"error": "Database unavailable"}), 503
    condition_id = condition_id.strip().lower()
    if not CONDITION_ID_RE.fullmatch(condition_id):
        return jsonify({"error": "Invalid market condition ID"}), 404
    with Session() as session:
        market = session.get(Market, condition_id)
        if market is None:
            return jsonify({"error": "Market not found"}), 404
        participants = db.market_participants(session, condition_id)

    def _participant(p):
        return {**p, "last_traded": _iso(p["last_traded"])}

    return jsonify({
        "condition_id": market.condition_id, "title": market.title,
        "category": market.category, "resolved": bool(market.resolved),
        "winning_outcome": market.winning_outcome,
        "end_date": _iso(market.end_date),
        "watched": [_participant(p) for p in participants["watched"]],
        "unknown": [_participant(p) for p in participants["unknown"]],
    })


@app.route("/health")
def health():
    result = {"ok": True, "tracker_alive": None, "tracker_last_poll": None}
    if Session:
        try:
            with Session() as session:
                t = db.tracker_health(session)
            result["tracker_alive"] = t["alive"]
            if t["last_poll_at"]:
                result["tracker_last_poll"] = t["last_poll_at"].isoformat()
        except Exception:
            result["ok"] = False
    return result


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
