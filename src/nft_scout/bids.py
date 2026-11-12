"""Wallet-first bid watch for EVM chains: no marketplace list, the wallet's own transactions say where its work is.

Learn (each tick, new wallet txs only, from Blockscout): a zero-value call whose method reads like listing (list,
configureAuction, setSalePrice, upsertListing, createReserveAuction, listForBuyNow ...) is a listing tx. Every contract
that emits a non-token event in it is a venue (Foundation's router lists through its market contract, so the emitter
counts, not the called address), and that event + the NFT transfers in the tx give a listing key: nft contracts,
token / listing / auction ids.
Watch (each tick, RPC getLogs on the venues since the last block): a log that hits a key and whose event name reads
like a bid, offer or sale pings the phone. Event names come from the public signature database, so a new marketplace
needs no code: list from the wallet once and its bids are watched."""

from __future__ import annotations

import os
import re
import time

import httpx

from . import chain, opensea, store
from .analyze import log

BLOCKSCOUT = {"ethereum": "https://eth.blockscout.com", "base": "https://base.blockscout.com"}
CHAIN_ID = {"ethereum": 1, "base": 8453}
CHUNK = {"ethereum": 800, "base": 500}
MAX_BACK = {"ethereum": 7200, "base": 43200}      # catch up after up to a day down; older than that is skipped
UA = {"User-Agent": "nft-scout/1.0 (+github.com/keyrunnftart/nft-scout)"}

LISTING = re.compile(r"list|auction|saleprice|offer|order|upsert|configure|reserve|consign", re.I)
NOT_LISTING = re.compile(r"^(mint|whitelist|match|fulfill|purchase|collect|place|bid|buy)", re.I)
BID = re.compile(r"bid|offer", re.I)
SALE = re.compile(r"settle|sold|purchase|buy(?!price)|fulfill|accept|finaliz|mintfrom", re.I)
NOISE = re.compile(r"cancel|withdraw|remov|expire|convert|unlock|refund|updat", re.I)
MARKET = re.compile(r"auction|list|price|offer|bid|sale|sold|drop|edition|order|reserve|exhibition|buy", re.I)   # venue events
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOKEN_EVENTS = {
    TRANSFER,                                                               # ERC721 / ERC20 Transfer
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",   # Approval
    "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31",   # ApprovalForAll
    "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62",   # TransferSingle
    "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb",   # TransferBatch
}
ZERO = "0x" + "0" * 64
MAX_KEYS = 400


def _explorer(ch: str) -> tuple[str, dict]:
    """Etherscan-style API: Etherscan v2 when ETHERSCAN_API_KEY is set, else keyless Blockscout (10 calls, then a
    ~40 min lockout, so it is only asked when the wallet's nonce moved or an RPC refused a log range)."""
    key = os.environ.get("ETHERSCAN_API_KEY")
    if key:
        return "https://api.etherscan.io/v2/api", {"chainid": CHAIN_ID[ch], "apikey": key}
    return f"{BLOCKSCOUT[ch]}/api", {}


def _http() -> httpx.Client:
    return httpx.Client(timeout=30, headers=UA)


def _is_addr(word: str) -> bool:
    return word.startswith("0x" + "0" * 24) and int(word, 16) >= 2 ** 64


def _words(data: str) -> list[str]:
    d = (data or "0x")[2:]
    return ["0x" + d[i:i + 64] for i in range(0, len(d) - 63, 64)]


def _pad(x: str | int) -> str:
    return "0x" + (x[2:] if isinstance(x, str) else hex(x)[2:]).lower().rjust(64, "0")


def _event_name(topic0: str) -> str | None:
    return _sig("event", {topic0}).get(topic0)


def _label(ch: str, nft: str | None, tid: int | None) -> tuple[str, str]:
    if not nft or tid is None:
        return "", ""
    url = f"https://opensea.io/item/{ch}/{nft}/{tid}"
    try:
        o = opensea.get(f"chain/{ch}/contract/{nft}/nfts/{tid}", ttl=86400).get("nft") or {}
        return o.get("name") or f"{o.get('collection') or nft[:8]} #{tid}", url
    except Exception:
        return f"{nft[:8]}… #{tid}", url


# ---- learn ------------------------------------------------------------------------------------------------

