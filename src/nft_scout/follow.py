"""Follow mode: one phone ping per new piece/drop minted by artists in config `follow_artists`, flip score or not
(to collect early). First run only sets the cursor. Dedupe: ONE ping per collection, ever (user, 13 Nov 2026: a single
HectorOZ drop sent ~200 pings when it was keyed per token)."""

from __future__ import annotations

import time

import httpx

from . import chain, notify, opensea, store
from .analyze import coll_info, log, parallel


def _artist_slugs(name: str) -> tuple[str, list[str], str]:
    acct = opensea.get(f"accounts/{name}", ttl=86400) if not name.startswith("0x") else opensea.account(name)
    user = acct.get("username") or (None if name.startswith("0x") else name)
    wallet = (acct.get("address") or (name if name.startswith("0x") else "")).lower()
    try:
        slugs = [c["collection"] for c in opensea.creator_collections(user, pages=3)] if user else []
    except RuntimeError as e:
        log(f"follow: {e}")
        slugs = []
    return user or name, slugs, wallet


def _key(info: dict, e: dict) -> str:
    return f"follow:{info['slug']}"


def _pinged(prefix: str) -> bool:
    """Already pinged for this collection, under the new key or any older per-token / per-day key."""
    return any(k == prefix or k.startswith(prefix + ":") for k in store.sent())


def check(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or store.config()
    artists = cfg.get("follow_artists") or []
    st = store.follow_state()
    now = int(time.time())
    since = st.get("since")
    st["since"] = now
    if not since or not artists:
        store.save_follow_state(st)
        return []
    pings = {}
    for name in artists:
        who, slugs, wallet = _artist_slugs(name)

        def mints(slug: str) -> list[dict]:
            try:
                return opensea.events(slug, "mint", after=since - 120, max_pages=2, ttl=0)
            except RuntimeError as e:
                log(f"follow skip {slug}: {e}")
                return []

        for slug, evs in zip(slugs, parallel(mints, slugs, 6)):
            info = coll_info(slug) if evs else None
            if not info:
                continue
            groups: dict[str, list[dict]] = {}
            for e in evs:
                groups.setdefault(_key(info, e), []).append(e)
            for k, es in groups.items():
                if _pinged(k):
                    continue
                nft = es[0].get("nft") or {}
                # price from collectors' mints; the artist's own first mint is free and says nothing
                buys = [e for e in es if (e.get("to_address") or "").lower() != wallet][:3]
                costs = [c for c in (chain.mint_cost(info["chain"], e["transaction"], info["contract"])
                                     for e in buys if e.get("transaction")) if c and "value" in c]
                if costs:
                    v = sorted(c["value"] for c in costs)[len(costs) // 2]
                    price = "free mint" if v == 0 else f"{v:.4f}".rstrip("0").rstrip(".") + " eth"
                    what = f"{price} · {len(es)} mints since last check"
                else:
                    what = "minted by the artist (new 1/1 or reserve; watch for a listing/auction)"
                pings[k] = (f"new from {who}: {info['name'] or nft.get('name')}",
                            f"{info['name']} · {info['chain']}\n{what}", nft.get("opensea_url") or info.get("opensea_url"))
    sent = []
    for k, (title, text, url) in pings.items():
        try:
            sent.append({"ping": title, **notify.ping(title, text, click=url, tags="eyes", dedupe_key=k)})
        except httpx.HTTPError as e:
            sent.append({"ping": title, "sent": False, "why": str(e)})
    store.save_follow_state(st)
    return sent
