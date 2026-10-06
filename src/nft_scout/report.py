"""HTML scan report in D:\\nft-scout\\reports (top artists, their drops, live mints)."""

from __future__ import annotations

from . import scan as scan_mod

import html
import time
from pathlib import Path

from . import store

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e3e3de;--hit:#1f7a4d;--miss:#b23b3b;--acc:#3a5bd9}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--mut:#9a9993;--card:#1d1d1b;--line:#33332f;
--hit:#5cc48d;--miss:#e07272;--acc:#8ea5ff}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1100px;margin:auto}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
td:first-child,th:first-child{text-align:left}th{color:var(--mut);font-weight:500}a{color:var(--acc)}
.hit{color:var(--hit);font-weight:600}.miss{color:var(--miss)}.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px;margin-left:6px}
"""


def _f(x, nd=4):
    return "–" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else html.escape(str(x)))


def write(scan: dict, live: list[dict]) -> Path:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(scan["at"]))
    q = scan_mod.top_artists(scan, 5)
    parts = [f"<h1>nft scout</h1><div class=mut>scan {when} · {scan['took_s']}s · rule: net resale ≥ "
             f"{scan['config']['min_premium']}× all-in mint within {scan['config']['hold_days']}d · budget "
             f"{scan['config']['budget_eth']} eth</div>"]
    parts.append("<h2>live mints from top artists</h2><div class=card>")
    if live:
        parts.append("<table><tr><th>drop</th><th>artist</th><th>chain</th><th>all-in</th><th>exit target</th>"
                     "<th>best offer</th><th>mints 6h</th><th>fits</th></tr>")
        for o in live:
            name = html.escape(o["name"] or o["slug"]) + (f" #{o['token']}" if o.get("token") else "")
            link = o.get("project_url") or o["opensea_url"]
            parts.append(f"<tr><td><a href='{html.escape(link)}'>{name}</a></td><td>{html.escape(o['artist'])}</td>"
                         f"<td>{o['chain']}</td><td>{_f(o.get('all_in'))}</td><td>{_f(o.get('target_exit'))}</td>"
                         f"<td>{_f(o.get('best_offer'))}</td><td>{_f(o.get('mints_6h'))}</td>"
                         f"<td>{'upcoming' if o.get('upcoming') else ('yes' if o.get('fits_budget') else 'no')}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<span class=mut>nothing minting from the top artists in the last 6h</span>")
    parts.append("</div><h2>top artists</h2>")
    for i, a in enumerate(q, 1):
        parts.append(f"<div class=card><b>{i}. {html.escape(a['name'])}</b><span class=pill>score {a['score']}</span>"
                     f"<span class=pill>{a['hits']}/{a['drops_judged']} drops hit</span><span class=pill>median "
                     f"{a['median_premium']}× net</span><span class=pill>{a['confidence']}</span>"
                     "<table><tr><th>drop</th><th>chain</th><th>start</th><th>all-in</th><th>median sale</th>"
                     "<th>net ×</th><th>hit %</th><th>sales</th><th>buyers</th><th>1st resale h</th><th>trend</th>"
                     "<th>verdict</th></tr>")
        for d in a["drops"][:12]:
            name = html.escape(d["name"] or d["slug"]) + (f" #{d['token']}" if d.get("token") else "")
            cls = "hit" if d["verdict"] == "hit" else "miss" if d["verdict"] == "miss" else "mut"
            parts.append(f"<tr><td><a href='{html.escape(d['opensea_url'] or '')}'>{name}</a></td><td>{d['chain']}</td>"
                         f"<td>{d['start']}</td><td>{_f(d.get('mint_all_in'))}</td><td>{_f(d.get('median_sale'))}</td>"
                         f"<td>{_f(d.get('premium'), 2)}</td><td>{_f(d.get('hit_rate'), 2)}</td><td>{d['sales_14d']}</td>"
                         f"<td>{d['unique_buyers']}</td><td>{_f(d.get('first_resale_h'), 1)}</td><td>{_f(d.get('trend'), 2)}</td>"
                         f"<td class={cls}>{d['verdict']}</td></tr>")
        parts.append("</table></div>")
    doc = (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
           f"initial-scale=1'><title>NFT Scout</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>")
    path = store.REPORTS / f"scan_{time.strftime('%Y-%m-%d_%H%M', time.localtime(scan['at']))}.html"
    path.write_text(doc, "utf-8")
    return path