def _bs(c: httpx.Client, url: str, params: dict | None = None) -> dict:
    """Blockscout GET with retries; RuntimeError when it stays down or answers with a challenge page."""
    why = None
    for attempt in range(4):
        try:
            r = c.get(url, params=params)
            if r.headers.get("content-type", "").startswith("application/json"):
                d = r.json()
                if isinstance(d, dict) and r.status_code == 200:
                    return d
            why = f"{r.status_code} {r.headers.get('content-type', '')[:20]}"
        except (httpx.HTTPError, ValueError) as e:
            why = type(e).__name__
        time.sleep((8 if why and why.startswith("429") else 2) * (attempt + 1))
    raise RuntimeError(f"blockscout: {why}")


def _sig(kind: str, sels: set[str]) -> dict[str, str]:
    """Selector / topic -> name ("list", "AuctionBid") from the public signature database, cached forever."""
    out, miss = {}, []
    for x in sels:
        hit = store.cache_get(f"sig:{x}", None)
        if hit is None:
            miss.append(x)
        elif hit:
            out[x] = hit
    for i in range(0, len(miss), 50):
        part = miss[i:i + 50]
        try:
            r = _http().get("https://api.openchain.xyz/signature-database/v1/lookup",
                            params={kind: ",".join(part), "filter": "true"}).json()
        except (httpx.HTTPError, ValueError):
            continue                    # not cached: retried next time
        found = (r.get("result") or {}).get(kind) or {}
        for x in part:
            name = ((found.get(x) or [{}])[0].get("name") or "").split("(")[0]
            store.cache_put(f"sig:{x}", name)
            if name:
                out[x] = name
    return out


def _venue_name(ch: str, addr: str) -> str:
    key = f"venue:{ch}:{addr}"
    hit = store.cache_get(key, None)
    if hit:
        return hit
    try:                                # cosmetic: one quick try, never worth a rate-limit wait
        d = _http().get(f"{BLOCKSCOUT[ch]}/api/v2/addresses/{addr}").json()
        name = next((i.get("name") for i in d.get("implementations") or [] if i.get("name")), None) or d.get("name")
    except (httpx.HTTPError, ValueError, AttributeError):
        return addr[:10]
    name = name or addr[:10]
    store.cache_put(key, name)
    return name


def _learn_tx(ch: str, wallet: str, tx: str, mine: dict) -> None:
    w = "0x" + wallet[2:].rjust(64, "0")
    logs = (chain.call(ch, "eth_getTransactionReceipt", [tx]) or {}).get("logs") or []
    nfts = [(lg["address"].lower(), int(lg["topics"][3], 16)) for lg in logs   # the wallet's NFTs moved in this tx
            if len(lg["topics"]) == 4 and lg["topics"][0] == TRANSFER and w in (lg["topics"][1], lg["topics"][2])]
    names = _sig("event", {lg["topics"][0] for lg in logs if lg["topics"]})
    for lg in logs:
        topics = [t.lower() for t in lg["topics"]]
        if not topics or topics[0] in TOKEN_EVENTS or not MARKET.search(names.get(topics[0], "")):
            continue                    # token moves, payment splitters, WETH/FETH balances ... are not venues
        venue = lg["address"].lower()
        rest = [t for t in topics[1:] if t not in (w, ZERO)]
        addrs = {t for t in rest if _is_addr(t)}
        nums = {t for t in rest if not _is_addr(t)}                 # indexed token / listing ids
        data = {d for d in _words(lg["data"]) if 1000 <= int(d, 16) < 10 ** 9}   # listing / auction ids in data
        mine_nfts = [x for x in nfts if _pad(x[1]) in nums | set(_words(lg["data"]))] or nfts
        if not mine_nfts:                # no transfer (SR configureAuction, setSalePrice): first address + next id topic
            a = next((t for t in rest if _is_addr(t)), None)
            n = next((t for t in rest if not _is_addr(t)), None)
            mine_nfts = [("0x" + a[-40:], int(n, 16))] if a and n else [(None, None)]
        if venue not in mine["venues"]:
            mine["venues"][venue] = {"name": _venue_name(ch, venue), "keys": []}
        v = mine["venues"][venue]
        for nft, tid in mine_nfts:      # one key per NFT (batch listings)
            name, url = _label(ch, nft, tid)
            key = {"a": sorted(addrs | ({_pad(nft)} if nft else set())),
                   "n": sorted(nums | ({_pad(tid)} if tid is not None else set())), "d": sorted(data),
                   "label": name, "url": url}
            if (key["n"] or key["d"]) and key not in v["keys"]:
                v["keys"] = (v["keys"] + [key])[-MAX_KEYS:]


