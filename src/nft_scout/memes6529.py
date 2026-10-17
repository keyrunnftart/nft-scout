"""6529 Main Stage vote watch: ping when a watched drop gets a new voter or any vote changes.

Config (private SCOUT_STATE / local state/config.json): "watch_6529_drops": ["<drop id>", ...].
State: data/memes6529_state.json -> {drop_id: {"votes": {handle: tdh}, "total": n}}.
The first run for a drop only records the baseline and sends one "watching" ping.
"""

from __future__ import annotations

import json
import time

import httpx

from . import notify, store

API = "https://api.6529.io/api"
UA = {"User-Agent": "nft-scout/1.0 (+github.com/keyrunnftart/nft-scout)"}


def log(*a) -> None:        # public CI log: counts only, never handles or ids
    print("[6529]", *a, flush=True)


def _get(path: str) -> dict | None:
    for attempt in range(3):
        try:
            r = httpx.get(f"{API}/{path}", headers=UA, timeout=20)
            if r.status_code == 200:
                return r.json()
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(2 * (attempt + 1))
    return None


def _state() -> dict:
    p = store.DATA / "memes6529_state.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def _save(st: dict) -> None:
    (store.DATA / "memes6529_state.json").write_text(json.dumps(st, indent=2), "utf-8")


def _fmt(n: int) -> str:
    return f"{n:,}"


def check(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or store.config()
    drops = cfg.get("watch_6529_drops") or []
    if not drops:
        return []
    st, out = _state(), []
    for drop_id in drops:
        try:
            out += _check_drop(drop_id, st)
        except Exception as e:          # never let the 6529 watch break the scout tick
            log(f"drop check failed: {type(e).__name__}")
    _save(st)
    return out


def _check_drop(drop_id: str, st: dict) -> list[dict]:
    rows, page = [], 1
    while True:                         # the API caps page_size at 100
        votes = _get(f"v2/drops/{drop_id}/votes?page_size=100&page={page}")
        if votes is None:
            break
        rows += votes.get("data", [])
        if not votes.get("next") or page >= 20:
            break
        page += 1
    drop = _get(f"drops/{drop_id}")
    if votes is None or drop is None:
        log("api unavailable, keeping previous state")
        return []
    now = {v["voter"]["handle"] or v["voter"]["primary_address"]: int(v["vote"]) for v in rows}
    total = sum(now.values())
    title = drop.get("title") or "6529 drop"
    rank = drop.get("rank")
    wave = (drop.get("wave") or {}).get("id", "")
    click = f"https://6529.io/waves/{wave}?drop={drop_id}"
    tail = f"rank {rank} | {len(now)} voters" if rank else f"{len(now)} voters"

    prev = st.get(drop_id)
    st[drop_id] = {"votes": now, "total": total, "rank": rank, "at": int(time.time())}
    if prev is None:
        res = notify.ping(f"Watching {title}: {_fmt(total)} TDH", tail, click=click, tags="eyes")
        log("baseline recorded")
        return [res]

    old = prev.get("votes", {})
    lines = []
    for h, v in sorted(now.items(), key=lambda kv: -kv[1]):
        if h not in old:
            lines.append(f"New voter: {h} +{_fmt(v)} TDH")
        elif old[h] != v:
            d = v - old[h]
            lines.append(f"{h}: {_fmt(old[h])} -> {_fmt(v)} ({'+' if d > 0 else ''}{_fmt(d)})")
    for h, v in old.items():
        if h not in now:
            lines.append(f"{h} removed their {_fmt(v)} TDH")
    if not lines:
        log("no vote changes")
        return []
    d = total - int(prev.get("total", 0))
    head = f"{title}: {_fmt(total)} TDH ({'+' if d >= 0 else ''}{_fmt(d)})"
    res = notify.ping(head, "\n".join(lines + [tail]), click=click, tags="chart_with_upwards_trend" if d >= 0 else "chart_with_downwards_trend")
    log(f"{len(lines)} vote change(s) pinged")
    return [res]
