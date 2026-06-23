# AGENTS.md — AlphaCred Plugin Context

This file provides context for AI coding assistants working on the AlphaCred Canopy plugin.

---

## What This Plugin Does

AlphaCred is a Social-Fi trade call reputation system built as a Canopy plugin. Users publish directional trade calls (long/short) with entry, TP, and SL. Calls resolve against real-time prices. Reputation accumulates from accuracy. Token rewards are queued on TP hits.

---

## Stack

- **Language:** Python 3
- **Framework:** Flask
- **Port:** 50004 (plugin UI), 50002/50003 (Canopy node RPC)
- **Chain interaction:** JSON-RPC via `requests` to localhost:50002
- **State:** In-memory Python dicts (mirrors on-chain state)
- **Auth:** Session-based with PBKDF2 password hashing
- **Security:** flask-limiter, bleach sanitization, wallet validation

---

## File Structure

```
plugin.py          # Single-file plugin — all routes, state, and UI
requirements.txt   # Python dependencies
README.md          # Setup and documentation
AGENTS.md          # This file
```

---

## Canopy RPC Interface

The plugin communicates with the Canopy node via JSON-RPC:

```python
def rpc(method, params=None):
    # Tries endpoints: localhost:50003, localhost:50002
    # Methods used: plugin_submitTx, plugin_reward, send_tx, transfer, status
```

**Every custom tx route calls:**
```python
rpc("plugin_submitTx", [{"type": "<tx_type>", "data": <payload>}])
```

If RPC is unavailable, the plugin continues with simulated tx hashes.

---

## Custom Transaction Types

```
publish_call     — asset, direction, entry, tp, sl, expiry, trader
endorse_call     — call_id, endorser
record_outcome   — call_id, result (tp_hit|sl_hit|expired), price
react            — call_id, emoji (🔥|🎯|📉|💀), user
tip_trader       — from, to, amount
counter_call     — original_call_id, counter_call_id, user
add_comment      — call_id, text, username
follow_trader    — follower, target, action (followed|unfollowed)
```

---

## State Schema

```python
users = {
    "username": {
        "password": bytes,          # PBKDF2 hash
        "wallet": str,              # 40 hex chars, no 0x
        "wins": int,
        "losses": int,
        "rep": int,
        "streak": int,
        "tips_received": float,
        "bio": str,
        "admin": bool,
        "rep_bonus_claimed": bool
    }
}

calls = {
    "CALL_ID": {
        "call_id": str,
        "trader": str,
        "asset": str,               # BTC, ETH, SOL, BNB, GOLD, SILVER, WTI
        "direction": str,           # long | short
        "entry": float,
        "tp": float,
        "sl": float,
        "rr": float,
        "expiry": int,              # unix timestamp
        "status": str,              # open | tp_hit | sl_hit | expired
        "ts": int,
        "endorsements": int,
        "resolved_at": int | None,
        "price_at_resolve": float | None,
        "counter_to": str | None,   # call_id of original if this is a counter
        "reward_queued": bool
    }
}

follows    = { "username": ["username", ...] }
endorsed   = { "call_id": ["username", ...] }
comments   = { "call_id": [{"username", "text", "timestamp"}] }
reactions  = { "call_id": {"🔥": int, "🎯": int, "📉": int, "💀": int, "_users": {username: emoji}} }
tips_log   = [{"tipper", "target", "amount", "ts", "tx_hash"}]
pending_rewards = [{"user", "wallet", "amount", "reason", "timestamp", "status"}]
```

---

## Reputation System

| Action | Rep delta | Token |
|---|---|---|
| publish_call | +5 | — |
| endorse_call | +2 | — |
| resolve tp_hit | +10 | 10 ACRED queued |
| resolve sl_hit | -2 | — |
| resolve expired | -1 | — |
| reach 100 rep (once) | — | 10 ACRED queued |

---

## Wallet Format

Canopy wallets are 40 hex characters with no `0x` prefix.

```python
def normalize_wallet(address):
    addr = address.strip().lower()
    if addr.startswith('0x'):
        addr = addr[2:]
    return addr  # 40 hex chars

def is_valid_wallet(address):
    return bool(re.match(r'^[a-f0-9]{40}$', normalize_wallet(address)))
```

---

## Price Sources

Priority order per asset:
1. Twelve Data API (`TWELVE_DATA_API_KEY` env var)
2. Gold-API (GOLD, SILVER only)
3. CoinGecko (BTC, ETH, SOL, BNB)
4. Hardcoded fallback prices

Prices are cached for 120 seconds.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | Flask session secret |
| `TREASURY_WALLET` | Recommended | Admin wallet — enables pending rewards panel |
| `TWELVE_DATA_API_KEY` | Optional | Live price data for all assets |
| `FLASK_ENV` | Optional | Set to `production` to enable secure cookies |

---

## Key Constraints for AI Assistants

- All tx routes require `session.get("user")` — no unauthenticated tx submission
- Wallet addresses must pass `is_valid_wallet()` before storage or use
- Counter calls automatically flip direction and mirror TP/SL from original
- `pending_rewards` is append-only — use `status: "sent"` to mark as processed
- `check_rep_bonus()` must be called after any rep change that could trigger the milestone
- Rate limits: 10/min on most tx routes, 5/min on tip and claim_reward
- Never call `apply_reward()` directly without checking `can_reward()` — it's called inside `apply_reward` but the reward system has daily caps and cooldowns
