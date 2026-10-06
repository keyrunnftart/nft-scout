"""OpenSea API v2 client: rate-limited (~10 req/s, key allows 16), retrying, cached in sqlite."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterator

import httpx

from . import store

BASE = "https://api.opensea.io/api/v2"
_lock = threading.Lock()
_next_slot = 0.0
_RPS = 10.0
_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=20, headers={"x-api-key": os.environ.get("OPENSEA_API_KEY", ""),
                                                     "accept": "application/json",
                                                     "user-agent": "keybot-nft-scout/0.1"})
    return _client


def _wait_slot() -> None:
    global _next_slot
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + 1.0 / _RPS
    if slot > now:
        time.sleep(slot - now)


def get(path: str, params: dict | None = None, ttl: float | None = 3600) -> dict:
    """GET with cache (ttl seconds; None = forever, 0 = no cache). 404 -> {}."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    key = "os:" + path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    if ttl != 0:
        hit = store.cache_get(key, ttl)
        if hit is not None:
            return hit
    timeouts, why = 0, None
    for attempt in range(6):
        _wait_slot()
        try:
            r = _http().get(f"{BASE}/{path}", params=params)
        except httpx.TimeoutException:
            timeouts, why = timeouts + 1, "timeout"                       # deep event pages can hang; 2 strikes and the caller skips
            if timeouts >= 2:
                break
            continue
        except httpx.HTTPError as e:
            why = type(e).__name__
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            why = f"http {r.status_code}"
            time.sleep(min(float(r.headers.get("retry-after") or 2 * (attempt + 1)), 20))
            continue
        if r.status_code in (400, 404):
            data: dict = {"_error": r.status_code, "errors": _errors(r)}
        else:
            r.raise_for_status()
            data = r.json()
        if ttl != 0:
            store.cache_put(key, data)
        return data
    # not cached; single lookups degrade to "unknown" (caller skips the collection), paged reads raise below
    return {"_error": "unavailable", "why": why}


def _errors(r: httpx.Response) -> Any:
    try:
        return r.json().get("errors")
    except Exception:
        return r.text[:200]


def paged(path: str, key: str, params: dict | None = None, max_pages: int = 10, ttl: float | None = 3600,
          limit: int = 50) -> Iterator[dict]:
    params = dict(params or {}, limit=limit)
    for _ in range(max_pages):
        d = get(path, params, ttl)
        if d.get("_error") == "unavailable":
            raise RuntimeError(f"OpenSea kept failing for {path} ({d.get('why')})")   # a partial event list would mis-score a drop
        yield from d.get(key) or []
        nxt = d.get("next")
        if not nxt:
            return
        params["next"] = nxt


# ---- typed helpers -------------------------------------------------------------------------------

def collection(slug: str) -> dict:
    return get(f"collections/{slug}", ttl=86400)


def stats(slug: str) -> dict:
    return get(f"collections/{slug}/stats", ttl=1800)


def top_collections(chain: str, order_by: str = "seven_day_volume", pages: int = 1) -> list[dict]:
    return list(paged("collections", "collections", {"chain": chain, "order_by": order_by},
                      max_pages=pages, ttl=3600, limit=100))


def creator_collections(username: str, pages: int = 3) -> list[dict]:
    return list(paged("collections", "collections", {"creator_username": username}, max_pages=pages,
                      ttl=21600, limit=100))


def account(address: str) -> dict:
    return get(f"accounts/{address}", ttl=7 * 86400)


def events(slug: str, event_type: str | list[str], after: int | None = None, before: int | None = None,
           max_pages: int = 10, ttl: float | None = 3600) -> list[dict]:
    return list(paged(f"events/collection/{slug}", "asset_events",
                      {"event_type": event_type, "after": after, "before": before}, max_pages=max_pages, ttl=ttl))


def best_collection_offer(slug: str) -> float | None:
    """Highest collection-wide offer in ETH (per unit), or None."""
    d = get(f"offers/collection/{slug}", ttl=600)
    best = None
    for o in d.get("offers") or []:
        try:
            p = o["price"]
            val = int(p["value"]) / 10 ** int(p.get("decimals", 18))
            if p.get("currency", "").upper() not in ("ETH", "WETH"):
                continue
            qty = int(o.get("protocol_data", {}).get("parameters", {}).get("consideration", [{}])[0]
                      .get("startAmount", 1)) or 1
            val /= qty
        except Exception:
            continue
        best = val if best is None else max(best, val)
    return best


def drops(kind: str = "upcoming", pages: int = 3) -> list[dict]:
    return list(paged("drops", "drops", {"type": kind}, max_pages=pages, ttl=1800, limit=100))
