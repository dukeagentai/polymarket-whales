#!/usr/bin/env python3
"""
🐋 Polymarket Whale Tracker — web dashboard.

Simple Flask UI over the whale database (SQLite locally, Postgres on Railway).

    python dashboard.py               # dev server on :8000
    gunicorn dashboard:app            # production (Railway web service)
"""

import os
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template_string, request, redirect

import db
from db import WhaleTrade, Wallet, WatchedAddress, WatchedTrade

app = Flask(__name__)

Session = db.init_db()

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
  .muted { color: var(--muted); font-size: 12px; }
  form.inline { display: inline; }
  .poslist { margin: 0; padding: 0; list-style: none; }
  .poslist li { margin: 2px 0; }
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
  <nav><a href="/">🐋 Live feed</a><a href="/watchlist">👀 Watchlist</a></nav>
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
        <th class="num">Amount</th><th class="num">Price</th><th>Wallet</th>
      </tr></thead>
      <tbody>
      {% for t in trades %}
      <tr>
        <td class="num">{{ t.traded_at.strftime("%m-%d %H:%M") if t.traded_at else "—" }}</td>
        <td>{{ t.market_title }}</td>
        <td>{{ t.category or "—" }}</td>
        <td class="{{ 'yes' if t.side == 'YES' else 'no' }}">{{ t.side }}</td>
        <td class="num">${{ "{:,.0f}".format(t.amount_usd) }}</td>
        <td class="num">{{ "%.3f" | format(t.price) }}</td>
        <td class="addr">{{ t.wallet[:6] ~ "…" ~ t.wallet[-4:] if t.wallet else "—" }}</td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">No whale trades yet — start the tracker: <code>python main.py</code></td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Top whale wallets</h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Wallet</th><th>Tag</th><th class="num">Trades</th>
        <th class="num">Total volume</th><th>Last seen (UTC)</th>
      </tr></thead>
      <tbody>
      {% for w in wallets %}
      <tr>
        <td class="addr"><a href="?wallet={{ w.address }}" style="color: inherit">{{ w.address[:10] ~ "…" ~ w.address[-6:] }}</a></td>
        <td class="tag">{{ w.tag or "—" }}</td>
        <td class="num">{{ w.trade_count }}</td>
        <td class="num">${{ "{:,.0f}".format(w.total_usd) }}</td>
        <td class="num">{{ w.last_seen.strftime("%m-%d %H:%M") if w.last_seen else "—" }}</td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="empty">No wallets tracked yet.</td></tr>
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
        <th class="num">Volume</th><th>Last trade (UTC)</th><th>Added</th><th></th>
      </tr></thead>
      <tbody>
      {% for a in addresses %}
      {% set s = stats.get(a.address, {}) %}
      <tr>
        <td class="addr">{{ a.address }}</td>
        <td class="tag">{{ a.label or "—" }}</td>
        <td class="num">{{ s.get("trades", 0) }}</td>
        <td class="num">${{ "{:,.0f}".format(s.get("volume", 0)) }}</td>
        <td class="num">{{ s["last_traded"].strftime("%m-%d %H:%M") if s.get("last_traded") else "—" }}</td>
        <td class="num">{{ a.added_at.strftime("%Y-%m-%d") if a.added_at else "—" }}</td>
        <td><form class="inline" method="post" action="/watchlist/remove">
          <input type="hidden" name="address" value="{{ a.address }}">
          <button class="btn-danger" type="submit">Remove</button>
        </form></td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">No watched addresses yet — paste a wallet above to start tracking it.</td></tr>
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
        <td>{{ m.title }}{% if m.consensus %}<span class="badge">⚡ ALL SAME SIDE</span>{% endif %}</td>
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
    <h2>Recent trades by watched wallets</h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Time (UTC)</th><th>Wallet</th><th>Market</th><th>Position</th>
        <th class="num">Amount</th><th class="num">Price</th>
      </tr></thead>
      <tbody>
      {% for t in recent %}
      <tr>
        <td class="num">{{ t.traded_at.strftime("%m-%d %H:%M") if t.traded_at else "—" }}</td>
        <td class="addr">{{ labels.get(t.address, t.address[:6] ~ "…" ~ t.address[-4:]) }}</td>
        <td>{{ t.market_title }}</td>
        <td><span class="{{ 'yes' if (t.outcome or '').upper() == 'YES' else ('no' if (t.outcome or '').upper() == 'NO' else '') }}">{{ t.outcome }} ({{ t.side }})</span></td>
        <td class="num">${{ "{:,.2f}".format(t.amount_usd) }}</td>
        <td class="num">{{ "%.3f" | format(t.price) }}</td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="empty">Nothing yet.</td></tr>
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

    with Session() as session:
        query = session.query(WhaleTrade).order_by(WhaleTrade.traded_at.desc())
        if q:
            query = query.filter(WhaleTrade.market_title.ilike(f"%{q}%"))
        if category:
            query = query.filter(WhaleTrade.category == category)
        if wallet:
            query = query.filter(WhaleTrade.wallet == wallet.lower())
        trades = query.limit(100).all()

        categories = [
            c[0] for c in session.query(WhaleTrade.category)
            .filter(WhaleTrade.category != "").distinct().order_by(WhaleTrade.category)
        ]
        wallets = (
            session.query(Wallet).order_by(Wallet.total_usd.desc()).limit(20).all()
        )
        stats = db.get_stats(session)
        hourly = hourly_volume(session)

    hourly_max = max((h["volume"] for h in hourly), default=0)
    return render_template_string(
        TEMPLATE,
        stats=type("S", (), stats),
        trades=trades,
        wallets=wallets,
        categories=categories,
        hourly=hourly,
        hourly_max=hourly_max,
        q=q, category=category, wallet=wallet,
    )


ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}$")


@app.route("/watchlist")
def watchlist():
    if Session is None:
        return "Database unavailable — check DATABASE_URL.", 503

    with Session() as session:
        addresses = db.get_watched_addresses(session)
        stats = db.watched_address_stats(session)
        convergence = db.market_convergence(session)
        recent = (
            session.query(WatchedTrade)
            .join(WatchedAddress, WatchedAddress.address == WatchedTrade.address)
            .order_by(WatchedTrade.traded_at.desc())
            .limit(50)
            .all()
        )

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
        error=request.args.get("error", ""),
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


@app.route("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
