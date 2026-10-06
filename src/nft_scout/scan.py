"""Scan: trending collections per chain -> creators -> every drop each creator made in the lookback
(flops included) -> top artists -> their live/upcoming mints that fit the budget."""

from __future__ import annotations

import os
import time
from collections import defaultdict

from . import analyze, chain, follow, notify, opensea, store, wallet
from .analyze import DAY, log


def _username(addr: str) -> str | None:
    a = opensea.account(addr)
    return (a.get("username") or None) if not a.get("_error") else None


def _creator_slugs(name: str) -> list[str]:
    try:
        return [c["collection"] for c in opensea.creator_collections(name, pages=1)[:25]]
    except RuntimeError as e:
        log(f"creator skip: {e}")
        return []


def candidates(cfg: dict, per_chain: int = 300, mode: str = "pfp") -> list[str]:
    """Trending collections created inside the lookback (= recent primaries that now trade).
    mode "art" digs deeper into the volume ranking (OpenSea ignores a category filter) and keeps only
    art/photography/uncategorised collections from the last art_lookback_days."""
    slugs: list[str] = []
    art = mode == "art"
    lb = time.time() - (cfg["art_lookback_days"] if art else cfg["lookback_days"]) * DAY
    cats = cfg["art_categories"] if art else [x or "" for x in cfg["categories"]]
    pages = cfg["art_pages"] if art else 3
    for ch in cfg["chains"]:
        for order in ("seven_day_volume", "one_day_volume"):
            for c in opensea.top_collections(ch, order, pages=pages)[:per_chain * (3 if art else 1)]:
                if c["collection"] in cfg["ignore_slugs"] or c.get("is_disabled") or c.get("is_nsfw"):
                    continue
                if (c.get("category") or "").lower() not in cats:
                    continue
                if c["collection"] not in slugs:
                    slugs.append(c["collection"])
    # creation date needs the per-collection lookup (cached a day); thousands on the art pass, so in parallel
    infos = analyze.parallel(analyze.coll_info, slugs, 10)
    return [s for s, i in zip(slugs, infos) if i and (i["created_ts"] or 0) >= lb]


def _floor(slug: str) -> tuple[float, str, int] | None:
    """(floor, symbol, 30d sales) or None on error."""
    s = opensea.stats(slug)
    if s.get("_error"):
        return None
    m30 = next((i for i in s.get("intervals", []) if i["interval"] == "thirty_day"), {})
    tot = s.get("total") or {}
    return tot.get("floor_price") or 0, (tot.get("floor_price_symbol") or "ETH").upper(), m30.get("sales", 0)


def _cheap(slug: str, cfg: dict) -> bool:
    f = _floor(slug)
    return bool(f) and f[1] in ("ETH", "WETH") and f[0] <= cfg["max_floor_eth"]


def _prefilter(slug: str, cfg: dict, min_sales: int = 15) -> bool:
    f = _floor(slug)
    return bool(f) and f[2] >= min_sales and f[1] in ("ETH", "WETH") and f[0] <= cfg["max_floor_eth"]


def _kind(drops: list[dict]) -> str:
    """'art' when most drops are art/photography collections or 1155 editions, else 'pfp'."""
    if not drops:
        return "pfp"
    arty = sum(1 for d in drops if d.get("token") is not None
               or (d.get("category") or "") in ("art", "photography"))
    return "art" if arty * 2 >= len(drops) else "pfp"


def _rank(slugs: list[str], cfg: dict, max_artists: int, min_sales: int, skip: set) -> list[dict]:
    """Creators behind `slugs` -> quick score on those collections -> full history for the best max_artists."""
    keep = [s for s, ok in zip(slugs, analyze.parallel(lambda x: _prefilter(x, cfg, min_sales), slugs, 8)) if ok]
    log(f"{len(keep)} liquid + floor <= {cfg['max_floor_eth']} ETH")
    infos = [i for i in analyze.parallel(analyze.coll_info, keep, 8) if i]
    by_owner: dict[str, list[str]] = defaultdict(list)
    for i in infos:
        if i["owner"] and i["owner"] not in skip:
            by_owner[i["owner"]].append(i["slug"])
    # platform-style owners (one contract owner, many unrelated projects) can't be scored as one artist
    owners = [o for o, s in by_owner.items() if len(s) <= 8]
    first = {o: [d for s in by_owner.get(o, []) for d in analyze.analyze_collection(s, cfg)] for o in owners}

    def quick(o):   # any judged paid drop first, then score, then secondary activity
        sc = analyze.score_artist(first[o], cfg)
        return (-(sc["drops_judged"] > 0), -sc["score"], -sum(d["sales_14d"] for d in first[o]))

    ranked = sorted(owners, key=quick)[:max_artists]
    log(f"{len(ranked)} creators -> full drop history")
    return [_artist(o, set(by_owner.get(o, [])), cfg) for o in ranked]


