"""nft-scout MCP server (keybot): find artists whose primary mints resell >=20% above all-in cost within
14 days, and flag their live mints that fit a 0.1 ETH budget. Read-only: it never signs, mints or sells."""

from __future__ import annotations

import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import analyze, notify, opensea, report, scan, store

INSTRUCTIONS = """\
keybot's primary-drop scout for ETH L1 + L2s (Base, Shape, Zora, Arbitrum, Optimism, Abstract).
Goal: mint from artists whose drops reliably resell >=1.2x the all-in mint cost (mint + gas), net of
required marketplace fees and sell gas, within 14 days; total budget 0.1 ETH, editions or cheap PFPs/1-1s.
It reads OpenSea + public RPCs only. It never signs, mints or sells: the user taps the link and mints.

Workflow:
1. scan_artists (slow the first time, 3-10 min; cached afterwards). Shows top 5 with per-drop evidence.
2. live_drops: mints from the top artists in the last 6h + OpenSea upcoming drops; fits_budget flag.
3. notify_opportunities pings the user's phone (ntfy) for budget-fitting live mints, deduped.
4. After the user mints: add_position. After they sell: close_position (keeps budget + real hit rate).
Be honest about thin data: 'low (1 drop)' confidence means one lucky drop, not a pattern.
"""

mcp = MCPServer("nft-scout", instructions=INSTRUCTIONS)


def _brief(a: dict, drops: int = 6) -> dict:
    keep = ("slug", "name", "token", "chain", "start", "mint_all_in", "median_sale", "median_exit", "floor_now",
            "floor_vs_mint", "premium", "hit_rate",
            "sales_14d", "unique_buyers", "first_resale_h", "trend", "verdict", "minting_now", "complete")
    return {**{k: v for k, v in a.items() if k != "drops"},
            "drops": [{k: d.get(k) for k in keep} for d in a["drops"][:drops]]}


@mcp.tool()
def scan_artists(per_chain: int = 300, max_artists: int = 30, use_cached_minutes: int = 180) -> dict:
    """Rank creators by their primary-to-secondary record. Reuses the last scan if it is younger than
    use_cached_minutes (0 forces a fresh scan). Returns the top artists with their latest drops."""
    last = store.last_scan()
    if last and use_cached_minutes and time.time() - last["at"] < use_cached_minutes * 60:
        s = last
    else:
        s = scan.run_scan(per_chain, max_artists)
    path = report.write(s, scan.live_drops(s) if s.get("top5") or s.get("top5_art") else [])
    q = [a for a in s["artists"] if a["qualifies"]]
    art = [a for a in q if a.get("kind") == "art"]
    q = [a for a in q if a.get("kind", "pfp") == "pfp"]
    return {"scanned_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(s["at"])), "took_s": s["took_s"],
            "top5": [_brief(a) for a in q[:5]],
            "top5_art": [_brief(a) for a in art[:5]],
            "near_misses": [{k: a[k] for k in ("name", "score", "consistency", "median_premium", "drops_judged")}
                            for a in s["artists"] if not a["qualifies"]][:8],
            "report": str(path)}


@mcp.tool()
def artist_shortlist(n: int = 10, days: int = 90) -> dict:
    """Art-side creators (art/photography/editions) with a paid primary <= max_mint_eth in the last `days` that
    has real secondary sales, qualifying or not: candidates for watch_artists."""
    return {"artists": scan.shortlist(n=n, days=days)}


@mcp.tool()
def artist_report(name_or_address: str) -> dict:
    """Full per-drop history for one artist (OpenSea username or owner address), scored."""
    cfg = store.config()
    for a in store.last_scan().get("artists", []):
        if name_or_address.lower() in ((a["name"] or "").lower(), (a["owner"] or "")):
            return _brief(a, drops=50)
    slugs = [c["collection"] for c in opensea.creator_collections(name_or_address, pages=1)[:25]]
    drops = [d for s in slugs for d in analyze.analyze_collection(s, cfg)]
    drops.sort(key=lambda d: d["start"], reverse=True)
    return _brief({"name": name_or_address, "owner": None, **analyze.score_artist(drops, cfg), "drops": drops}, 50)


@mcp.tool()
def collection_check(slug: str) -> dict:
    """Score one collection's drop(s) on demand (e.g. a mint link the user found)."""
    return {"drops": analyze.analyze_collection(slug, store.config()), "best_offer": opensea.best_collection_offer(slug),
            "stats": opensea.stats(slug)}


@mcp.tool()
def live_drops(top_n: int = 5) -> dict:
    """Live mints (last 6h) + upcoming OpenSea drops from the top_n artists of the last scan."""
    cfg = store.config()
    return {"budget_left": scan.budget_left(cfg), "drops": scan.live_drops(only_top=top_n)}


@mcp.tool()
def notify_opportunities(top_n: int = 5, test: bool = False) -> dict:
    """Ping the phone (ntfy) for each budget-fitting live mint not pinged before. test=True sends one test ping."""
    if test:
        return notify.ping("keybot scout", "test ping .. nft-scout is wired to this topic", tags="robot")
    sent = scan.notify_live(top_n)
    return {"pings": sent, "note": None if sent else "no budget-fitting live mints from top artists right now"}


@mcp.tool()
def add_position(slug: str, cost_eth: float, quantity: int = 1, token: str | None = None, note: str = "") -> dict:
    """Record a mint the user made (cost_eth = total all-in for the quantity). Reduces budget_left."""
    p = store.positions()
    p.append({"id": len(p) + 1, "slug": slug, "token": token, "quantity": quantity, "cost_eth": cost_eth,
              "opened": time.strftime("%Y-%m-%d"), "status": "open", "note": note})
    store.save_positions(p)
    return {"position": p[-1], "budget_left": scan.budget_left(store.config())}


@mcp.tool()
def close_position(position_id: int, proceeds_eth: float) -> dict:
    """Record the sale (net ETH received). Keeps a real hit rate for the scout itself."""
    p = store.positions()
    for x in p:
        if x["id"] == position_id:
            x.update(status="closed", closed=time.strftime("%Y-%m-%d"), proceeds_eth=proceeds_eth,
                     multiple=round(proceeds_eth / x["cost_eth"], 2) if x["cost_eth"] else None)
    store.save_positions(p)
    return portfolio()


@mcp.tool()
def portfolio() -> dict:
    """Open/closed positions, budget left and realised results."""
    p = store.positions()
    closed = [x for x in p if x["status"] == "closed"]
    return {"budget_left": scan.budget_left(store.config()), "open": [x for x in p if x["status"] == "open"],
            "closed": closed, "realised_pnl_eth": round(sum(x["proceeds_eth"] - x["cost_eth"] for x in closed), 6),
            "hit_rate": round(sum(x.get("multiple", 0) >= 1.2 for x in closed) / len(closed), 2) if closed else None}


@mcp.tool()
def follow_artists(add: list[str] | None = None, remove: list[str] | None = None) -> dict:
    """Artists whose every new mint pings the phone (OpenSea usernames or addresses). Kept private."""
    cur = [a for a in store.config().get("follow_artists", []) if a not in (remove or [])]
    cur += [a for a in (add or []) if a not in cur]
    return {"follow_artists": store.update_config({"follow_artists": cur})["follow_artists"]}


@mcp.tool()
def get_config() -> dict:
    """Budget, thresholds, chains, watch list."""
    return store.config()


@mcp.tool()
def update_config(patch: dict[str, Any]) -> dict:
    """Change settings, e.g. {"watch_artists": ["someartist"]} or {"min_premium": 1.3}."""
    return store.update_config(patch)


def main() -> None:
    mcp.run()
