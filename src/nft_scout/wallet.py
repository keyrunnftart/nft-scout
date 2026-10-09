"""Wallet watch: phone pings when the user's own work sells (primary or secondary), gets a bid or an auction starts.

Sources, each with its own cursor in data/wallet_state.json (the first run only sets cursors, no replays):
- OpenSea: sales (+ primary mints, item offers, auction listings) on collections the wallet created or that are listed in
  config `my_contracts`, plus account-level sales where the wallet is the seller (1/1s on shared contracts).
- OpenSea account feed: NFTs leaving the wallet on any chain, which catches sales and auction settlements on
  venues OpenSea doesn't trade (SuperRare, Foundation, KnownOrigin, Manifold ...).
- Tezos: objkt events on tokens the tz address created (sales, offer accepts, auction create/bid/settle)."""

from __future__ import annotations

import time

import httpx

from . import notify, opensea, store
from .analyze import coll_info, log, parallel

OBJKT = "https://data.objkt.com/v3/graphql"
TZ_SALE = ("buy", "accept", "settle")          # list_buy, offer_accept, english_auction_settle, dutch_auction_buy ...
TZ_AUCTION = ("auction_create", "auction_bid", "offer_create")


def _short(a: str | None) -> str:
    return f"{a[:6]}…{a[-4:]}" if a else "?"


def _amount(ev: dict) -> float:
    p = ev.get("payment") or {}
    try:
        return int(p["quantity"]) / 10 ** int(p.get("decimals", 18))
    except (KeyError, ValueError, TypeError):
        return 0.0


def _eth(ev: dict) -> str:
    p = ev.get("payment") or {}
    try:
        v = int(p["quantity"]) / 10 ** int(p.get("decimals", 18))
    except (KeyError, ValueError, TypeError):
        return ""
    return f"{v:.6f}".rstrip("0").rstrip(".") + f" {p.get('symbol') or 'ETH'}"


# ---- ETH / L2 via OpenSea ---------------------------------------------------------------------------------

def _my_slugs(cfg: dict, wallet: str) -> dict[str, dict]:
    """slug -> {mute_mints} for collections the wallet created (cached a day) + config my_contracts."""
    out: dict[str, dict] = {}
    name = (opensea.account(wallet) or {}).get("username")
    if name:
        for c in opensea.creator_collections(name, pages=3):
            out[c["collection"]] = {"mute_mints": False}
    for mc in cfg.get("my_contracts", []):
        d = opensea.get(f"chain/{mc['chain']}/contract/{mc['address'].lower()}", ttl=86400)
        if d.get("collection"):
            out[d["collection"]] = {"mute_mints": bool(mc.get("mute_mints"))}
    return out


def _my_tokens(wallet: str) -> set[tuple[str, str]]:
    """(contract, token id) the wallet minted itself: on shared platform contracts (Async, Ninfa ...) these are
    the user's own works; everything else there belongs to other artists. Cached a day."""
    try:
        evs = opensea.paged(f"events/accounts/{wallet}", "asset_events", {"event_type": "mint"},
                            max_pages=20, ttl=86400)
        return {((e.get("nft") or {}).get("contract", "").lower(), str((e.get("nft") or {}).get("identifier")))
                for e in evs if (e.get("to_address") or "").lower() == wallet}
    except RuntimeError as e:
        log(f"wallet my tokens: {e}")
        return set()


