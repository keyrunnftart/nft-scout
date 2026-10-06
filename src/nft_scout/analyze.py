"""Drop-level track record: all-in mint cost vs net resale in the first `hold_days`, per collection (721)
or per token id (1155 editions), then artist scores built from every drop the artist made, flops included."""

from __future__ import annotations

import math
import os
import statistics as st
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import chain, onchain, opensea, store

DAY = 86400


_CI = bool(os.environ.get("GITHUB_ACTIONS"))   # the repo is public: CI logs show counts only, never names


def log(*a, public: bool = False) -> None:
    if public or not _CI:
        print("[scout]", *a, file=sys.stderr, flush=True)


def _ts(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return int(datetime.fromisoformat(date_str[:10]).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _med(xs: list[float]) -> float | None:
    return st.median(xs) if xs else None


def coll_info(slug: str) -> dict | None:
    m = opensea.collection(slug)
    if not m or m.get("_error") or not m.get("contracts"):
        return None
    c = m["contracts"][0]
    return {
        "slug": slug, "name": m.get("name"), "owner": (m.get("owner") or "").lower(), "chain": c["chain"],
        "contract": c["address"].lower(), "category": (m.get("category") or "").lower(),
        "created_ts": _ts(m.get("created_date")), "supply": m.get("total_supply"),
        "required_fee": sum(f["fee"] for f in m.get("fees", []) if f.get("required")) / 100,
        "creator_fee_optional": sum(f["fee"] for f in m.get("fees", []) if not f.get("required")) / 100,
        "opensea_url": m.get("opensea_url"), "project_url": m.get("project_url") or None,
        "twitter": m.get("twitter_username") or None, "disabled": m.get("is_disabled"), "nsfw": m.get("is_nsfw"),
    }


def _unit_price(ev: dict) -> float | None:
    p = ev.get("payment") or {}
    if (p.get("symbol") or "").upper() not in ("ETH", "WETH"):
        return None
    try:
        return int(p["quantity"]) / 10 ** int(p.get("decimals", 18)) / max(1, int(ev.get("quantity") or 1))
    except (KeyError, ValueError):
        return None


def _clean_sales(sales: list[dict]) -> list[dict]:
    """Drop self-trades and A<->B ping-pong pairs (cheap wash filter)."""
    pairs = defaultdict(int)
    for s in sales:
        pairs[(s.get("seller"), s.get("buyer"))] += 1
    out = []
    for s in sales:
        a, b = s.get("seller"), s.get("buyer")
        if not a or not b or a == b or pairs.get((b, a)):
            continue
        out.append(s)
    return out


def _mint_cost(info: dict, mints: list[dict], samples: int = 8) -> dict | None:
    txs = []
    for m in sorted(mints, key=lambda e: e["event_timestamp"]):
        if m["transaction"] not in txs:
            txs.append(m["transaction"])
    if not txs:
        return None
    step = max(1, len(txs) // samples)
    pick = txs[::step][:samples]
    raw = [c for c in (chain.mint_cost(info["chain"], t, info["contract"]) for t in pick) if c]
    costs = [c for c in raw if not c.get("airdrop")]
    if not costs:
        return {"airdrop_only": True} if raw else None
    paid = [c for c in costs if c["value"] > 0]
    return {"value": _med([c["value"] for c in costs]), "gas": _med([c["gas"] for c in costs]),
            "all_in": _med([c["value"] + c["gas"] for c in costs]), "sampled": len(costs),
            "airdrops_skipped": len(raw) - len(costs),
            # free claims mixed with a paid public mint: what a new minter actually pays
            "paid_all_in": _med([c["value"] + c["gas"] for c in paid]) if paid else None,
            "free_share": round(1 - len(paid) / len(costs), 2)}


def _mint_start(slug: str, mints: list[dict], created_ts: int, nth: int = 10) -> int:
    """Public mint start = time of the nth mint (skips test/pre-mints days before the public window).
    With >= 1000 mints the earliest ones were cut off, so binary-search the time where nth mints exist."""
    ts = sorted(m["event_timestamp"] for m in mints)
    if len(mints) < 1000:
        return ts[min(nth, len(ts)) - 1]
    lo, hi = created_ts - DAY, ts[0]
    while hi - lo > 600:
        mid = (lo + hi) // 2
        n = len(opensea.events(slug, "mint", after=created_ts - DAY, before=mid, max_pages=1))
        lo, hi = (lo, mid) if n >= nth else (mid, hi)
    return hi


def _window_sales(slug: str, t0: int, hold_days: int) -> list[dict]:
    out: list[dict] = []
    now = int(time.time())
    for a, b in ((0, 3), (3, 7), (7, hold_days)):
        if t0 + a * DAY >= now:
            break
        out += opensea.events(slug, "sale", after=t0 + a * DAY, before=min(t0 + b * DAY, now), max_pages=8)
    return out


def analyze_drop(info: dict, mints: list[dict], sales: list[dict], t0: int, token: str | None, cfg: dict) -> dict:
    hold = cfg["hold_days"] * DAY
    now = int(time.time())
    cost = _mint_cost(info, mints)
    sell_cost = chain.sell_cost_eth(info["chain"])
    window = [s for s in sales if t0 <= s["event_timestamp"] < t0 + hold
              and (token is None or s["nft"]["identifier"] == token)]
    window = _clean_sales(window)
    rows = []
    for s in window:
        p = _unit_price(s)
        if p is None or p <= 0:
            continue
        rows.append((s["event_timestamp"], p, p * (1 - info["required_fee"]) - sell_cost, s.get("buyer")))
    rows.sort()
    d = {
        "slug": info["slug"], "name": info["name"], "token": token, "chain": info["chain"],
        "start": datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d"),
        "complete": now >= t0 + hold, "mints_seen": len(mints),
        "minting_now": bool(mints) and max(m["event_timestamp"] for m in mints) > now - 6 * 3600,
        "mint_value": round(cost["value"], 6) if cost and "value" in cost else None,
        "mint_all_in": round(cost["all_in"], 6) if cost and "all_in" in cost else None,
        "sales_14d": len(rows), "unique_buyers": len({r[3] for r in rows}),
    }
    if cost and cost.get("airdrop_only"):
        d.update(mint_value=None, mint_all_in=None, premium=None, hit_rate=None, verdict="airdrop/claim")
        return d
    if not rows or not cost:
        d.update(premium=None, hit_rate=None, verdict="no data" if not cost else "no resales")
        return d
    basis = max(cost["all_in"], 1e-5)
    early = [r[1] for r in rows if r[0] < t0 + 3 * DAY]
    late = [r[1] for r in rows if r[0] >= t0 + 7 * DAY]
    # exit = what a holder could get after the launch spike (days 3-14), not the hype-inflated full median
    post = [r for r in rows if r[0] >= t0 + 3 * DAY]
    nets = [r[2] for r in post]
    d.update(
        median_sale=round(_med([r[1] for r in rows]), 6), median_early=round(_med(early), 6) if early else None,
        median_exit=round(_med([r[1] for r in post]), 6) if post else None,
        median_net=round(_med(nets), 6) if nets else None,
        premium=round(_med(nets) / basis, 2) if nets else None,
        hit_rate=round(sum(n >= cfg["min_premium"] * basis for n in nets) / len(nets), 2) if nets else None,
        first_resale_h=round((rows[0][0] - t0) / 3600, 1),
        trend=round(_med(late) / _med(early), 2) if early and late else None,
        free_mint=cost["value"] == 0,
    )
    if d["premium"] is not None:
        d["premium"] = min(d["premium"], 10.0)   # free mints divide by ~gas; cap so they don't swamp scores
    if len(rows) < cfg["min_sales_14d"] or len(post) < 3:
        d["verdict"] = "thin"
    elif d["unique_buyers"] < max(5, 0.15 * len(rows)):
        d["verdict"] = "wash?"
    elif d["premium"] >= cfg["min_premium"] and d["hit_rate"] >= 0.5:
        d["verdict"] = "hit"
    else:
        d["verdict"] = "miss"
    return d


def analyze_collection(slug: str, cfg: dict) -> list[dict]:
    """All drops (primary mint windows) inside the lookback for one collection. Cached 1h. A collection whose
    data can't be fetched in full is skipped (not cached) rather than scored on partial history."""
    try:
        return _analyze_collection(slug, cfg)
    except RuntimeError as e:
        log(f"skip {slug}: {e}")
        return []


def _analyze_collection(slug: str, cfg: dict) -> list[dict]:
    key = f"drops4:{slug}:{cfg['hold_days']}:{cfg['lookback_days']}:{cfg['min_premium']}"
    hit = store.cache_get(key, 3600)
    if hit is not None:
        return hit
    info = coll_info(slug)
    out: list[dict] = []
    if info and not info["disabled"] and not info["nsfw"] and info["chain"] in cfg["chains"]:
        now = int(time.time())
        lb = now - cfg["lookback_days"] * DAY
        mints = opensea.events(slug, "mint", after=lb, max_pages=20)
        if mints:
            std = mints[0]["nft"].get("token_standard", "erc721")
            if std == "erc1155":
                groups = defaultdict(list)
                for m in mints:
                    groups[m["nft"]["identifier"]].append(m)
                groups = {k: v for k, v in groups.items() if len(v) >= 3}
                newest = sorted(groups, key=lambda k: -min(e["event_timestamp"] for e in groups[k]))[:12]
                # 3rd mint of each token = public start (skips the artist's own first mint)
                starts = {k: sorted(e["event_timestamp"] for e in groups[k])[2] for k in newest}
                if starts:
                    sales = opensea.events(slug, "sale", after=min(starts.values()), max_pages=30)
                    if len(sales) < cfg["min_sales_14d"] * len(starts):
                        # thin on OpenSea: add resales from other venues, read from the chain
                        end = min(max(starts.values()) + cfg["hold_days"] * DAY, min(starts.values()) + 60 * DAY)
                        sales += onchain.sales(info, min(starts.values()), end,
                                               {x.get("transaction") for x in sales}) or []
                    for k in newest:
                        out.append(analyze_drop(info, groups[k], sales, starts[k], k, cfg))
            elif (info["created_ts"] or 0) >= lb - 7 * DAY:
                # one 721 drop per collection; older contracts minting now are mutations/claims, not primaries
                t0 = _mint_start(slug, mints, info["created_ts"] or lb)
                early = mints if len(mints) < 1000 else opensea.events(slug, "mint", after=t0 - DAY,
                                                                        before=t0 + 3 * DAY, max_pages=4)
                sales = _window_sales(slug, t0, cfg["hold_days"])
                if len(sales) < cfg["min_sales_14d"]:
                    sales += onchain.sales(info, t0, t0 + cfg["hold_days"] * DAY,
                                           {x.get("transaction") for x in sales}) or []
                d = analyze_drop(info, early or mints, sales, t0, None, cfg)
                d["mints_seen"] = len(mints)
                out.append(d)
        fl = opensea.stats(slug)
        floor_now = None if fl.get("_error") else ((fl.get("total") or {}).get("floor_price") or None)
        for d in out:
            if d["token"] is None and floor_now and d.get("mint_all_in"):
                d.update(floor_now=round(floor_now, 6), floor_vs_mint=round(floor_now / d["mint_all_in"], 2))
            d.update(owner=info["owner"], opensea_url=info["opensea_url"], project_url=info["project_url"],
                     required_fee=info["required_fee"], category=info["category"], contract=info["contract"])
    store.cache_put(key, out)
    return out


def score_artist(drops: list[dict], cfg: dict) -> dict:
    # free mints don't count: resale / gas makes any sale look like a 10x hit and says nothing about the artist
    judged = [d for d in drops if d["verdict"] in ("hit", "miss") and not d.get("free_mint")
              and (d.get("mint_all_in") or 0) <= cfg.get("max_mint_eth", 1e9)]
    hits = [d for d in judged if d["verdict"] == "hit"]
    prem = [d["premium"] for d in judged if d["premium"] is not None]
    sales = [d["sales_14d"] for d in judged]
    consistency = len(hits) / len(judged) if judged else 0
    med_prem = _med(prem) or 0
    med_sales = _med(sales) or 0
    live = any(d["minting_now"] for d in drops)
    held = [d["floor_vs_mint"] for d in judged if d.get("complete") and d.get("floor_vs_mint") is not None]
    holds = _med(held)
    score = (50 * consistency + 25 * min(med_prem, 3) / 3 + 15 * min(math.log10(1 + med_sales) / 2, 1)
             + 10 * min(len(judged), 5) / 5)
    return {
        "score": round(score, 1), "drops_judged": len(judged), "hits": len(hits),
        "consistency": round(consistency, 2), "median_premium": round(med_prem, 2),
        "median_sales_14d": med_sales, "live_now": live,
        "floor_vs_mint": round(holds, 2) if holds is not None else None,
        "qualifies": bool(judged) and consistency >= 0.6 and med_prem >= cfg["min_premium"]
                     and (holds is None or holds >= 1.0),
        "confidence": "high" if len(judged) >= 3 else "medium" if len(judged) == 2 else "low (1 drop)",
    }


def parallel(fn, items, workers: int = 6):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))
