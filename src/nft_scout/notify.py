"""Phone pings through ntfy (same pattern as the ABX cloud watcher). env: NTFY_TOPIC."""

from __future__ import annotations

import os
import time

import httpx

from . import store


def ping(title: str, text: str, click: str | None = None, urgent: bool = False, tags: str = "moneybag",
         dedupe_key: str | None = None) -> dict:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return {"sent": False, "why": "NTFY_TOPIC missing in D:\\nft-scout\\.env"}
    if dedupe_key and dedupe_key in store.sent():
        return {"sent": False, "why": "already pinged", "key": dedupe_key}
    h = {"Title": title.encode("utf-8"), "Priority": "urgent" if urgent else "high", "Tags": tags}
    if click:
        h["Click"] = click
    # ntfy.sh rate-limits per IP and GitHub runners share IPs: back off on 429/5xx instead of dropping the ping
    for attempt in range(4):
        r = httpx.post(f"https://ntfy.sh/{topic}", content=text.encode("utf-8"), headers=h, timeout=15)
        if r.status_code != 429 and r.status_code < 500:
            break
        time.sleep(min(float(r.headers.get("Retry-After") or 0) or 5 * 2 ** attempt, 40))
    r.raise_for_status()
    if dedupe_key:
        store.mark_sent(dedupe_key)
    return {"sent": True}


def opportunity_text(o: dict) -> tuple[str, str]:
    title = f"mint: {o['name']}" + (f" #{o['token']}" if o.get("token") else "") + f" .. {o['chain']}"
    lines = [
        f"{o['artist']} · score {o['artist_score']} · {o['confidence']}",
        f"cost {o['all_in']:.4f} eth all-in (mint {o['mint_value']:.4f})",
        f"track record: {int(o['artist_consistency'] * 100)}% drops hit, median {o['artist_median_premium']}x net",
        f"sell target ≥ {o['target_exit']:.4f} eth within 14d",
    ]
    if o.get("best_offer"):
        lines.append(f"best offer now {o['best_offer']:.4f} ({o['offer_net_vs_cost']}x net)")
    lines.append(f"{o['mints_6h']} mints in 6h, last {o['last_mint_min_ago']} min ago")
    return title, "\n".join(lines)