def _os_pings(cfg: dict, wallet: str, since: int) -> tuple[list[tuple[str, str, str, str]], bool]:
    """(dedupe key, title, text, click) for OpenSea events after `since`, and whether every feed was read."""
    out = []
    complete = True
    mine: set[tuple[str, str]] | None = None
    slugs = _my_slugs(cfg, wallet)

    def fetch(slug: str) -> list[tuple[str, dict]] | None:
        # one call per collection; mints come back as event_type "transfer" with transfer_type "mint"
        try:
            evs = opensea.events(slug, ["sale", "mint", "listing", "offer"], after=since, max_pages=5, ttl=0)
        except RuntimeError as e:
            log(f"wallet skip {slug}: {e}")
            return None
        out = []
        for e in evs:
            if e["event_type"] == "sale":
                out.append(("sale", e))
            elif e.get("transfer_type") == "mint" and not slugs[slug]["mute_mints"]:
                out.append(("mint", e))
            elif e["event_type"] == "order" and "auction" in (e.get("order_type") or "").lower():
                out.append(("auction", e))
            elif e["event_type"] == "order" and e.get("order_type") == "item_offer":
                out.append(("offer", e))
        return out

    for slug, evs in zip(slugs, parallel(fetch, list(slugs), 6)):
        if evs is None:
            complete = False                 # rate-limited: re-read this window next tick
            continue
        info = coll_info(slug) or {}
        own_contract = info.get("owner") == wallet or slugs[slug]["mute_mints"]   # my_contracts count as own
        for kind, e in evs:
            nft = e.get("nft") or e.get("asset") or {}
            if not own_contract:
                mine = _my_tokens(wallet) if mine is None else mine
                if ((nft.get("contract") or "").lower(), str(nft.get("identifier"))) not in mine:
                    continue                     # another artist's work on a shared platform contract
            if kind == "mint" and (e.get("to_address") or "").lower() == wallet:
                continue                         # the artist minting to their own wallet isn't a sale
            name = nft.get("name") or f"{slug} #{nft.get('identifier')}"
            url = nft.get("opensea_url") or f"https://opensea.io/collection/{slug}"
            tx = e.get("transaction") or e.get("order_hash") or f"{slug}:{nft.get('identifier')}:{e['event_timestamp']}"
            if kind == "sale":
                primary = (e.get("seller") or "").lower() == wallet
                out.append((f"os:{tx}:{nft.get('identifier')}", f"{'sold' if primary else 'resold'}: {name}",
                            f"{'primary' if primary else 'secondary'} sale · {_eth(e)} · {e.get('chain')}\n"
                            f"{_short(e.get('seller'))} → {_short(e.get('buyer'))}", url))
            elif kind == "mint":
                out.append((f"os:{tx}:{nft.get('identifier')}", f"minted: {name}",
                            f"primary mint · {e.get('chain')}\nto {_short(e.get('to_address'))}", url))
            elif kind == "offer":
                if _amount(e) < cfg.get("min_offer_eth", 0):
                    continue                     # bot lowballs
                out.append((f"os:{tx}", f"new bid: {name}",
                            f"offer {_eth(e)} · {e.get('chain')}\nfrom {_short(e.get('maker'))}", url))
            else:
                out.append((f"os:{tx}", f"auction: {name}", f"auction listed · {_eth(e)} · {e.get('chain')}", url))
    # account feed (every chain OpenSea indexes): sales where the wallet is the seller, then any NFT leaving
    # the wallet, which catches sales/auction settlements on venues OpenSea doesn't trade (SuperRare, KO ...)
    for et in ("sale", "transfer"):
        try:
            acct = list(opensea.paged(f"events/accounts/{wallet}", "asset_events",
                                      {"event_type": et, "after": since}, max_pages=3, ttl=0))
        except RuntimeError as e:
            log(f"wallet account feed: {e}")
            complete = False
            continue
        for e in acct:
            nft = e.get("nft") or {}
            name = nft.get("name") or f"{nft.get('collection')} #{nft.get('identifier')}"
            key = f"os:{e.get('transaction')}:{nft.get('identifier')}"
            url = nft.get("opensea_url") or "https://opensea.io"
            if et == "sale" and (e.get("seller") or "").lower() == wallet:
                out.append((key, f"sold: {name}", f"you sold · {_eth(e)} · {e.get('chain')}\n→ {_short(e.get('buyer'))}", url))
            elif (et == "transfer" and e.get("transfer_type") == "transfer"
                  and (e.get("from_address") or "").lower() == wallet
                  and (e.get("to_address") or "") != "0x" + "0" * 40):
                out.append((key, f"left wallet: {name}",
                            f"{e.get('chain')} → {_short(e.get('to_address'))}\nsale, auction settle or transfer",
                            _explorer(e.get("chain"), e.get("transaction")) if e.get("transaction") else url))
    return out, complete