def learn(ch: str, wallet: str, mine: dict) -> None:
    """Wallet txs since the last learned block (one Etherscan-style call) -> venues + listing keys. Only runs when
    the wallet's nonce moved: a listing is always a tx the wallet sends."""
    nonce = int(chain.call(ch, "eth_getTransactionCount", [wallet, "latest"]), 16)
    if nonce == mine.get("nonce") and not mine.get("retry"):
        return
    since = mine.get("learned", 0)
    url, extra = _explorer(ch)
    with _http() as c:
        d = _bs(c, url, {"module": "account", "action": "txlist", "address": wallet, "startblock": since + 1,
                         "sort": "asc", "offset": 10000, "page": 1, **extra})
    txs = [t for t in d.get("result") or [] if isinstance(t, dict)]     # "no transactions" -> [] / message
    mine_txs = [t for t in txs if t["from"].lower() == wallet and t.get("isError") == "0"
                and int(t.get("value") or 0) == 0 and len(t.get("input") or "") >= 10]
    methods = _sig("function", {t["input"][:10] for t in mine_txs})
    todo = mine.pop("retry", []) + [t["hash"] for t in mine_txs
                                    if LISTING.search(m := methods.get(t["input"][:10], "")) and not NOT_LISTING.search(m)]
    if txs:
        mine["learned"] = max(int(t["blockNumber"]) for t in txs)
    # the explorer lags the chain: a listing sent seconds ago may not be in txlist yet. Commit the nonce only once the
    # wallet's newest tx (nonce - 1) has been seen, otherwise learn again next tick (Crossings, 10 Nov 2026: the listing
    # tx was missed this way, so its bid never pinged)
    own = [int(t["nonce"]) for t in txs if t["from"].lower() == wallet and str(t.get("nonce", "")).isdigit()]
    mine["seen_nonce"] = max([mine.get("seen_nonce", -1)] + own)
    if mine["seen_nonce"] >= nonce - 1:
        mine["nonce"] = nonce
    else:
        log(f"bids learn {ch}: explorer behind (seen nonce {mine['seen_nonce']} < {nonce - 1}); retry next tick")
    for h in dict.fromkeys(todo):
        try:
            _learn_tx(ch, wallet, h, mine)
        except (RuntimeError, httpx.HTTPError, ValueError, KeyError) as e:
            log(f"bids learn {ch} {h[:10]}: {e}")
            mine.setdefault("retry", []).append(h)


# ---- watch ------------------------------------------------------------------------------------------------

def _hit(lg: dict, venue: dict) -> dict | None:
    topics = [t.lower() for t in lg["topics"][1:] if t]    # blockscout pads missing topics with null
    la = {t for t in topics if _is_addr(t)}
    ln = {t for t in topics if not _is_addr(t) and t != ZERO}
    ld = set(_words(lg["data"]))
    keys = venue["keys"]
    return (next((k for k in keys if set(k["a"]) & la and set(k["n"]) & ln), None)          # nft + token id topics
            or next((k for k in keys                                                         # listing / auction id topic
                     if {x for x in k["n"] + k.get("d", []) if int(x, 16) >= 1000} & ln), None)
            or next((k for k in keys if not ln and set(k["a"]) & la and set(k["n"]) & ld), None))   # id only in data