def _artist(owner: str | None, slugs_o: set, cfg: dict, name: str | None = None) -> dict:
    name = name or (_username(owner) if owner else None)
    if name:
        slugs_o |= set(_creator_slugs(name))
    slugs_o = {x for x, ok in zip(sorted(slugs_o), analyze.parallel(lambda x: _cheap(x, cfg), sorted(slugs_o), 8))
               if ok}
    drops = [d for s in analyze.parallel(lambda s: analyze.analyze_collection(s, cfg), sorted(slugs_o), 4)
             for d in s]
    drops.sort(key=lambda d: d["start"], reverse=True)
    return {"owner": owner, "name": name or (owner or "?")[:10], "kind": _kind(drops),
            **analyze.score_artist(drops, cfg), "drops": drops}


def _order(a: dict) -> tuple:
    return (-a["qualifies"], -min(a["drops_judged"], 3), -a["score"], -a["median_sales_14d"])


def run_scan(per_chain: int = 300, max_artists: int = 30, cfg: dict | None = None) -> dict:
    cfg = cfg or store.config()
    t = time.time()
    pfp = candidates(cfg, per_chain, "pfp")
    log(f"{len(pfp)} trending collections (pfp pass)")
    artists = _rank(pfp, cfg, max_artists, 15, set())
    seen = {a["owner"] for a in artists}
    art = [s for s in candidates(cfg, per_chain, "art") if s not in pfp]
    log(f"{len(art)} trending collections (art pass)")
    artists += _rank(art, cfg, max_artists, cfg["art_min_sales_30d"], seen)
    known = {(a["name"] or "").lower() for a in artists} | {a["owner"] for a in artists}
    for w in cfg["watch_artists"]:
        if w.lower() not in known:
            artists.append(_artist(w.lower(), set(), cfg) if w.startswith("0x") else _artist(None, set(), cfg, w))

    artists.sort(key=_order)
    q = [a for a in artists if a["qualifies"]]
    scan = {"at": int(time.time()), "took_s": round(time.time() - t), "config": cfg, "artists": artists,
            "top5": [a["name"] for a in q if a["kind"] == "pfp"][:5],
            "top5_art": [a["name"] for a in q if a["kind"] == "art"][:5]}
    store.save_scan(scan)
    log(f"scan done in {scan['took_s']}s; {len(q)} qualifying artists", public=True)
    return scan


def top_artists(scan: dict, n: int = 5) -> list[dict]:
    """Qualifying top n of each list (pfp + art)."""
    q = [a for a in scan.get("artists", []) if a["qualifies"]]
    return [a for a in q if a.get("kind", "pfp") == "pfp"][:n] + [a for a in q if a.get("kind") == "art"][:n]


def shortlist(scan: dict | None = None, n: int = 10, days: int = 90) -> list[dict]:
    """Art-side creators with a paid primary <= max_mint_eth in the last `days` that has real secondary sales,
    qualifying or not, for the user to pick watch_artists from."""
    scan = scan or store.last_scan()
    cfg = store.config()
    cut = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * DAY))
    keep = ("name", "chain", "start", "token", "mint_all_in", "median_sale", "premium", "sales_14d",
            "unique_buyers", "verdict", "opensea_url")
    out = []
    for a in scan.get("artists", []):
        if a.get("kind") != "art":
            continue
        paid = [d for d in a["drops"] if d["start"] >= cut and d.get("mint_value")
                and (d.get("mint_all_in") or 0) <= cfg["max_mint_eth"] and d["sales_14d"] >= cfg["min_sales_14d"]
                and d["verdict"] in ("hit", "miss")]
        if paid:
            out.append({**{k: a[k] for k in ("name", "owner", "score", "qualifies", "consistency",
                                            "median_premium", "drops_judged", "confidence")},
                        "recent_paid_drops": [{k: d.get(k) for k in keep} for d in paid[:3]]})
    out.sort(key=lambda a: (-a["qualifies"], -sum(d["verdict"] == "hit" for d in a["recent_paid_drops"]),
                            -sum(d["sales_14d"] for d in a["recent_paid_drops"])))
    return out[:n]


def budget_left(cfg: dict) -> float:
    open_cost = sum(p["cost_eth"] for p in store.positions() if p.get("status") == "open")
    return round(cfg["budget_eth"] - open_cost, 6)