def _explorer(ch: str, tx: str) -> str:
    base = {"ethereum": "etherscan.io", "base": "basescan.org", "arbitrum": "arbiscan.io",
            "optimism": "optimistic.etherscan.io", "zora": "explorer.zora.energy", "shape": "shapescan.xyz",
            "abstract": "abscan.org", "matic": "polygonscan.com", "polygon": "polygonscan.com"}.get(ch, "blockscan.com")
    return f"https://{base}/tx/{tx}"


# ---- Tezos via objkt ------------------------------------------------------------------------------------

def _tz_pings(tz: str, cursor: int | None) -> tuple[list[tuple[str, str, str, str]], int | None]:
    q = """query($a:String!,$c:bigint!){ event(where:{token:{creators:{creator_address:{_eq:$a}}},
      marketplace_event_type:{_is_null:false}, id:{_gt:$c}}, order_by:{id:asc}, limit:200){
      id marketplace_event_type price amount creator_address recipient_address
      token{ name token_id fa_contract } } }"""
    if cursor is None:   # first run: start at the newest event
        q0 = """query($a:String!){ event(where:{token:{creators:{creator_address:{_eq:$a}}}},
          order_by:{id:desc}, limit:1){ id } }"""
        r = httpx.post(OBJKT, json={"query": q0, "variables": {"a": tz}}, timeout=30).json()
        return [], (r.get("data", {}).get("event") or [{"id": 0}])[0]["id"]
    r = httpx.post(OBJKT, json={"query": q, "variables": {"a": tz, "c": cursor}}, timeout=30).json()
    if "errors" in r:
        raise RuntimeError(f"objkt: {r['errors']}")
    out = []
    for e in r["data"]["event"]:
        cursor = max(cursor, e["id"])
        kind = e["marketplace_event_type"] or ""
        tok = e.get("token") or {}
        url = f"https://objkt.com/tokens/{tok.get('fa_contract')}/{tok.get('token_id')}"
        price = f"{(e.get('price') or 0) / 1e6:g} tez"
        name = tok.get("name") or f"#{tok.get('token_id')}"
        if any(k in kind for k in TZ_SALE):
            primary = e.get("creator_address") == tz and "accept" not in kind
            out.append((f"tz:{e['id']}", f"{'sold' if primary else 'resold'}: {name}",
                        f"{'primary' if primary else 'secondary'} · {price} × {e.get('amount') or 1} · tezos ({kind})", url))
        elif any(k in kind for k in TZ_AUCTION):
            what = "auction started" if "auction_create" in kind else "new bid"
            out.append((f"tz:{e['id']}", f"{what}: {name}", f"{price} · tezos ({kind})", url))
    return out, cursor


# ---- tick -------------------------------------------------------------------------------------------------

def check(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or store.config()
    wallet = (cfg.get("my_wallet") or "").lower()
    tz = cfg.get("my_tezos")
    st = store.wallet_state()
    prev = dict(st)
    now = int(time.time())
    pings: list[tuple[str, str, str, str]] = []
    if wallet:
        since = st.get("os_since")
        complete = True
        if since:
            osp, complete = _os_pings(cfg, wallet, since - 120)   # small overlap; dedupe keys stop repeats
            pings += osp
        if complete or now - (since or 0) > 86400:   # a feed that stays down for a day stops holding the cursor
            st["os_since"] = now
    if tz:
        try:
            tzp, st["tz_cursor"] = _tz_pings(tz, st.get("tz_cursor"))
            pings += tzp
        except (RuntimeError, httpx.HTTPError) as e:
            log(f"wallet tezos: {e}")
    sent, keys = [], set()
    for key, title, text, click in pings:
        if key in keys:
            continue                     # a sale also shows as a transfer: keep the first (sale) ping
        keys.add(key)
        try:
            sent.append({"ping": title, **notify.ping(title, text, click=click, tags="art", dedupe_key=key)})
        except httpx.HTTPError as e:
            sent.append({"ping": title, "sent": False, "why": str(e)})
    if any(not x.get("sent") and x.get("why") != "already pinged" for x in sent):
        st = prev                        # keep the old cursors so the next tick retries; dedupe keys skip the sent ones
    store.save_wallet_state(st)
    return sent
