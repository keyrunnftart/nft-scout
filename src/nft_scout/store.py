"""Local state in D:\\nft-scout\\data: sqlite response cache + JSON files (config, positions, sent pings)."""

from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
STATE = ROOT / "state"           # config + positions: local only (gitignored); the cloud reads SCOUT_STATE
DATA.mkdir(exist_ok=True)
REPORTS.mkdir(exist_ok=True)
STATE.mkdir(exist_ok=True)

_local = threading.local()


def _db() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(DATA / "cache.sqlite", timeout=30)
        con.execute("pragma journal_mode=wal")
        con.execute("create table if not exists kv (k text primary key, v text, ts real)")
        _local.con = con
    return con


def cache_get(key: str, ttl: float | None) -> Any:
    row = _db().execute("select v, ts from kv where k=?", (key,)).fetchone()
    if row is None or (ttl is not None and time.time() - row[1] > ttl):
        return None
    return json.loads(row[0])


def cache_put(key: str, value: Any) -> None:
    con = _db()
    con.execute("insert or replace into kv values (?,?,?)", (key, json.dumps(value), time.time()))
    con.commit()


def prune_cache() -> None:
    """Drop expired API pages (event pages live 1h, everything else from OpenSea <= 1 day; mint receipts are
    kept forever) so the cache carried between cloud runs stays small."""
    now = time.time()
    con = _db()
    con.execute("delete from kv where k like 'os:events%' and ts < ?", (now - 3600,))
    con.execute("delete from kv where k like 'os:%' and k not like 'os:accounts/%' and ts < ?", (now - 86400,))
    con.execute("delete from kv where k like 'drops%' and ts < ?", (now - 3600,))
    con.commit()
    con.execute("vacuum")


def _load(name: str, default: Any, d: Path = DATA) -> Any:
    p = d / name
    return json.loads(p.read_text("utf-8")) if p.exists() else default


def _save(name: str, value: Any, d: Path = DATA) -> None:
    (d / name).write_text(json.dumps(value, indent=2), "utf-8")


# ---- config ---------------------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "budget_eth": 0.1,
    "max_per_position_eth": 0.05,
    "min_premium": 1.2,          # net resale / all-in mint cost
    "hold_days": 14,
    "lookback_days": 180,
    "chains": ["ethereum", "base", "shape", "zora", "arbitrum", "optimism", "abstract"],
    "categories": ["art", "pfps", "photography", "", None],
    "max_floor_eth": 0.1,        # skip collections whose current floor is above this (PFPs at 1 ETH etc.)
    "max_mint_eth": 0.1,         # drops whose all-in mint cost was above this don't count toward an artist
    "rescan_hours": 24,          # watch: full artist rescan at most this often; live checks every run
    "art_categories": ["art", "photography", ""],
    "art_lookback_days": 90,     # art pass: collections created in the last 3 months
    "art_pages": 8,              # x100 collections per chain per volume ranking
    "art_min_sales_30d": 5,      # art trades thinner than pfps
    "min_sales_14d": 8,          # secondary sales needed in a drop's first 14 days to judge it
    "watch_artists": [],         # OpenSea usernames / addresses always included in scans
    "follow_artists": [],        # ping on any new mint by these artists, flip score or not
    "ignore_slugs": [],
    # wallet watch: pings when the user's own work sells (primary/secondary) or an auction starts
    # (all personal values live in the private SCOUT_STATE secret / local state/config.json, never in the repo)
    "my_wallet": "",
    "my_tezos": "",
    # contracts not created by my_wallet on OpenSea; mute_mints when another watcher already pings their mints
    "my_contracts": [],
}


def _cloud_state() -> dict:
    """The cloud run gets config + positions from the SCOUT_STATE secret (the repo is public)."""
    try:
        return json.loads(os.environ.get("SCOUT_STATE") or "{}")
    except ValueError:
        return {}


def sync_cloud() -> str:
    """Push local config + positions into the repo's SCOUT_STATE secret (watch list and mints stay private)."""
    body = json.dumps({"config": _load("config.json", {}, STATE), "positions": _load("positions.json", [], STATE)})
    try:
        r = subprocess.run(["gh", "secret", "set", "SCOUT_STATE", "-R", "keyrunnftart/nft-scout"], input=body,
                           capture_output=True, text=True, timeout=60)
        return "synced" if r.returncode == 0 else f"sync failed: {r.stderr.strip()[:200]}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"sync failed: {e}"


def config() -> dict:
    return {**DEFAULT_CONFIG, **_cloud_state().get("config", {}), **_load("config.json", {}, STATE)}


def update_config(patch: dict) -> dict:
    cur = _load("config.json", {}, STATE)
    cur.update(patch)
    _save("config.json", cur, STATE)
    sync_cloud()
    return config()


# ---- positions / outcomes ----------------------------------------------------------------------------

def positions() -> list[dict]:
    return _load("positions.json", None, STATE) or _cloud_state().get("positions", [])


def save_positions(items: list[dict]) -> None:
    _save("positions.json", items, STATE)
    sync_cloud()


# ---- latest scan + pings already sent -----------------------------------------------------------------

def save_scan(scan: dict) -> None:
    _save("last_scan.json", scan)


def last_scan() -> dict:
    return _load("last_scan.json", {})


def sent() -> dict:
    return _load("sent.json", {})


def mark_sent(key: str) -> None:
    s = sent()
    s[key] = int(time.time())
    _save("sent.json", s)


def follow_state() -> dict:
    return _load("follow_state.json", {})


def save_follow_state(st: dict) -> None:
    _save("follow_state.json", st)


def wallet_state() -> dict:
    return _load("wallet_state.json", {})


def save_wallet_state(st: dict) -> None:
    _save("wallet_state.json", st)
