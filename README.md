# AlphaCred — Onchain Trade Call Reputation

> Publish trade calls onchain, earn rep from accuracy, tip traders, counter positions, and claim ACRED rewards when your calls hit TP.

AlphaCred is a full Social-Fi application built as a Canopy plugin. Traders publish directional calls (long/short) with entry, TP, and SL levels. Calls are resolved against real-time market prices. Reputation is earned through accuracy — not self-promotion.

---

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables:
   ```bash
   export SECRET_KEY="your-secret-key"
   export TREASURY_WALLET="your40hexwalletaddress"
   export TWELVE_DATA_API_KEY="your-api-key"  # optional, enables live prices
   ```

3. Start the Canopy node on port 50002, then run the plugin:
   ```bash
   python plugin.py
   ```

4. Open `http://localhost:50004`

---

## Reputation & Reward System

| Action | Rep | Token |
|---|---|---|
| Publish a call | +5 | — |
| Endorse a call | +2 | — |
| TP hit | +10 | 10 ACRED queued |
| SL hit | -2 | — |
| Call expired | -1 | — |
| Reach 100 rep | — | 10 ACRED one-time bonus |

Token rewards are queued in `pending_rewards` for manual payout via the Canopy Launchpad. This is intentional — the plugin submits all actions to the chain via RPC, but treasury disbursements require the admin wallet to sign outbound transfers.

---

## Custom Transaction Types

| Route | Type | Description |
|---|---|---|
| `POST /tx/publish_call` | `publish_call` | Publish a trade call with asset, direction, entry, TP, SL, expiry |
| `POST /tx/endorse_call` | `endorse_call` | Endorse another trader's open call |
| `POST /tx/resolve_call` | `record_outcome` | Resolve a call against real-time price |
| `POST /tx/react` | `react` | React to a call (🔥 🎯 📉 💀) |
| `POST /tx/tip` | `tip_trader` | Send ACRED tip directly to a trader |
| `POST /tx/counter_call` | `counter_call` | Create an opposing call against an open one |
| `POST /tx/add_comment` | `add_comment` | Comment on an open call |
| `POST /tx/follow` | `follow_trader` | Follow or unfollow a trader |
| `POST /tx/set_wallet` | — | Link a Canopy wallet address to account |

All tx routes submit to the Canopy node via JSON-RPC on port 50002/50003.

---

## Features

- Live price ticker (Twelve Data + CoinGecko + Gold-API + fallbacks)
- Rep leaderboard with win rate, streak, and badges
- Following feed — see only calls from traders you follow
- Hot tab — top calls by endorsements
- Activity feed — live event log
- Counter-call mechanic — opposing position linked to original
- Tip modal — peer ACRED transfers
- Admin reward queue — pending payouts visible to treasury wallet holder
- Session auth with hashed passwords, rate limiting, input sanitization

---

## RPC Integration

The plugin attempts to connect to the Canopy node on startup across multiple endpoints (`localhost:50003`, `localhost:50002`). Every custom transaction is submitted via `plugin_submitTx`. If the node is unreachable, rewards fall back to simulated tx hashes and the app continues running.

---

## Built for Canopy Vibe Code Contest #2 — Social-Fi Theme

