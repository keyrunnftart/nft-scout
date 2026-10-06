"""Venue-agnostic resales straight from the chain: NFT transfers of one contract in a time window, priced by the
ETH (tx value) + WETH the buyer paid in the same transaction. Catches sales OpenSea's feed doesn't carry
(Verse, Manifold Gallery, Foundation, SuperRare ...). Returned in OpenSea's event shape so analyze_drop uses them
unchanged. Only called for drops whose OpenSea sales are thin, since every chunk is an RPC call."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import httpx

from . import chain, store


def log(*a) -> None:     # own copy (analyze imports this module); same CI rule: no names in public logs
    if not os.environ.get("GITHUB_ACTIONS"):
        print("[scout]", *a, file=sys.stderr, flush=True)

ZERO = "0x" + "0" * 40
# keyless sources per chain (checked 2026-10-06): free ETH RPCs refuse historical getLogs and Base's RPC caps
# ranges at 500 blocks, so ETH/Shape use Blockscout, Base needs ETHERSCAN_API_KEY, the rest read their own RPC
BLOCKSCOUT = {"ethereum": "https://eth.blockscout.com", "shape": "https://shapescan.xyz"}
ETHERSCAN_CHAIN_ID = {"base": 8453}
RPC_CHUNK = {"optimism": 49_999, "zora": 49_999, "arbitrum": 49_999, "abstract": 49_999}
MAX_CHUNKS = 120
MAX_PAGES = 40                    # blockscout pages of 50 transfers, newest first
WETH = {
    "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "base": "0x4200000000000000000000000000000000000006", "optimism": "0x4200000000000000000000000000000000000006",
    "zora": "0x4200000000000000000000000000000000000006", "shape": "0x4200000000000000000000000000000000000006",
    "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    "abstract": "0x3439153eb7af838ad19d56e1571fbd09333c2809",
}


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _block_ts(ch: str, n: int) -> int:
    key = f"bts:{ch}:{n}"
    hit = store.cache_get(key, None)
    if hit is not None:
        return hit
    ts = int(chain.call(ch, "eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
    store.cache_put(key, ts)
    return ts


def block_at(ch: str, ts: int) -> int:
    """First block at or after unix time ts (binary search, cached per hour)."""
    key = f"bat:{ch}:{ts // 3600}"
    hit = store.cache_get(key, None)
    if hit is not None:
        return hit
    lo, hi = 0, int(chain.call(ch, "eth_blockNumber", []), 16)
    if _block_ts(ch, hi) < ts:
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        if _block_ts(ch, mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    store.cache_put(key, lo)
    return lo


def _from_logs(ch: str, contract: str, t_from: int, t_to: int) -> list[tuple]:
    chunk = RPC_CHUNK[ch]
    b0, b1 = block_at(ch, t_from), block_at(ch, t_to)
    if (b1 - b0) / (chunk + 1) > MAX_CHUNKS:
        raise RuntimeError("window too long for free rpcs")
    out = []
    for i in range(b0, b1 + 1, chunk + 1):
        for lg in chain.call(ch, "eth_getLogs", [{"address": contract, "fromBlock": hex(i),
                                                  "toBlock": hex(min(i + chunk, b1))}]) or []:
            t = lg.get("topics", [])
            if t and t[0] == chain.T721 and len(t) == 4:
                row = (_addr(t[1]), _addr(t[2]), str(int(t[3], 16)), 1)
            elif t and t[0] == chain.T1155_SINGLE and len(t) == 4:
                row = (_addr(t[2]), _addr(t[3]), str(int(lg["data"][2:66], 16)), int(lg["data"][66:130], 16))
            else:
                continue                                  # batch transfers are airdrops/migrations, not sales
            out.append((lg["transactionHash"], _block_ts(ch, int(lg["blockNumber"], 16)), *row))
    return out


def _from_blockscout(ch: str, contract: str, t_from: int, t_to: int) -> list[tuple]:
    out, params = [], {}
    for _ in range(MAX_PAGES):
        r = httpx.get(f"{BLOCKSCOUT[ch]}/api/v2/tokens/{contract}/transfers", params=params, timeout=30,
                      headers={"user-agent": "keybot-nft-scout", "accept": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"blockscout {r.status_code}")
        d = r.json()
        for it in d.get("items", []):
            ts = int(datetime.fromisoformat(it["timestamp"].replace("Z", "+00:00")).timestamp())
            if ts < t_from:
                return out
            if ts > t_to or it.get("type") != "token_transfer":
                continue
            tot = it.get("total") or {}
            qty = int(tot.get("value") or 1) if it.get("token_type") == "ERC-1155" else 1
            out.append((it["transaction_hash"], ts, (it.get("from") or {}).get("hash", "").lower(),
                        (it.get("to") or {}).get("hash", "").lower(), str(tot.get("token_id")), qty))
        params = d.get("next_page_params")
        if not params:
            return out
    raise RuntimeError("too many transfers to page through")


def _from_etherscan(ch: str, contract: str, t_from: int, t_to: int) -> list[tuple]:
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        raise RuntimeError(f"{ch} needs ETHERSCAN_API_KEY")
    b0, b1 = block_at(ch, t_from), block_at(ch, t_to)
    out = []
    for action in ("tokennfttx", "token1155tx"):
        r = httpx.get("https://api.etherscan.io/v2/api", timeout=30, params={
            "chainid": ETHERSCAN_CHAIN_ID[ch], "module": "account", "action": action, "contractaddress": contract,
            "startblock": b0, "endblock": b1, "sort": "asc", "page": 1, "offset": 1000, "apikey": key}).json()
        rows = r.get("result") if isinstance(r.get("result"), list) else []
        for it in rows:
            out.append((it["hash"], int(it["timeStamp"]), it["from"].lower(), it["to"].lower(),
                        str(it["tokenID"]), int(it.get("tokenValue") or 1)))
    return out


def _transfers(ch: str, contract: str, t_from: int, t_to: int) -> list[tuple]:
    """[(tx, ts, seller, buyer, token, qty)] for every transfer of the contract in the window."""
    if ch in BLOCKSCOUT:
        return _from_blockscout(ch, contract, t_from, t_to)
    if ch in ETHERSCAN_CHAIN_ID:
        return _from_etherscan(ch, contract, t_from, t_to)
    if ch in RPC_CHUNK:
        return _from_logs(ch, contract, t_from, t_to)
    raise RuntimeError(f"no transfer source for {ch}")


def _paid(ch: str, tx_hash: str, buyer: str) -> float:
    """ETH + WETH the buyer paid in this tx (cached forever)."""
    key = f"paid:{ch}:{tx_hash}:{buyer}"
    hit = store.cache_get(key, None)
    if hit is not None:
        return hit
    tx = chain.call(ch, "eth_getTransactionByHash", [tx_hash])
    rc = chain.call(ch, "eth_getTransactionReceipt", [tx_hash])
    wei = int(tx.get("value", "0x0"), 16) if (tx.get("from") or "").lower() == buyer else 0
    weth = WETH.get(ch)
    for lg in rc.get("logs", []):
        t = lg.get("topics", [])
        if (lg.get("address", "").lower() == weth and len(t) == 3 and t[0] == chain.T721
                and _addr(t[1]) == buyer):
            wei += int(lg["data"], 16)
    v = wei / 1e18
    store.cache_put(key, v)
    return v


def sales(info: dict, t_from: int, t_to: int, skip_tx: set[str] | None = None) -> list[dict] | None:
    """Priced secondary transfers of info's contract between t_from and t_to, as OpenSea-style sale events.
    None when the window is too long to scan or the RPCs fail (the caller then keeps OpenSea data only)."""
    ch, contract = info["chain"], info["contract"]
    now = int(time.time())
    t_to = min(t_to, now)
    key = f"onchain:{ch}:{contract}:{t_from}:{t_to // 3600}"
    hit = store.cache_get(key, None if t_to < now - 3600 else 3600)
    if hit is not None:
        return [s for s in hit if s["transaction"] not in (skip_tx or set())]
    try:
        units: dict[tuple[str, str], list[tuple[str, int, str]]] = {}   # (tx, buyer) -> [(token, qty, seller)]
        stamp: dict[str, int] = {}
        for tx, ts, seller, buyer, tok, qty in _transfers(ch, contract, t_from, t_to):
            if seller == ZERO or buyer == ZERO or seller == buyer:
                continue                                  # mint / burn / self
            units.setdefault((tx, buyer), []).append((tok, qty, seller))
            stamp[tx] = ts
        out = []
        for (tx, buyer), items in units.items():
            paid = _paid(ch, tx, buyer)
            n = sum(q for _, q, _ in items)
            if paid <= 0 or n == 0:
                continue                                  # gift / transfer between own wallets
            ts = stamp[tx]
            for tok, qty, seller in items:
                out.append({"event_type": "sale", "event_timestamp": ts, "transaction": tx, "chain": ch,
                            "seller": seller, "buyer": buyer, "quantity": qty, "source": "onchain",
                            "nft": {"identifier": tok},
                            "payment": {"quantity": str(int(paid / n * qty * 1e18)), "decimals": 18,
                                        "symbol": "ETH"}})
    except (RuntimeError, httpx.HTTPError, KeyError, ValueError) as e:
        log(f"onchain skip {info['slug']}: {e}")
        return None
    store.cache_put(key, out)
    return [s for s in out if s["transaction"] not in (skip_tx or set())]
