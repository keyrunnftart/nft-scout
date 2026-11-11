# nft-scout: notes for the next session

Primary-drop scout plus the wallet's own sale / bid pings, sent to the phone via ntfy.

## Setup
- **Cloud.** `.github/workflows/scout.yml` (`python -m nft_scout watch`). It is triggered every 5 min by cron-job.org job 8565652 via workflow_dispatch.
  - The fine-grained GitHub token for that trigger EXPIRES 31 Dec 2026. Renew it, or ticks fall back to roughly every 6 h.
- **State.** `data/` is carried between runs in the Actions cache (`scout-data-<run_id>`, restored by prefix).
  - Config lives in the private `SCOUT_STATE` secret. The ntfy topic is in `NTFY_TOPIC`.
- **Bid / sale pings** (`src/nft_scout/bids.py`) are wallet-first:
  1. Learn venues and listing keys from the wallet's own listing txs (explorer txlist, only when the wallet nonce moves).
  2. Read those venues' logs every tick.
- **Artist pings.**
  - `follow.py`: new mints by followed artists.
  - `scan.notify_live`: budget-fitting live mints from the top artists.

## Fixes
- **13 Nov 2026, commit f8bcca0.**
  - **Missed bid ping.** A Transient bid on Crossings (10 Nov) never pinged.
    - The token was minted and listed 2 min apart. One tick saw the new wallet nonce before the explorer had indexed the listing tx, but committed the nonce anyway, so the listing was not learned until the wallet's next tx.
    - Now the nonce is committed only when the explorer shows the wallet's newest tx (`seen_nonce >= nonce - 1`); otherwise it retries next tick.
    - Repro: `dev/repro_bid.py` replays 10 Nov and gets "new bid: Crossings · 0.015 ETH".
  - **Ping flood.** One HectorOZ drop sent ~200 pings.
    - Follow pings and live-mint pings were deduped per token.
    - Now there is ONE ping per collection, ever: `follow:<slug>`, `live:<slug>`. Collections pinged under the old per-token keys count as done.
- **Earlier** (the full log is in the user's Claude memory, `nft_scout_mcp.md`):
  - Wallet-first bid watch: 0742f2d.
  - SR / Transient auction bids: cb47dd9.
  - SR escrow = auction live: 8305d1f.
  - 6529 vote watch: d981556.
  - objkt non-JSON: 23ea508.
  - Never drop a ping on ntfy 429: 14df9e4.