def pings(ch: str, wallet: str, mine: dict, logs: list[dict]) -> list[tuple[str, str, str, str]]:
    """Raw venue logs -> pings for bids / offers / sales that hit one of the wallet's listing keys."""
    out = []
    for lg in logs:
        v = mine["venues"].get(lg["address"].lower())
        k = v and _hit(lg, v)
        if not k:
            continue
        name = _event_name(lg["topics"][0]) or ""
        kind = None if NOISE.search(name) else "new bid" if BID.search(name) else "sold" if SALE.search(name) else None
        if not kind:
            continue
        tx = chain.call(ch, "eth_getTransactionByHash", [lg["transactionHash"]]) or {}
        sender = (tx.get("from") or "").lower()
        if sender == wallet:
            continue                    # the wallet's own settle / accept: already known
        value = int(tx.get("value") or "0x0", 16) / 1e18
        amount = f"{value:g} ETH · " if value else ""
        out.append((f"evm:{ch}:{lg['transactionHash']}:{int(str(lg['logIndex']), 0)}", f"{kind}: {k['label'] or v['name']}",
                    f"{amount}{v['name']} · {name} · {ch}\nfrom {sender[:6]}…{sender[-4:]}",
                    k["url"] or f"https://{'basescan.org' if ch == 'base' else 'etherscan.io'}/tx/{lg['transactionHash']}"))
    return out


def _logs(ch: str, venues: list[str], start: int, end: int) -> list[dict]:
    """RPC getLogs (max 10 addresses per call on public nodes); ranges they refuse as 'archive' go to Blockscout."""
    out = []
    for i in range(0, len(venues), 10):
        part = venues[i:i + 10]
        try:
            out += chain.call(ch, "eth_getLogs", [{"address": part, "fromBlock": hex(start), "toBlock": hex(end)}]) or []
            continue
        except RuntimeError as e:
            log(f"bids {ch} rpc logs {start}-{end}: {e}; via blockscout")
        url, extra = _explorer(ch)
        with _http() as c:
            for v in part:
                page = 1
                while True:
                    d = _bs(c, url, {"module": "logs", "action": "getLogs", "address": v, "fromBlock": start,
                                     "toBlock": end, "page": page, "offset": 1000, **extra})
                    res = [x for x in d.get("result") or [] if isinstance(x, dict)]
                    out += res
                    if len(res) < 1000:
                        break
                    page += 1
    return out


def watch(ch: str, wallet: str, mine: dict) -> list[tuple[str, str, str, str]]:
    head = int(chain.call(ch, "eth_blockNumber", []), 16)
    cur = mine.get("block")
    if cur is None or not mine["venues"]:
        mine["block"] = head
        return []
    venues, logs = list(mine["venues"]), []
    for start in range(max(cur + 1, head - MAX_BACK[ch]), head + 1, CHUNK[ch]):
        end = min(head, start + CHUNK[ch] - 1)
        logs += _logs(ch, venues, start, end)
    out = pings(ch, wallet, mine, logs)
    mine["block"] = head
    return out


RELEARN = "2026-11-12"
RELEARN_BACK = {"ethereum": 7200 * 45, "base": 43200 * 45}   # ~45 days of blocks


def check(wallet: str, st: dict, chains: tuple[str, ...] = ("ethereum", "base")) -> list[tuple[str, str, str, str]]:
    pings = []
    for ch in chains:
        mine = st.setdefault("evm", {}).setdefault(ch, {"venues": {}})
        if mine.get("relearn") != RELEARN:
            # one-time full re-learn: listings sent while the old nonce bug was live (PARITY #7, 11 Nov 21:58 UTC, 7 min before
            # f8bcca0 deployed) were never learned, and a fix alone never revisits them because the nonce no longer moves
            mine.update(relearn=RELEARN, nonce=None, learned=max(0, mine.get("learned", 0) - RELEARN_BACK[ch]), learn_after=0)
            log(f"bids {ch}: full re-learn of listings ({RELEARN})")
        if time.time() >= mine.get("learn_after", 0):
            try:
                learn(ch, wallet, mine)
            except (RuntimeError, httpx.HTTPError, ValueError) as e:
                log(f"bids learn {ch}: {e}")    # known venues are still watched; explorer lockouts last ~40 min
                mine["learn_after"] = time.time() + 45 * 60
        log(f"bids {ch}: {sum(len(v['keys']) for v in mine['venues'].values())} listings on {len(mine['venues'])} venues, block {mine.get('block')}")
        try:
            pings += watch(ch, wallet, mine)
        except (RuntimeError, httpx.HTTPError, ValueError) as e:
            log(f"bids watch {ch}: {e}")    # block cursor kept: re-read next tick
    return pings
