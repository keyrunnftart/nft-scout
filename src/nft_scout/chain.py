"""Read-only JSON-RPC: the real all-in mint cost per token (tx value + gas + L1 fee) from mint receipts."""

from __future__ import annotations

import os
import time

import httpx

from . import store

ZERO_TOPIC = "0x" + "0" * 64
T721 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T1155_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
T1155_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

DEFAULT_RPCS = {
    "ethereum": "https://eth.drpc.org,https://cloudflare-eth.com,https://ethereum.publicnode.com",
    "base": "https://mainnet.base.org,https://base.drpc.org",
    "shape": "https://mainnet.shape.network",
    "zora": "https://rpc.zora.energy",
    "arbitrum": "https://arb1.arbitrum.io/rpc,https://arbitrum.drpc.org",
    "optimism": "https://mainnet.optimism.io,https://optimism.drpc.org",
    "abstract": "https://api.mainnet.abs.xyz",
}
ENV = {"ethereum": "ETH_RPC_URL", "base": "BASE_RPC_URL"}
SELL_GAS_UNITS = 180_000          # accepting an offer / filling a listing, roughly
MINT_GAS_UNITS = 150_000

_client = httpx.Client(timeout=20, headers={"user-agent": "Mozilla/5.0 keybot-nft-scout"})


def rpcs(chain: str) -> list[str]:
    # .env RPCs first, then the public defaults as fallback (some providers refuse archive getLogs)
    raw = os.environ.get(ENV.get(chain, ""), "") + "," + DEFAULT_RPCS.get(chain, "")
    return list(dict.fromkeys(u.strip() for u in raw.split(",") if u.strip()))


def call(chain: str, method: str, params: list) -> object:
    last = None
    for url in rpcs(chain):
        for attempt in range(2):
            try:
                r = _client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                d = r.json()
                if "error" in d:
                    last = d["error"]
                    break
                if d.get("result") is None and method.startswith("eth_getTransaction"):
                    last = "null (pruned?)"
                    break
                return d.get("result")
            except Exception as e:  # network/json hiccup -> retry then next url
                last = e
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"rpc {chain} {method} failed: {last}")


def gas_price_eth(chain: str) -> float:
    key = f"gas:{chain}"
    hit = store.cache_get(key, 600)
    if hit is not None:
        return hit
    try:
        v = int(call(chain, "eth_gasPrice", []), 16) / 1e18
    except Exception:
        v = 3e-9 if chain == "ethereum" else 1e-11
    store.cache_put(key, v)
    return v


def sell_cost_eth(chain: str) -> float:
    return gas_price_eth(chain) * SELL_GAS_UNITS


def mint_gas_eth(chain: str) -> float:
    return gas_price_eth(chain) * MINT_GAS_UNITS


def mint_cost(chain: str, tx_hash: str, contract: str) -> dict | None:
    """{'value': ETH paid per token, 'gas': gas+L1 fee per token, 'tokens': n, 'airdrop': bool} for one mint tx (cached forever)."""
    key = f"mint3:{chain}:{tx_hash}:{contract.lower()}"
    hit = store.cache_get(key, None)
    if hit is not None:
        return hit or None
    try:
        tx = call(chain, "eth_getTransactionByHash", [tx_hash])
        rc = call(chain, "eth_getTransactionReceipt", [tx_hash])
    except Exception:
        return None
    n = 0
    to: set[str] = set()
    c = contract.lower()
    for lg in rc.get("logs", []):
        if lg.get("address", "").lower() != c:
            continue
        t = lg.get("topics", [])
        if not t:
            continue
        if t[0] == T721 and len(t) == 4:                       # Transfer(from, to, id)
            to.add("0x" + t[2][-40:])
        elif t[0] in (T1155_SINGLE, T1155_BATCH) and len(t) == 4:  # Transfer*(operator, from, to, ...)
            to.add("0x" + t[3][-40:])
        if t[0] == T721 and len(t) == 4 and t[1] == ZERO_TOPIC:
            n += 1
        elif t[0] == T1155_SINGLE and len(t) == 4 and t[2] == ZERO_TOPIC:
            n += int(lg["data"][2 + 64:2 + 128], 16)
        elif t[0] == T1155_BATCH and len(t) == 4 and t[2] == ZERO_TOPIC:
            words = [lg["data"][2 + i:2 + i + 64] for i in range(0, len(lg["data"]) - 2, 64)]
            try:
                ids_len = int(words[int(words[0], 16) // 32], 16)
                vals_at = int(words[1], 16) // 32
                n += sum(int(w, 16) for w in words[vals_at + 1:vals_at + 1 + ids_len])
            except Exception:
                n += 1
    if n == 0:
        store.cache_put(key, {})
        return None
    gas = int(rc.get("gasUsed", "0x0"), 16) * int(rc.get("effectiveGasPrice") or tx.get("gasPrice") or "0x0", 16)
    gas += int(rc.get("l1Fee") or "0x0", 16)
    out = {"value": int(tx.get("value", "0x0"), 16) / 1e18 / n, "gas": gas / 1e18 / n, "tokens": n,
           # minted to someone other than the payer = airdrop / gift / claim by the artist, not a public mint
           "airdrop": (tx.get("from") or "").lower() not in to}
    store.cache_put(key, out)
    return out