def live_drops(scan: dict | None = None, only_top: int = 5, cfg: dict | None = None) -> list[dict]:
    """Mints happening in the last 6h (plus OpenSea upcoming drops) from the top-scored artists."""
    cfg = cfg or store.config()
    scan = scan or store.last_scan()
    if not scan:
        return []
    top = top_artists(scan, only_top)
    now = int(time.time())
    left = budget_left(cfg)
    cap = min(left, cfg["max_per_position_eth"])
    out = []
    for a in top:
        slugs = {d["slug"] for d in a["drops"]}
        if a["name"] and not a["name"].startswith("0x"):
            slugs |= set(_creator_slugs(a["name"]))
        for slug in slugs:
            info = analyze.coll_info(slug)
            if not info or info["chain"] not in cfg["chains"] or not _cheap(slug, cfg):
                continue
            # no secondary sale ever and older than 3 days = locked/soulbound/claim contract, nothing to resell
            ever = (opensea.stats(slug).get("total") or {}).get("sales") or 0
            if not ever and (info["created_ts"] or 0) < now - 3 * DAY:
                continue
            try:
                recent = opensea.events(slug, "mint", after=now - 6 * 3600, max_pages=2, ttl=300)
            except RuntimeError as e:
                log(f"live skip {slug}: {e}")
                continue
            if not recent:
                continue
            by_tok = defaultdict(list)
            for m in recent:
                tok = m["nft"]["identifier"] if m["nft"].get("token_standard") == "erc1155" else None
                by_tok[tok].append(m)
            for tok, ms in by_tok.items():
                cost = analyze._mint_cost(info, ms, samples=3)
                if not cost or "value" not in cost:   # airdrop/claim-only mints have no price
                    continue
                offer = opensea.best_collection_offer(slug)
                sell = chain.sell_cost_eth(info["chain"])
                offer_net = offer * (1 - info["required_fee"]) - sell if offer else None
                out.append({
                    "artist": a["name"], "artist_score": a["score"], "artist_consistency": a["consistency"],
                    "artist_median_premium": a["median_premium"], "confidence": a["confidence"],
                    "slug": slug, "name": info["name"], "token": tok, "chain": info["chain"],
                    "mint_value": round(cost["value"], 6), "all_in": round(cost["all_in"], 6),
                    "mints_6h": len(ms), "last_mint_min_ago": round((now - max(m["event_timestamp"] for m in ms)) / 60),
                    "best_offer": offer, "offer_net_vs_cost": round(offer_net / cost["all_in"], 2)
                    if offer_net and cost["all_in"] > 0 else None,
                    "target_exit": round(cfg["min_premium"] * cost["all_in"] / (1 - info["required_fee"])
                                         + sell, 6),
                    "fits_budget": cost["all_in"] <= cap, "budget_cap": cap,
                    "opensea_url": info["opensea_url"] + (f"/overview?token={tok}" if tok else ""),
                    "project_url": info["project_url"],
                })
    # OpenSea SeaDrop upcoming drops by the same creators
    owners = {a["owner"] for a in top if a["owner"]}
    for dr in opensea.drops("upcoming"):
        info = analyze.coll_info(dr["collection_slug"])
        if info and info["owner"] in owners and _cheap(info["slug"], cfg):
            out.append({"artist": next(a["name"] for a in top if a["owner"] == info["owner"]), "upcoming": True,
                        "slug": info["slug"], "name": info["name"], "chain": info["chain"],
                        "next_stage": dr.get("next_stage"), "opensea_url": dr["opensea_url"]})
    out.sort(key=lambda o: (not o.get("fits_budget", False), -(o.get("artist_score") or 0)))
    return out


def notify_live(top_n: int = 5, cfg: dict | None = None) -> list[dict]:
    """Ping the phone for each budget-fitting live mint from the top artists (deduped per drop)."""
    sent = []
    for o in live_drops(only_top=top_n, cfg=cfg):
        if o.get("upcoming") or not o.get("fits_budget"):
            continue
        title, text = notify.opportunity_text(o)
        r = notify.ping(title, text, click=o["project_url"] or o["opensea_url"],
                        dedupe_key=f"{o['slug']}:{o.get('token')}")
        sent.append({"drop": title, **r})
    return sent


def watch(top_n: int = 5) -> dict:
    """One unattended tick: full rescan only when the last one is older than rescan_hours, then live check + pings."""
    cfg = store.config()
    last = store.last_scan()
    rescanned = (not last or time.time() - last["at"] > cfg["rescan_hours"] * 3600
                 or bool(os.environ.get("SCOUT_FORCE_RESCAN")))
    if rescanned:
        run_scan(cfg=cfg)
    sent = notify_live(top_n, cfg)
    mine = wallet.check(cfg)
    mine += follow.check(cfg)
    store.prune_cache()
    log(f"watch: rescanned={rescanned} pings={sum(x.get('sent', False) for x in sent)} "
        f"wallet={sum(x.get('sent', False) for x in mine)}", public=True)
    return {"rescanned": rescanned, "top5": store.last_scan().get("top5"), "pings": sent, "wallet": mine}
