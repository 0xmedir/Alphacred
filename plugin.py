import os
import time
import uuid
import hashlib
import bleach
import requests
import logging
import re
import random
from flask import Flask, request, jsonify, render_template_string, session, redirect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logging.getLogger("urllib3").setLevel(logging.WARNING)

app = Flask(__name__)

# -------------------- SECRET KEY (from env or random) --------------------
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    print("\n⚠️  WARNING: SECRET_KEY not set. Sessions will be invalid after restart.")
    print("   Set it with: export SECRET_KEY=\"your-secret-key\"\n")
    app.secret_key = os.urandom(24)

# -------------------- SESSION HARDENING (only in production) --------------------
SESSION_COOKIE_SECURE = os.environ.get("FLASK_ENV") == "production"
app.config.update(
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)

# -------------------- RATE LIMITING (higher limits, GET routes exempted) --------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["1000 per day", "200 per hour"])
limiter.init_app(app)

# -------------------- CONFIG (All from environment) --------------------
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TREASURY_WALLET = os.environ.get("TREASURY_WALLET", "")

if not TREASURY_WALLET:
    print("\n⚠️  WARNING: TREASURY_WALLET not set. Admin features (pending rewards) will be disabled.")
    print("   Set it with: export TREASURY_WALLET=\"your_canopy_address\"\n")

if TREASURY_WALLET:
    TREASURY_WALLET = TREASURY_WALLET.lower().strip()
    if TREASURY_WALLET.startswith('0x'):
        TREASURY_WALLET = TREASURY_WALLET[2:]

REWARD_POOL = 100000
DAILY_USER_CAP = 50
DAILY_GLOBAL_CAP = 5000
MIN_REP_FOR_FULL = 10
COOLDOWN_SECONDS = 5

REWARDS = {"tp_hit": 10, "endorse": 1, "publish": 2}
REP_BONUS_THRESHOLD = 100
REP_BONUS_AMOUNT = 10

daily_user_rewards = {}
daily_global_rewards = 0
last_reward_time = {}
reward_claim_count = {}

SYMBOL_MAP = {
    "BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "BNB": "BNB/USD",
    "GOLD": "XAU/USD", "SILVER": "XAG/USD", "WTI": "WTI/USD"
}
CG_MAP = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin"}
FALLBACK_PRICES = {"BTC": 65000, "ETH": 3200, "SOL": 140, "BNB": 600, "GOLD": 1950.0, "SILVER": 25.5, "WTI": 78.0}
PRICE_CACHE = {}
CACHE_TTL = 120

VALID_REACTIONS = ["🔥", "🎯", "📉", "💀"]

users = {}
calls = {}
follows = {}
endorsed = {}
comments = {}
reactions = {}
tips_log = []
feed = []
pending_rewards = []

# -------------------- SANITIZATION --------------------
def sanitize(text):
    return bleach.clean(text, tags=[], strip=True) if text else ""

# -------------------- WALLET NORMALIZATION & VALIDATION --------------------
def normalize_wallet(address):
    if not address:
        return ""
    addr = address.strip().lower()
    if addr.startswith('0x'):
        addr = addr[2:]
    return addr

def is_valid_wallet(address):
    if not address:
        return False
    addr = normalize_wallet(address)
    return bool(re.match(r'^[a-f0-9]{40}$', addr))

def is_wallet_used_by_other(address, exclude_user=None):
    norm = normalize_wallet(address)
    for u, data in users.items():
        if exclude_user and u == exclude_user:
            continue
        if data.get("wallet") == norm:
            return True
    return False

# -------------------- ADMIN DETECTION --------------------
def is_admin(username):
    if not TREASURY_WALLET:
        return False
    if username not in users:
        return False
    return users[username].get("wallet") == TREASURY_WALLET

# -------------------- PASSWORD HASHING --------------------
def hash_password(password):
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt + key

def verify_password(stored, password):
    salt = stored[:16]
    key = stored[16:]
    new_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return key == new_key

# -------------------- HELPERS --------------------
def add_feed(text):
    feed.insert(0, f"[{time.strftime('%H:%M')}] {text}")
    if len(feed) > 60:
        feed.pop()

def tx_hash(seed):
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()[:64]

def init_reactions(call_id):
    if call_id not in reactions:
        reactions[call_id] = {e: 0 for e in VALID_REACTIONS}
        reactions[call_id]["_users"] = {}

# -------------------- REPUTATION BONUS CHECK --------------------
def check_rep_bonus(user):
    if user not in users:
        return
    if users[user].get("rep_bonus_claimed", False):
        return
    if users[user]["rep"] >= REP_BONUS_THRESHOLD:
        users[user]["rep_bonus_claimed"] = True
        wallet = users[user].get("wallet")
        if wallet and is_valid_wallet(wallet):
            apply_reward(user, wallet, REP_BONUS_AMOUNT, "rep_bonus")
            add_feed(f"🎉 {user} reached 100 rep and earned bonus reward!")
        else:
            add_feed(f"ℹ️ {user} reached 100 rep but no valid wallet – bonus skipped")

# -------------------- AVATAR GENERATOR --------------------
def generate_avatar(username):
    if not username:
        return "👤"
    h = int(hashlib.md5(username.encode()).hexdigest()[:6], 16)
    colors = ['#ff6b6b', '#feca57', '#48dbfb', '#1dd1a1', '#5f27cd', '#ff9ff3', '#ff6348', '#00d2d3']
    color = colors[h % len(colors)]
    initial = username[0].upper()
    return f'<span style="display:inline-block;width:28px;height:28px;border-radius:50%;background:{color};color:white;text-align:center;line-height:28px;font-family:monospace;font-size:14px;font-weight:bold;">{initial}</span>'

# -------------------- RPC (attempts admin port) --------------------
def rpc(method, params=None):
    endpoints = [
        ("http://localhost:50003", "/"),
        ("http://localhost:50003", "/rpc"),
        ("http://localhost:50002", "/"),
        ("http://localhost:50002", "/rpc"),
        ("http://localhost:50002", "/v1/rpc"),
    ]
    for base, path in endpoints:
        try:
            url = f"{base}{path}"
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code == 200:
                return r.json()
        except:
            continue
    return {"error": "rpc_unavailable"}

def send_onchain_tx(frm, to, amount, token, reason):
    res = rpc("plugin_reward", [{
        "from": frm,
        "to": to,
        "amount": f"{amount}{token.lower()}",
        "reason": reason
    }])
    if "result" in res or "tx_hash" in res:
        return res
    res = rpc("send_tx", [{
        "from": frm,
        "to": to,
        "amount": f"{amount}{token.lower()}"
    }])
    if "result" in res or "tx_hash" in res:
        return res
    res = rpc("transfer", [{
        "from": frm,
        "to": to,
        "amount": f"{amount}{token.lower()}"
    }])
    return res

# -------------------- REWARD SYSTEM (queued) --------------------
def can_reward(user):
    global daily_global_rewards
    if REWARD_POOL <= 0:
        return False, "Pool empty"
    if time.time() - last_reward_time.get(user, 0) < COOLDOWN_SECONDS:
        return False, "Cooldown"
    dk = user + str(int(time.time() // 86400))
    if daily_user_rewards.get(dk, 0) >= DAILY_USER_CAP:
        return False, "User cap"
    if daily_global_rewards >= DAILY_GLOBAL_CAP:
        return False, "Global cap"
    return True, "ok"

def get_diminished_amount(base_amount, user):
    dk = user + str(int(time.time() // 86400))
    count = reward_claim_count.get(dk, 0)
    factor = max(0.2, 1.0 - (count * 0.1))
    return round(base_amount * factor, 2)

def apply_reward(user, wallet, base_amount, reason):
    global REWARD_POOL, daily_global_rewards
    norm_wallet = normalize_wallet(wallet)
    if not norm_wallet:
        return {"success": False, "reason": "No wallet set"}
    if not is_valid_wallet(norm_wallet):
        return {"success": False, "reason": "Invalid wallet address"}

    ok, msg = can_reward(user)
    if not ok:
        return {"success": False, "reason": msg}

    rep = users.get(user, {}).get("rep", 0)
    amount = base_amount
    if rep < MIN_REP_FOR_FULL:
        amount = round(base_amount * 0.5, 2)
    amount = get_diminished_amount(amount, user)
    if amount <= 0:
        return {"success": False, "reason": "Amount too small"}

    REWARD_POOL -= amount
    daily_global_rewards += amount
    dk = user + str(int(time.time() // 86400))
    daily_user_rewards[dk] = daily_user_rewards.get(dk, 0) + amount
    reward_claim_count[dk] = reward_claim_count.get(dk, 0) + 1
    last_reward_time[user] = time.time()

    pending_rewards.append({
        "user": user,
        "wallet": norm_wallet,
        "amount": amount,
        "reason": reason,
        "timestamp": int(time.time()),
        "status": "pending"
    })

    h = tx_hash(f"{norm_wallet}{amount}{time.time()}")
    return {"success": True, "amount": amount, "tx_hash": h, "real": False}

# -------------------- PRICE FUNCTIONS --------------------
def get_price(asset):
    asset = asset.upper()
    now = time.time()
    if asset in PRICE_CACHE and now - PRICE_CACHE[asset]['ts'] < CACHE_TTL:
        return PRICE_CACHE[asset]['price']
    price = None
    sym = SYMBOL_MAP.get(asset)
    if sym and TWELVE_DATA_API_KEY:
        for endpoint in [f"quote?symbol={sym}", f"price?symbol={sym}"]:
            try:
                r = requests.get(f"https://api.twelvedata.com/{endpoint}&apikey={TWELVE_DATA_API_KEY}", timeout=5)
                d = r.json()
                p = d.get("close") or d.get("price")
                if p:
                    price = float(p)
                    break
            except:
                pass
    if price is None and asset == "GOLD":
        try:
            r = requests.get("https://api.gold-api.com/price/XAU", timeout=5)
            p = r.json().get("price")
            if p:
                price = float(p)
        except:
            pass
    elif price is None and asset == "SILVER":
        try:
            r = requests.get("https://api.gold-api.com/price/XAG", timeout=5)
            p = r.json().get("price")
            if p:
                price = float(p)
        except:
            pass
    if price is None and asset in CG_MAP:
        try:
            r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={CG_MAP[asset]}&vs_currencies=usd", timeout=5)
            p = r.json().get(CG_MAP[asset], {}).get("usd")
            if p:
                price = float(p)
        except:
            pass
    if price is None and asset in FALLBACK_PRICES:
        price = FALLBACK_PRICES[asset]
    PRICE_CACHE[asset] = {'price': price, 'ts': now}
    return price

def all_prices():
    return {a: get_price(a) for a in SYMBOL_MAP}

# -------------------- SEED DEMO DATA (fixed hashed passwords) --------------------
def seed_demo_data():
    if len(calls) > 0:
        return
    now = int(time.time())
    assets = ["BTC", "ETH", "GOLD", "WTI"]
    users_data = [
        {"username": "alice", "wallet": "a"*40, "rep": 120, "wins": 15, "losses": 5, "streak": 3},
        {"username": "bob", "wallet": "b"*40, "rep": 80, "wins": 8, "losses": 4, "streak": 1},
        {"username": "charlie", "wallet": "c"*40, "rep": 200, "wins": 25, "losses": 10, "streak": 5},
        {"username": "diana", "wallet": "d"*40, "rep": 50, "wins": 4, "losses": 6, "streak": 0},
    ]
    for u in users_data:
        if u["username"] not in users:
            users[u["username"]] = {
                "password": hash_password("demo"),
                "wallet": u["wallet"],
                "wins": u["wins"],
                "losses": u["losses"],
                "rep": u["rep"],
                "streak": u["streak"],
                "tips_received": 0,
                "bio": f"Trader {u['username']}",
                "avatar": "",
                "admin": False,
                "rep_bonus_claimed": False
            }
    # Create only 3 demo calls
    for i in range(3):
        asset = random.choice(assets)
        direction = random.choice(["long", "short"])
        entry = random.uniform(50, 70000)
        if direction == "long":
            tp = entry * (1 + random.uniform(0.01, 0.05))
            sl = entry * (1 - random.uniform(0.01, 0.03))
        else:
            tp = entry * (1 - random.uniform(0.01, 0.05))
            sl = entry * (1 + random.uniform(0.01, 0.03))
        if direction == "long" and not (sl < entry < tp):
            continue
        if direction == "short" and not (tp < entry < sl):
            continue
        trader = random.choice(users_data)["username"]
        statuses = ["open", "tp_hit", "sl_hit", "expired"]
        weights = [0.5, 0.3, 0.15, 0.05]
        status = random.choices(statuses, weights=weights)[0]
        call_id = str(uuid.uuid4())[:8].upper()
        rr = round(abs(tp - entry) / abs(sl - entry), 2) if sl != entry else 0
        call = {
            "call_id": call_id,
            "trader": trader,
            "asset": asset,
            "direction": direction,
            "entry": round(entry, 2),
            "tp": round(tp, 2),
            "sl": round(sl, 2),
            "rr": rr,
            "expiry": now + 3600 * 24 * random.randint(1, 3),
            "status": status,
            "ts": now - random.randint(0, 86400 * 7),
            "endorsements": random.randint(0, 5),
            "resolved_at": now - random.randint(0, 86400 * 2) if status != "open" else None,
            "price_at_resolve": random.uniform(50, 70000) if status != "open" else None,
            "counter_to": None,
            "reward_queued": False
        }
        if status != "open":
            call["resolved_at"] = now - random.randint(0, 86400 * 2)
            call["price_at_resolve"] = call["tp"] if status == "tp_hit" else call["sl"] if status == "sl_hit" else random.uniform(entry*0.95, entry*1.05)
        calls[call_id] = call
        endorsed[call_id] = []
        comments[call_id] = []
        init_reactions(call_id)
        commenters = ["alice", "bob", "charlie"]
        com = {
            "username": random.choice(commenters),
            "text": f"Demo comment for {call_id}",
            "timestamp": now - random.randint(0, 3600)
        }
        comments[call_id].append(com)
        for emoji in VALID_REACTIONS:
            reactions[call_id][emoji] = random.randint(0, 2)
    add_feed("🎲 Seeded 3 demo calls")

# -------------------- AUTH (with wallet uniqueness & admin) --------------------
@app.route("/auth", methods=["POST"])
def auth():
    d = request.json or {}
    action = d.get("action")
    username = sanitize(d.get("username"))
    password = d.get("password", "")
    wallet = d.get("wallet", "").strip()
    if action == "signup":
        if username in users:
            return jsonify({"status": "error", "msg": "Username taken"})
        if not username or not password:
            return jsonify({"status": "error", "msg": "Username and password required"})
        if wallet:
            wallet = normalize_wallet(wallet)
            if not is_valid_wallet(wallet):
                return jsonify({"status": "error", "msg": "Invalid wallet address format"})
            if is_wallet_used_by_other(wallet):
                return jsonify({"status": "error", "msg": "Wallet address already used by another user"})
        hashed = hash_password(password)
        user_data = {
            "password": hashed,
            "wallet": wallet,
            "wins": 0,
            "losses": 0,
            "rep": 0,
            "streak": 0,
            "tips_received": 0,
            "bio": "",
            "avatar": "",
            "admin": False,
            "rep_bonus_claimed": False
        }
        if TREASURY_WALLET and wallet == TREASURY_WALLET:
            user_data["admin"] = True
        users[username] = user_data
        add_feed(f"🎉 {username} joined!")
        session['user'] = username
        return jsonify({"status": "success", "user": username})
    elif action == "login":
        if username not in users:
            return jsonify({"status": "error", "msg": "Invalid credentials"}), 401
        stored = users[username]["password"]
        if not verify_password(stored, password):
            return jsonify({"status": "error", "msg": "Invalid credentials"}), 401
        session['user'] = username
        return jsonify({"status": "success", "user": username})
    return jsonify({"status": "error", "msg": "Invalid action"}), 400

@app.route("/api/me")
def api_me():
    u = session.get('user')
    if u and u in users:
        return jsonify({
            "username": u,
            "admin": users[u].get("admin", False)
        })
    return jsonify({"username": None}), 401

@app.route("/logout", methods=["POST"])
def logout():
    session.pop('user', None)
    return jsonify({"status": "success"})

# -------------------- PROFILE --------------------
@app.route("/api/profile/<username>")
def api_profile(username):
    if username not in users:
        return jsonify({"error": "Not found"}), 404
    u = users[username]
    followers = [f for f, fl in follows.items() if username in fl]
    return jsonify({
        "username": username,
        "bio": u.get("bio", ""),
        "avatar": u.get("avatar", ""),
        "rep": u["rep"],
        "wins": u["wins"],
        "losses": u["losses"],
        "streak": u.get("streak", 0),
        "tips_received": u.get("tips_received", 0),
        "wallet": u.get("wallet", ""),
        "followers": followers,
        "following": follows.get(username, [])
    })

@limiter.limit("10 per minute")
@app.route("/tx/update_profile", methods=["POST"])
def update_profile():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    bio = sanitize(d.get("bio", ""))
    avatar = sanitize(d.get("avatar", ""))
    users[user]["bio"] = bio
    if avatar:
        users[user]["avatar"] = avatar
    add_feed(f"✏️ {user} updated their profile")
    return jsonify({"ok": True})

# -------------------- TX: publish_call --------------------
@limiter.limit("10 per minute")
@app.route("/tx/publish_call", methods=["POST"])
def publish_call():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    required = ["asset", "direction", "entry", "tp", "sl", "expiry"]
    if any(k not in d for k in required):
        return jsonify({"error": "Missing fields"}), 400
    try:
        entry = float(d["entry"])
        tp = float(d["tp"])
        sl = float(d["sl"])
        expiry = int(d["expiry"])
    except:
        return jsonify({"error": "Invalid numbers"}), 400
    if d["direction"] not in ("long", "short"):
        return jsonify({"error": "Invalid direction"}), 400
    if d["direction"] == "long" and not (sl < entry < tp):
        return jsonify({"error": "Long: SL < entry < TP"}), 400
    if d["direction"] == "short" and not (tp < entry < sl):
        return jsonify({"error": "Short: TP < entry < SL"}), 400

    call_id = str(uuid.uuid4())[:8].upper()
    rr = round(abs(tp - entry) / abs(sl - entry), 2) if sl != entry else 0

    call = {
        "call_id": call_id,
        "trader": user,
        "asset": d["asset"].upper(),
        "direction": d["direction"],
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "rr": rr,
        "expiry": expiry,
        "status": "open",
        "ts": int(time.time()),
        "endorsements": 0,
        "resolved_at": None,
        "price_at_resolve": None,
        "counter_to": d.get("counter_to", None)
    }
    calls[call_id] = call
    endorsed[call_id] = []
    comments[call_id] = []
    init_reactions(call_id)
    add_feed(f"{user} published {call['asset']} {call['direction'].upper()} #{call_id}")
    try:
        rpc("plugin_submitTx", [{"type": "publish_call", "data": call}])
    except:
        pass
    users[user]["rep"] += 5
    check_rep_bonus(user)
    return jsonify({"ok": True, "call_id": call_id, "rr": rr})

# -------------------- TX: endorse_call --------------------
@limiter.limit("10 per minute")
@app.route("/tx/endorse_call", methods=["POST"])
def endorse_call():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request body"}), 400
    call_id = d.get("call_id")
    if not call_id:
        return jsonify({"error": "Missing call_id"}), 400
    if call_id not in calls:
        return jsonify({"error": "Call not found"}), 404
    call = calls[call_id]
    if call["status"] != "open":
        return jsonify({"error": "Call not open"}), 400
    if user == call["trader"]:
        return jsonify({"error": "Cannot endorse own call"}), 400
    if user in endorsed.get(call_id, []):
        return jsonify({"error": "Already endorsed"}), 400
    endorsed[call_id].append(user)
    call["endorsements"] += 1
    add_feed(f"{user} endorsed {call['trader']}'s #{call_id}")
    try:
        rpc("plugin_submitTx", [{"type": "endorse_call", "data": d}])
    except:
        pass
    users[user]["rep"] += 2
    check_rep_bonus(user)
    return jsonify({"ok": True, "endorsements": call["endorsements"]})

# -------------------- TX: resolve_call --------------------
@limiter.limit("10 per minute")
@app.route("/tx/resolve_call", methods=["POST"])
def resolve_call():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request body"}), 400
    call_id = d.get("call_id")
    if not call_id:
        return jsonify({"error": "Missing call_id"}), 400
    if call_id not in calls:
        return jsonify({"error": "Call not found"}), 404
    call = calls[call_id]
    if call["status"] != "open":
        return jsonify({"error": "Already resolved"}), 400
    if user != call["trader"]:
        return jsonify({"error": "Only trader can resolve"}), 403

    price = get_price(call["asset"])
    if price is None:
        return jsonify({"error": "Could not fetch price"}), 400
    call["price_at_resolve"] = price

    direction = call["direction"]
    entry = call["entry"]
    tp = call["tp"]
    sl = call["sl"]
    now = int(time.time())

    if direction == "long":
        if price >= tp:
            result = "tp_hit"
        elif price <= sl:
            result = "sl_hit"
        elif now >= call["expiry"]:
            result = "expired"
        else:
            return jsonify({"error": f"Price ${price} between SL/TP — not resolved"}), 400
    else:
        if price <= tp:
            result = "tp_hit"
        elif price >= sl:
            result = "sl_hit"
        elif now >= call["expiry"]:
            result = "expired"
        else:
            return jsonify({"error": f"Price ${price} between SL/TP — not resolved"}), 400

    call["status"] = result
    call["resolved_at"] = now
    u = users[user]

    reward_tx_hash = None
    if result == "tp_hit":
        u["wins"] += 1
        u["rep"] += 10
        u["streak"] = u.get("streak", 0) + 1
        check_rep_bonus(user)
        wallet = u.get("wallet")
        if wallet:
            reward_res = apply_reward(user, wallet, REWARDS["tp_hit"], "tp_hit")
            if reward_res.get("success"):
                reward_tx_hash = reward_res.get("tx_hash")
                call["reward_queued"] = True
                add_feed(f"🎯 {user} hit TP on {call['asset']} at ${price} – reward queued for manual payout")
            else:
                add_feed(f"⚠️ Reward queue failed for {user}: {reward_res.get('reason')}")
        else:
            add_feed(f"ℹ️ {user} hit TP on {call['asset']} but no wallet set for reward")
    elif result == "sl_hit":
        u["losses"] += 1
        u["rep"] -= 2
        u["streak"] = 0
        add_feed(f"❌ {user} hit SL on {call['asset']} at ${price}")
    elif result == "expired":
        u["rep"] -= 1
        u["streak"] = 0
        add_feed(f"⌛ {user}'s {call['asset']} #{call_id} expired at ${price}")

    try:
        rpc("plugin_submitTx", [{"type": "record_outcome", "data": {"call_id": call_id, "result": result, "price": price}}])
    except:
        pass
    return jsonify({"ok": True, "result": result, "price": price, "tx_hash": reward_tx_hash})

# -------------------- TX: claim_reward --------------------
@limiter.limit("5 per minute")
@app.route("/tx/claim_reward", methods=["POST"])
def claim_reward():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    call_id = d.get("call_id")
    if not call_id:
        return jsonify({"error": "Missing call_id"}), 400
    call = calls.get(call_id)
    if not call:
        return jsonify({"error": "Call not found"}), 404
    if call["status"] != "tp_hit":
        return jsonify({"error": "Reward only available for TP hit"}), 400
    if call.get("reward_queued"):
        return jsonify({"error": "Reward already queued"}), 400

    wallet = users[user].get("wallet")
    if not wallet:
        return jsonify({"error": "Set your wallet first"}), 400
    if not is_valid_wallet(wallet):
        return jsonify({"error": "Invalid wallet address"}), 400

    reward_res = apply_reward(user, wallet, REWARDS["tp_hit"], "tp_hit")
    if not reward_res.get("success"):
        return jsonify({"error": reward_res.get("reason")}), 400

    call["reward_queued"] = True
    add_feed(f"💰 {user} claimed reward for #{call_id}")
    return jsonify({"ok": True, "tx_hash": reward_res.get("tx_hash")})

# -------------------- TX: set_wallet (with uniqueness) --------------------
@limiter.limit("10 per minute")
@app.route("/tx/set_wallet", methods=["POST"])
def set_wallet():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request"}), 400
    address = d.get("address", "").strip()
    if not address:
        return jsonify({"error": "Address required"}), 400
    address = normalize_wallet(address)
    if not is_valid_wallet(address):
        return jsonify({"error": "Invalid wallet address. Must be 40 hex chars (no 0x)"}), 400
    if is_wallet_used_by_other(address, exclude_user=user):
        return jsonify({"error": "Wallet address already used by another user"}), 400
    users[user]["wallet"] = address
    if TREASURY_WALLET and address == TREASURY_WALLET:
        users[user]["admin"] = True
    add_feed(f"🔑 {user} updated wallet")
    return jsonify({"ok": True})

# -------------------- TX: follow (toggle) --------------------
@limiter.limit("10 per minute")
@app.route("/tx/follow", methods=["POST"])
def follow_trader():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request"}), 400
    target = sanitize(d.get("target", ""))
    if not target or target not in users:
        return jsonify({"error": "Invalid target"}), 400
    if user == target:
        return jsonify({"error": "Cannot follow yourself"}), 400

    fl = follows.setdefault(user, [])
    if target in fl:
        fl.remove(target)
        action = "unfollowed"
        add_feed(f"{user} unfollowed {target}")
    else:
        fl.append(target)
        action = "followed"
        add_feed(f"{user} followed {target}")

    try:
        rpc("plugin_submitTx", [{"type": "follow_trader", "data": {"follower": user, "target": target, "action": action}}])
    except:
        pass
    return jsonify({"ok": True, "action": action})

# -------------------- TX: add_comment --------------------
@limiter.limit("10 per minute")
@app.route("/tx/add_comment", methods=["POST"])
def add_comment():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request"}), 400
    call_id = d.get("call_id")
    text = sanitize(d.get("text", ""))
    if not call_id or call_id not in calls:
        return jsonify({"error": "Call not found"}), 404
    if not text:
        return jsonify({"error": "Empty comment"}), 400
    if calls[call_id]["status"] != "open":
        return jsonify({"error": "Cannot comment on resolved call"}), 400
    comment = {"username": user, "text": text, "timestamp": int(time.time())}
    if call_id not in comments:
        comments[call_id] = []
    comments[call_id].append(comment)
    add_feed(f"💬 {user} commented on call #{call_id}")
    try:
        rpc("plugin_submitTx", [{"type": "add_comment", "data": d}])
    except:
        pass
    return jsonify({"ok": True, "comment": comment})

# -------------------- TX: react --------------------
@limiter.limit("10 per minute")
@app.route("/tx/react", methods=["POST"])
def react():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request"}), 400
    call_id = d.get("call_id")
    emoji = d.get("emoji")
    if not call_id or call_id not in calls:
        return jsonify({"error": "Call not found"}), 404
    if emoji not in VALID_REACTIONS:
        return jsonify({"error": f"Use: {VALID_REACTIONS}"}), 400

    init_reactions(call_id)
    prev = reactions[call_id]["_users"].get(user)
    if prev == emoji:
        reactions[call_id][emoji] = max(0, reactions[call_id][emoji] - 1)
        del reactions[call_id]["_users"][user]
        action = "removed"
    else:
        if prev:
            reactions[call_id][prev] = max(0, reactions[call_id][prev] - 1)
        reactions[call_id][emoji] += 1
        reactions[call_id]["_users"][user] = emoji
        action = "added"

    try:
        rpc("plugin_submitTx", [{"type": "react", "data": {"call_id": call_id, "emoji": emoji, "user": user}}])
    except:
        pass
    totals = {e: reactions[call_id][e] for e in VALID_REACTIONS}
    return jsonify({
        "ok": True,
        "action": action,
        "reactions": totals,
        "user_reaction": reactions[call_id]["_users"].get(user, "")
    })

# -------------------- TX: tip --------------------
@limiter.limit("5 per minute")
@app.route("/tx/tip", methods=["POST"])
def tip_trader():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request"}), 400
    target = sanitize(d.get("target"))
    try:
        amount = float(d.get("amount", 1))
    except:
        return jsonify({"error": "Invalid amount"}), 400
    if not target or target not in users:
        return jsonify({"error": "Trader not found"}), 404
    if target == user:
        return jsonify({"error": "Cannot tip yourself"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400
    target_wallet = users[target].get("wallet")
    if not target_wallet:
        return jsonify({"error": "Target has no wallet set"}), 400
    if not is_valid_wallet(target_wallet):
        return jsonify({"error": "Target wallet invalid"}), 400
    sender_wallet = users[user].get("wallet")
    if not sender_wallet:
        return jsonify({"error": "Set your wallet first"}), 400
    if not is_valid_wallet(sender_wallet):
        return jsonify({"error": "Your wallet is invalid"}), 400

    res = send_onchain_tx(sender_wallet, target_wallet, amount, "ACRED", f"tip_from_{user}")
    h = res.get("tx_hash") or tx_hash(f"{user}{target}{amount}{time.time()}")

    users[target]["tips_received"] = users[target].get("tips_received", 0) + amount
    users[target]["rep"] += 1
    tips_log.append({"tipper": user, "target": target, "amount": amount, "ts": int(time.time()), "tx_hash": h})
    add_feed(f"💸 {user} tipped {amount} ACRED → {target}")
    try:
        rpc("plugin_submitTx", [{"type": "tip_trader", "data": {"from": user, "to": target, "amount": amount}}])
    except:
        pass
    return jsonify({"ok": True, "tx_hash": h, "amount": amount})

# -------------------- TX: counter_call --------------------
@limiter.limit("10 per minute")
@app.route("/tx/counter_call", methods=["POST"])
def counter_call():
    user = session.get("user")
    if not user or user not in users:
        return jsonify({"error": "Unauthorized"}), 401
    d = request.json or {}
    if not d:
        return jsonify({"error": "Empty request"}), 400
    original_id = d.get("original_call_id")
    if not original_id:
        return jsonify({"error": "Missing original_call_id"}), 400
    original = calls.get(original_id)
    if not original:
        return jsonify({"error": "Original call not found"}), 404
    if original["status"] != "open":
        return jsonify({"error": "Can only counter open calls"}), 400
    if original["trader"] == user:
        return jsonify({"error": "Cannot counter own call"}), 400

    opp = "short" if original["direction"] == "long" else "long"
    entry = original["entry"]
    tp, sl = original["sl"], original["tp"]
    call_id = str(uuid.uuid4())[:8].upper()
    rr = round(abs(tp - entry) / abs(sl - entry), 2) if sl != entry else 0

    call = {
        "call_id": call_id,
        "trader": user,
        "asset": original["asset"],
        "direction": opp,
        "entry": round(entry, 4),
        "tp": round(tp, 4),
        "sl": round(sl, 4),
        "rr": rr,
        "expiry": original["expiry"],
        "status": "open",
        "ts": int(time.time()),
        "endorsements": 0,
        "resolved_at": None,
        "price_at_resolve": None,
        "counter_to": original_id
    }
    calls[call_id] = call
    endorsed[call_id] = []
    comments[call_id] = []
    init_reactions(call_id)
    add_feed(f"⚔️ {user} countered {original['trader']}'s #{original_id} with {opp.upper()} {original['asset']}")
    try:
        rpc("plugin_submitTx", [{"type": "counter_call", "data": {"original": original_id, "counter": call_id, "user": user}}])
    except:
        pass
    return jsonify({"ok": True, "call_id": call_id, "direction": opp, "asset": original["asset"], "rr": rr})

# -------------------- QUERY ROUTES (all exempt from rate limiting) --------------------
@limiter.exempt
@app.route("/api/calls")
def api_calls():
    following_user = request.args.get("following")
    data = list(calls.values())
    if following_user and following_user in follows:
        fl = follows[following_user]
        data = [c for c in data if c["trader"] in fl]
    return jsonify(sorted(data, key=lambda c: c["ts"], reverse=True))

@limiter.exempt
@app.route("/api/hot_calls")
def api_hot_calls():
    hot = sorted(calls.values(), key=lambda c: c.get("endorsements", 0), reverse=True)[:5]
    return jsonify(hot)

@limiter.exempt
@app.route("/api/comments/<call_id>")
def api_comments(call_id):
    return jsonify(comments.get(call_id, []))

@limiter.exempt
@app.route("/api/reactions/<call_id>")
def api_reactions(call_id):
    init_reactions(call_id)
    totals = {e: reactions[call_id][e] for e in VALID_REACTIONS}
    user_reaction = reactions[call_id]["_users"].get(session.get('user'), "")
    return jsonify({"reactions": totals, "user_reaction": user_reaction})

@limiter.exempt
@app.route("/api/leaderboard")
def api_leaderboard():
    board = sorted(users.items(), key=lambda x: x[1]["rep"], reverse=True)
    result = []
    for u, data in board[:20]:
        wins = data["wins"]
        losses = data["losses"]
        total = wins + losses
        win_rate = round(wins / total * 100, 1) if total > 0 else 0
        rep = data["rep"]
        badge = ""
        if rep >= 200:
            badge = "🥇"
        elif rep >= 100:
            badge = "🥈"
        elif rep >= 50:
            badge = "🥉"
        result.append({
            "username": u,
            "wins": wins,
            "losses": losses,
            "rep": rep,
            "streak": data.get("streak", 0),
            "win_rate": win_rate,
            "badge": badge
        })
    return jsonify(result)

@limiter.exempt
@app.route("/api/trader/<username>")
def api_trader(username):
    if username not in users:
        return jsonify({"error": "Not found"}), 404
    u = users[username]
    trader_calls = [c for c in calls.values() if c["trader"] == username]
    trader_calls.sort(key=lambda c: c["ts"], reverse=True)
    return jsonify({
        "user": {"username": username, "wins": u["wins"], "losses": u["losses"], "rep": u["rep"], "wallet": u.get("wallet", ""), "streak": u.get("streak", 0), "tips_received": u.get("tips_received", 0)},
        "calls": trader_calls,
        "following": follows.get(username, [])
    })

@limiter.exempt
@app.route("/api/feed")
def api_feed():
    return jsonify(feed[:40])

@limiter.exempt
@app.route("/api/tips")
def api_tips():
    return jsonify(tips_log[-20:][::-1])

@limiter.exempt
@app.route("/api/prices")
def api_prices():
    return jsonify(all_prices())

@limiter.exempt
@app.route("/api/pending_rewards")
def api_pending_rewards():
    user = session.get("user")
    if not user or not is_admin(user):
        return jsonify([])
    return jsonify(pending_rewards)

@limiter.limit("10 per minute")
@app.route("/tx/mark_reward_sent", methods=["POST"])
def mark_reward_sent():
    user = session.get("user")
    if not user or not is_admin(user):
        return jsonify({"error": "Admin only"}), 403
    d = request.json or {}
    index = d.get("index")
    if index is None or index < 0 or index >= len(pending_rewards):
        return jsonify({"error": "Invalid index"}), 400
    if pending_rewards[index]["status"] != "pending":
        return jsonify({"error": "Already processed"}), 400
    pending_rewards[index]["status"] = "sent"
    add_feed(f"✅ Reward for {pending_rewards[index]['user']} marked as sent")
    return jsonify({"ok": True})

# -------------------- FRONTEND (Full UI) --------------------
# (The UI is unchanged – same as before)
UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AlphaCred</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    :root{--bg:#080808;--bg1:#111111;--bg2:#171717;--border:#222222;--text:#e8e8e8;--muted:#555555;--long:#00e87c;--short:#ff4040;--accent:#c8ff00;--orange:#ff9500;--mono:'Space Mono',monospace;--sans:'Inter',sans-serif}
    body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;line-height:1.5;min-height:100vh}
    .ticker{background:var(--bg1);border-bottom:1px solid var(--border);padding:6px 0;overflow:hidden;white-space:nowrap;font-family:var(--mono);font-size:12px;color:var(--muted)}
    .ticker-inner{display:inline-block;padding-left:100%;animation:ticker-scroll 30s linear infinite}
    .ticker-inner span{margin:0 20px}.price{color:var(--text)}.na{color:var(--short)}
    @keyframes ticker-scroll{0%{transform:translateX(0)}100%{transform:translateX(-100%)}}
    header{display:flex;align-items:center;justify-content:space-between;padding:12px 28px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:100}
    .logo{font-family:var(--mono);font-size:17px;font-weight:700;letter-spacing:.12em;color:var(--accent);display:flex;align-items:center;gap:8px}
    .logo::before{content:'*';color:var(--muted);animation:blink 1.4s step-end infinite}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
    .hstats{display:flex;gap:24px;font-family:var(--mono);font-size:11px;color:var(--muted)}
    .hstats span b{color:var(--text)}
    .user-menu{display:flex;align-items:center;gap:12px}
    .uname{font-family:var(--mono);font-size:12px;color:var(--accent)}
    .logout-btn{background:var(--bg2);border:1px solid var(--border);color:var(--muted);padding:4px 12px;border-radius:3px;cursor:pointer;font-family:var(--mono);font-size:10px}
    .logout-btn:hover{border-color:var(--short);color:var(--short)}
    .grid{display:grid;grid-template-columns:1fr 320px;min-height:calc(100vh - 57px)}
    .feed-col{border-right:1px solid var(--border);padding:20px 24px}
    .sidebar{display:flex;flex-direction:column}
    .sidebar-section{padding:16px 20px;border-bottom:1px solid var(--border)}
    .sec-label{font-family:var(--mono);font-size:10px;letter-spacing:.15em;color:var(--muted);text-transform:uppercase;margin-bottom:12px}
    .call-card{background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px;margin-bottom:8px;transition:border-color .15s}
    .call-card:hover{border-color:#333}
    .call-card.is-counter{border-left:2px solid var(--short)}
    .call-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
    .call-asset{font-family:var(--mono);font-size:14px;font-weight:700}
    .dir-badge{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.1em;padding:2px 8px;border-radius:2px}
    .dir-long{background:rgba(0,232,124,.12);color:var(--long);border:1px solid rgba(0,232,124,.25)}
    .dir-short{background:rgba(255,64,64,.12);color:var(--short);border:1px solid rgba(255,64,64,.25)}
    .call-levels{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px}
    .level{text-align:center}
    .level-label{font-size:10px;color:var(--muted);letter-spacing:.06em}
    .level-val{font-family:var(--mono);font-size:13px;font-weight:700}
    .tp-val{color:var(--long)}.sl-val{color:var(--short)}.ent-val{color:var(--text)}
    .call-meta{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--muted)}
    .call-trader{font-family:var(--mono);font-size:11px;display:flex;align-items:center;gap:6px}
    .call-trader .avatar{display:inline-block;width:24px;height:24px;border-radius:50%;text-align:center;line-height:24px;font-size:12px;font-weight:bold;color:#fff}
    .rr-badge{font-family:var(--mono);font-size:11px;color:var(--accent)}
    .status-pill{font-size:10px;font-family:var(--mono);padding:1px 6px;border-radius:2px;letter-spacing:.05em}
    .status-open{background:rgba(200,255,0,.08);color:var(--accent)}
    .status-tp_hit{background:rgba(0,232,124,.12);color:var(--long)}
    .status-sl_hit{background:rgba(255,64,64,.12);color:var(--short)}
    .status-expired{background:rgba(85,85,85,.2);color:var(--muted)}
    .action-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
    .btn-sm{font-family:var(--mono);font-size:10px;padding:4px 10px;border-radius:2px;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--muted);transition:all .15s;white-space:nowrap}
    .btn-sm:hover{color:var(--text);border-color:#444}
    .btn-sm.endorse:hover{border-color:var(--accent);color:var(--accent)}
    .btn-sm.resolve:hover{border-color:var(--long);color:var(--long)}
    .btn-sm.counter:hover{border-color:var(--short);color:var(--short)}
    .claim-btn{font-family:var(--mono);font-size:10px;padding:4px 10px;border-radius:2px;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--orange);border-color:rgba(255,149,0,.3)}
    .claim-btn:hover{background:rgba(255,149,0,.1)}
    .endorse-count{font-family:var(--mono);font-size:11px;color:var(--muted);align-self:center;margin-left:auto}
    .react-row{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
    .react-btn{display:flex;align-items:center;gap:4px;font-family:var(--mono);font-size:11px;padding:3px 8px;border-radius:12px;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--muted);transition:all .15s}
    .react-btn:hover{background:var(--bg2);border-color:#444}
    .react-btn.active{background:rgba(200,255,0,.08);border-color:var(--accent);color:var(--accent)}
    .react-emoji{font-size:14px}
    .react-count{font-size:11px}
    .comment-section{margin-top:10px;padding-top:10px;border-top:1px solid var(--border);max-height:200px;overflow-y:auto}
    .comment-item{padding:3px 0;font-size:12px}
    .c-user{color:var(--accent);font-family:var(--mono);font-size:11px}
    .c-text{color:var(--text)}
    .c-time{font-size:10px;color:var(--muted)}
    .comment-form{display:flex;gap:6px;margin-top:6px}
    .comment-form input{flex:1;background:var(--bg2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-family:var(--mono);font-size:11px;padding:4px 8px;outline:none}
    .comment-form input:focus{border-color:var(--accent)}
    .comment-form button{background:var(--bg2);border:1px solid var(--border);color:var(--muted);padding:4px 10px;border-radius:3px;cursor:pointer;font-family:var(--mono);font-size:11px}
    .comment-form button:hover{border-color:var(--accent);color:var(--accent)}
    .counter-badge{font-family:var(--mono);font-size:10px;color:var(--short);margin-top:4px}
    .lb-row{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border)}
    .lb-rank{font-family:var(--mono);font-size:10px;color:var(--muted);width:18px}
    .lb-name{font-family:var(--mono);font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center;gap:6px}
    .lb-name .avatar{display:inline-block;width:24px;height:24px;border-radius:50%;text-align:center;line-height:24px;font-size:12px;font-weight:bold;color:#fff}
    .lb-rep{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--accent);min-width:36px;text-align:right}
    .lb-rec{font-family:var(--mono);font-size:10px;color:var(--muted)}
    .streak-badge{font-family:var(--mono);font-size:10px;color:var(--orange)}
    .tip-btn{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:2px;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--muted);transition:all .15s}
    .tip-btn:hover{border-color:var(--orange);color:var(--orange)}
    .badge{font-size:14px;margin-left:4px}
    .winrate{font-size:10px;color:var(--long);font-family:var(--mono)}
    .form-row{margin-bottom:10px}
    .form-row label{display:block;font-size:10px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:4px;font-family:var(--mono)}
    .form-row input,.form-row select,.form-row textarea{width:100%;background:var(--bg2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-family:var(--mono);font-size:12px;padding:7px 10px;outline:none;transition:border-color .15s}
    .form-row textarea{min-height:40px;resize:vertical}
    .form-row input:focus,.form-row select:focus,.form-row textarea:focus{border-color:var(--accent)}
    .form-grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
    .submit-btn{width:100%;background:var(--accent);color:#000;border:none;border-radius:3px;font-family:var(--mono);font-size:12px;font-weight:700;letter-spacing:.08em;padding:10px;cursor:pointer;margin-top:8px;transition:opacity .15s}
    .submit-btn:hover{opacity:.85}
    .btn-secondary{background:var(--bg2)!important;color:var(--text)!important;border:1px solid var(--border)!important}
    .btn-secondary:hover{background:var(--border)!important}
    .msg{font-family:var(--mono);font-size:11px;padding:6px 10px;border-radius:3px;margin-top:8px;display:none}
    .msg.ok{background:rgba(0,232,124,.1);color:var(--long);border:1px solid rgba(0,232,124,.2);display:block}
    .msg.err{background:rgba(255,64,64,.1);color:var(--short);border:1px solid rgba(255,64,64,.2);display:block}
    .tab-bar{display:flex;border-bottom:1px solid var(--border);margin-bottom:16px;flex-wrap:wrap}
    .tab{font-family:var(--mono);font-size:11px;letter-spacing:.06em;color:var(--muted);padding:8px 14px;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;background:none;border-top:none;border-left:none;border-right:none}
    .tab.active{color:var(--accent);border-bottom-color:var(--accent)}
    .tab:hover:not(.active){color:var(--text)}
    .pane{display:none}.pane.active{display:block}
    .feed-item{padding:6px 0;border-bottom:1px solid var(--border);font-size:12px;color:var(--muted)}
    .feed-item:last-child{border-bottom:none}
    .tip-modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg1);border:1px solid var(--border);border-radius:6px;padding:24px;z-index:200;min-width:260px;display:none}
    .tip-modal.open{display:block}
    .tip-modal h3{font-family:var(--mono);font-size:13px;color:var(--accent);margin-bottom:16px}
    .modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:199;display:none}
    .modal-overlay.open{display:block}
    .authOverlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.92);z-index:9999;display:flex;align-items:center;justify-content:center}
    .authBox{background:var(--bg1);padding:40px;border-radius:6px;max-width:400px;width:90%;border:1px solid var(--border)}
    #authBox h1{font-family:var(--mono);font-size:18px;color:var(--accent);margin-bottom:24px;text-align:center;letter-spacing:.12em}
    #authResult{margin-top:10px;text-align:center;font-family:var(--mono);font-size:12px}
    .profile-card{background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:20px}
    .profile-avatar{width:64px;height:64px;border-radius:50%;object-fit:cover;border:2px solid var(--border);background:var(--bg2);display:inline-flex;align-items:center;justify-content:center;font-size:28px}
    .profile-stats{display:flex;gap:20px;margin:12px 0;flex-wrap:wrap}
    .profile-stat .num{font-family:var(--mono);font-size:16px;font-weight:700;color:var(--accent)}
    .profile-stat .lbl{font-size:10px;color:var(--muted);text-transform:uppercase}
    .tag{background:var(--bg2);padding:2px 8px;border-radius:12px;font-size:11px;font-family:var(--mono);color:var(--muted);border:1px solid var(--border)}
    .pending-item{background:var(--bg2);padding:8px;border-radius:4px;margin-bottom:6px;border-left:2px solid var(--orange);font-size:12px}
    .pending-item .action-btn{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:2px;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--muted);transition:all .15s}
    .pending-item .action-btn:hover{border-color:var(--orange);color:var(--orange)}
    #pendingSection{display:none}
    #pendingSection.visible{display:block}
    .trending-assets{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}
    .asset-pill{background:var(--bg2);padding:4px 12px;border-radius:12px;font-family:var(--mono);font-size:11px;border:1px solid var(--border)}
    .asset-pill .price{color:var(--text)}
    ::-webkit-scrollbar{width:4px}
    ::-webkit-scrollbar-track{background:var(--bg)}
    ::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
    .hidden{display:none}
  </style>
</head>
<body>
<!-- Auth -->
<div class="authOverlay" id="authOverlay">
  <div class="authBox" id="authBox">
    <h1>▸ ALPHACRED</h1>
    <div class="form-row"><label>Username</label><input id="authUsername" placeholder="Choose a username"></div>
    <div class="form-row"><label>Password</label><input id="authPassword" type="password" placeholder="Password"></div>
    <div class="form-row"><label>Canopy Wallet Address <span style="color:var(--muted);font-size:11px;font-weight:normal;">(40 hex chars, no 0x)</span></label><input id="authWallet" placeholder="e.g. 0ca311d15834d02848765ba784"></div>
    <button class="submit-btn" onclick="doAuth('login')">LOGIN</button>
    <button class="submit-btn btn-secondary" onclick="doAuth('signup')">SIGN UP</button>
    <div id="authResult"></div>
  </div>
</div>
<!-- Tip modal -->
<div class="modal-overlay" id="tipOverlay" onclick="closeTip()"></div>
<div class="tip-modal" id="tipModal">
  <h3>💸 TIP TRADER</h3>
  <div id="tipTarget" style="font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:12px"></div>
  <div class="form-row"><label>Amount (ACRED)</label><input id="tipAmount" type="number" min="1" value="5"></div>
  <button class="submit-btn" onclick="submitTip()">SEND TIP</button>
  <div id="tipMsg" class="msg"></div>
</div>
<!-- Ticker -->
<div class="ticker"><div class="ticker-inner" id="tickerInner">Loading prices...</div></div>
<!-- Header -->
<header>
  <div class="logo">ALPHACRED</div>
  <div class="hstats">
    <span>CALLS: <b id="hd-calls">0</b></span>
    <span>TRADERS: <b id="hd-traders">0</b></span>
    <span style="color:var(--long);font-family:var(--mono);font-size:11px">RPC:50002</span>
  </div>
  <div class="user-menu hidden" id="userMenu">
    <span class="uname" id="navUsername"></span>
    <button class="logout-btn" onclick="doLogout()">LOGOUT</button>
  </div>
</header>
<div class="grid">
  <div class="feed-col">
    <div class="tab-bar">
      <button class="tab active" onclick="switchTab('all',this)">ALL</button>
      <button class="tab" onclick="switchTab('open',this)">OPEN</button>
      <button class="tab" onclick="switchTab('following',this)">FOLLOWING</button>
      <button class="tab" onclick="switchTab('resolved',this)">RESOLVED</button>
      <button class="tab" onclick="switchTab('hot',this)">🔥 HOT</button>
      <button class="tab" onclick="switchTab('activity',this)">ACTIVITY</button>
      <button class="tab" onclick="switchTab('profile',this)">👤 PROFILE</button>
    </div>
    <div id="feed-all" class="pane active"></div>
    <div id="feed-open" class="pane"></div>
    <div id="feed-following" class="pane"></div>
    <div id="feed-resolved" class="pane"></div>
    <div id="feed-hot" class="pane"></div>
    <div id="feed-activity" class="pane"></div>
    <div id="feed-profile" class="pane"></div>
  </div>
  <div class="sidebar">
    <div class="sidebar-section">
      <div class="sec-label">Trending Assets</div>
      <div class="trending-assets" id="trendingAssets">Loading...</div>
    </div>
    <div class="sidebar-section">
      <div class="sec-label">Publish Call</div>
      <div class="form-grid2">
        <div class="form-row"><label>Asset</label><select id="f-asset"><option>BTC</option><option>ETH</option><option>SOL</option><option>BNB</option><option>GOLD</option><option>SILVER</option><option>WTI</option></select></div>
        <div class="form-row"><label>Direction</label><select id="f-dir"><option value="long">LONG ▲</option><option value="short">SHORT ▼</option></select></div>
      </div>
      <div class="form-grid2">
        <div class="form-row"><label>Entry</label><input id="f-entry" type="number" placeholder="0.00"></div>
        <div class="form-row"><label>Expiry (hrs)</label><input id="f-expiry" type="number" value="24"></div>
      </div>
      <div class="form-grid2">
        <div class="form-row"><label>Take Profit</label><input id="f-tp" type="number" placeholder="0.00"></div>
        <div class="form-row"><label>Stop Loss</label><input id="f-sl" type="number" placeholder="0.00"></div>
      </div>
      <button class="submit-btn" onclick="publishCall()">PUBLISH CALL</button>
      <div id="pub-msg" class="msg"></div>
    </div>
    <div class="sidebar-section">
      <div class="form-row"><label>Canopy Wallet Address <span style="color:var(--muted);font-size:11px;">(40 hex, no 0x)</span></label><input id="wallet-address" placeholder="0ca311d15834d02848765ba784"></div>
      <button class="submit-btn" onclick="setWallet()">SET WALLET</button>
      <div id="wallet-msg" class="msg"></div>
      <div style="margin-top:12px">
        <div class="form-row"><label>Follow Trader</label><input id="follow-target" placeholder="username"></div>
        <button class="submit-btn btn-secondary" onclick="followTrader()">FOLLOW / UNFOLLOW</button>
        <div id="follow-msg" class="msg"></div>
      </div>
    </div>
    <div class="sidebar-section" id="pendingSection">
      <div class="sec-label">⏳ Pending Rewards</div>
      <div id="pendingList">Loading...</div>
    </div>
    <div class="sidebar-section" style="flex:1;overflow-y:auto">
      <div class="sec-label">Rep Leaderboard</div>
      <div id="leaderboard"></div>
    </div>
  </div>
</div>
<script>
let currentUser = '';
const EMOJI_SLUGS = {"🔥":"fire","🎯":"target","📉":"down","💀":"skull"};
let tipTargetUser = '';
let pendingInterval;

// Avatar generation (simple)
function getAvatar(username) {
  const colors = ['#ff6b6b','#feca57','#48dbfb','#1dd1a1','#5f27cd','#ff9ff3','#ff6348','#00d2d3'];
  const h = username.split('').reduce((a,b) => a + b.charCodeAt(0), 0);
  const color = colors[h % colors.length];
  const initial = username.charAt(0).toUpperCase();
  return `<span class="avatar" style="background:${color}">${initial}</span>`;
}

async function checkSession() {
  const r = await fetch('/api/me');
  const d = await r.json();
  if (d.username) {
    currentUser = d.username;
    document.getElementById('authOverlay').style.display = 'none';
    document.getElementById('navUsername').textContent = currentUser;
    document.getElementById('userMenu').classList.remove('hidden');
    refreshAll();
    loadTrendingAssets();
    if (d.admin) {
      document.getElementById('pendingSection').classList.add('visible');
      loadPendingRewards();
      pendingInterval = setInterval(loadPendingRewards, 5000);
    } else {
      document.getElementById('pendingSection').classList.remove('visible');
    }
  } else {
    document.getElementById('authOverlay').style.display = 'flex';
  }
}

function doAuth(action) {
  const username = document.getElementById('authUsername').value.trim();
  const password = document.getElementById('authPassword').value.trim();
  const wallet = document.getElementById('authWallet').value.trim();
  const res = document.getElementById('authResult');
  if (!username || !password) {
    res.style.color='var(--short)'; res.textContent='❌ Fill username and password.'; return;
  }
  fetch('/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,username,password,wallet})})
    .then(r=>r.json()).then(d=>{
      if (d.status==='success') {
        res.style.color='var(--long)'; res.textContent='✅ OK';
        window.location.reload();
      } else {
        res.style.color='var(--short)'; res.textContent='❌ '+d.msg;
      }
    }).catch(()=>{ res.style.color='var(--short)'; res.textContent='❌ Network error'; });
}

async function doLogout() {
  await fetch('/logout',{method:'POST'});
  window.location.reload();
}

function updateTicker() {
  fetch('/api/prices').then(r=>r.json()).then(prices=>{
    let html='';
    for (const [a,p] of Object.entries(prices)) {
      html += p!==null ? `<span>${a}: <span class="price">$${Number(p).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</span></span>` : `<span>${a}: <span class="na">N/A</span></span>`;
    }
    document.getElementById('tickerInner').innerHTML = html;
  }).catch(()=>{});
}

async function loadTrendingAssets() {
  const r = await fetch('/api/prices');
  const prices = await r.json();
  const assets = ['BTC','ETH','GOLD','WTI'];
  const container = document.getElementById('trendingAssets');
  container.innerHTML = assets.map(a => {
    const p = prices[a];
    return `<span class="asset-pill">${a}: <span class="price">${p!==null ? '$'+Number(p).toFixed(2) : 'N/A'}</span></span>`;
  }).join('');
}

async function refreshAll() {
  await Promise.all([fetchCalls('all'),fetchCalls('open'),fetchCalls('following'),fetchCalls('resolved'),fetchCalls('hot'),fetchLeaderboard()]);
}

async function fetchCalls(type) {
  let url = '/api/calls';
  if (type==='following' && currentUser) url += '?following='+encodeURIComponent(currentUser);
  else if (type==='hot') url = '/api/hot_calls';
  const r = await fetch(url);
  const data = await r.json();
  if (type==='all') document.getElementById('hd-calls').textContent = data.length;
  renderFeed(type, data);
  return data;
}

async function fetchLeaderboard() {
  const r = await fetch('/api/leaderboard');
  const board = await r.json();
  document.getElementById('hd-traders').textContent = board.length;
  const el = document.getElementById('leaderboard');
  if (!board.length) { el.innerHTML='<div style="color:var(--muted);font-family:var(--mono);font-size:11px">No traders yet.</div>'; return; }
  const medals = ['🥇','🥈','🥉'];
  el.innerHTML = board.map((u,i)=>{
    const streak = u.streak >= 2 ? `<span class="streak-badge">🔥${u.streak}</span>` : '';
    const badge = u.badge ? `<span class="badge">${u.badge}</span>` : '';
    const winrate = u.win_rate ? `<span class="winrate">W: ${u.win_rate}%</span>` : '';
    return `<div class="lb-row">
      <span class="lb-rank">${medals[i]||i+1}</span>
      <span class="lb-name">${getAvatar(u.username)} ${u.username} ${badge}</span>
      ${streak}
      <span class="lb-rec" style="color:var(--long)">${u.wins}W</span>
      <span class="lb-rec" style="color:var(--short)">${u.losses}L</span>
      ${winrate}
      <span class="lb-rep">${u.rep}</span>
      <button class="tip-btn" onclick="showTip('${u.username}')">💸</button>
    </div>`;
  }).join('');
}

function callCard(c) {
  const open = c.status === 'open';
  const resolvedDate = c.resolved_at ? new Date(c.resolved_at*1000).toLocaleString('en-US',{month:'short',day:'numeric'}) : '';
  const counterBadge = c.counter_to ? `<div class="counter-badge">⚔️ counters #${c.counter_to}</div>` : '';
  const claimBtn = (!open && c.status === 'tp_hit' && !c.reward_queued) ? `<button class="claim-btn" onclick="claimReward('${c.call_id}')">💰 Claim Reward</button>` : '';
  return `<div class="call-card${c.counter_to?' is-counter':''}" id="call-${c.call_id}">
    <div class="call-top">
      <span class="call-asset">${c.asset}</span>
      <span class="dir-badge dir-${c.direction}">${c.direction.toUpperCase()}</span>
    </div>
    <div class="call-levels">
      <div class="level"><div class="level-label">TP</div><div class="level-val tp-val">${Number(c.tp).toLocaleString()}</div></div>
      <div class="level"><div class="level-label">ENTRY</div><div class="level-val ent-val">${Number(c.entry).toLocaleString()}</div></div>
      <div class="level"><div class="level-label">SL</div><div class="level-val sl-val">${Number(c.sl).toLocaleString()}</div></div>
    </div>
    <div class="call-meta">
      <span class="call-trader">${getAvatar(c.trader)} ${c.trader}</span>
      <span class="rr-badge">R:R ${c.rr}x</span>
      <span class="status-pill status-${c.status}">${c.status.replace('_',' ').toUpperCase()}</span>
    </div>
    ${counterBadge}
    ${open ? `
    <div class="action-row">
      <button class="btn-sm endorse" onclick="endorse('${c.call_id}',this)">⬆ ENDORSE</button>
      <button class="btn-sm resolve" onclick="resolveCall('${c.call_id}')">⚡ RESOLVE</button>
      <button class="btn-sm counter" onclick="counterCall('${c.call_id}')">⚔️ COUNTER</button>
      <span class="endorse-count" id="end-${c.call_id}">${c.endorsements}</span>
    </div>
    ` : `<div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:8px">Closed ${resolvedDate}${c.price_at_resolve ? ` — Price: $${Number(c.price_at_resolve).toLocaleString()}` : ''}</div>`}
    ${claimBtn}
    <div class="react-row" id="reacts-${c.call_id}">
      ${['🔥','🎯','📉','💀'].map(e => `<button class="react-btn" id="rb-${c.call_id}-${EMOJI_SLUGS[e]}" onclick="doReact('${c.call_id}','${e}')"><span class="react-emoji">${e}</span><span class="react-count" id="rc-${c.call_id}-${EMOJI_SLUGS[e]}">0</span></button>`).join('')}
    </div>
    <div class="comment-section" id="comments-${c.call_id}">
      <div class="comment-list" id="cl-${c.call_id}"></div>
      ${open ? `<div class="comment-form"><input id="ci-${c.call_id}" placeholder="Add comment..." onkeydown="if(event.key==='Enter')addComment('${c.call_id}')"><button onclick="addComment('${c.call_id}')">💬</button></div>` : ''}
    </div>
  </div>`;
}

function renderFeed(type, data) {
  const el = document.getElementById('feed-'+type);
  if (!el) return;
  if (!data.length) { el.innerHTML='<div style="color:var(--muted);font-family:var(--mono);font-size:12px;padding:20px 0">No calls yet.</div>'; return; }
  el.innerHTML = data.map(callCard).join('');
  data.forEach(c => {
    loadReactions(c.call_id);
    loadComments(c.call_id);
  });
}

function loadReactions(callId) {
  fetch('/api/reactions/'+callId).then(r=>r.json()).then(d=>{
    const ur = d.user_reaction;
    for (const [e, slug] of Object.entries(EMOJI_SLUGS)) {
      const cnt = document.getElementById(`rc-${callId}-${slug}`);
      const btn = document.getElementById(`rb-${callId}-${slug}`);
      if (cnt) cnt.textContent = d.reactions[e] || 0;
      if (btn) btn.className = `react-btn${ur===e?' active':''}`;
    }
  }).catch(()=>{});
}

function loadComments(callId) {
  fetch('/api/comments/'+callId).then(r=>r.json()).then(comments=>{
    const el = document.getElementById('cl-'+callId);
    if (!el) return;
    el.innerHTML = comments.map(c=>`<div class="comment-item"><span class="c-user">${c.username}</span><span class="c-text">: ${c.text}</span></div>`).join('');
  }).catch(()=>{});
}

async function loadPendingRewards() {
  if (!currentUser) return;
  const r = await fetch('/api/pending_rewards');
  const data = await r.json();
  const el = document.getElementById('pendingList');
  if (!data.length) {
    el.innerHTML = '<div style="color:var(--muted);font-family:var(--mono);font-size:11px">No pending rewards.</div>';
    return;
  }
  el.innerHTML = data.map((item, idx) => `
    <div class="pending-item">
      <div><strong>${item.user}</strong> – ${item.amount} ACRED (${item.reason})</div>
      <div style="font-size:11px;color:var(--muted);font-family:var(--mono)">Wallet: ${item.wallet.slice(0,10)}...</div>
      <div style="margin-top:4px;display:flex;gap:6px;flex-wrap:wrap">
        <a href="https://testnet.app.canopynetwork.org/wallet" target="_blank" class="action-btn">📤 Launchpad</a>
        <button class="action-btn" onclick="markSent(${idx})">✅ Mark Sent</button>
      </div>
    </div>
  `).join('');
}

async function markSent(index) {
  if (!confirm('Mark this reward as sent?')) return;
  const r = await fetch('/tx/mark_reward_sent', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({index})
  });
  const d = await r.json();
  if (d.ok) loadPendingRewards();
}

async function claimReward(callId) {
  if (!currentUser) { alert('Login first.'); return; }
  const r = await fetch('/tx/claim_reward', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({call_id:callId})
  });
  const d = await r.json();
  if (d.error) { alert('❌ '+d.error); return; }
  alert('✅ Reward queued for manual payout! Check "Pending Rewards" section.');
  await refreshAll();
}

async function fetchActivity() {
  const r = await fetch('/api/feed');
  const items = await r.json();
  const el = document.getElementById('feed-activity');
  if (!el) return;
  el.innerHTML = items.length ? items.map(i=>`<div class="feed-item">${i}</div>`).join('') : '<div style="color:var(--muted);padding:20px;font-family:var(--mono);font-size:12px">No activity yet.</div>';
}

function switchTab(tab, btn) {
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('feed-'+tab).classList.add('active');
  if (tab==='activity') fetchActivity();
  else if (tab==='profile') loadProfile();
  else if (tab==='following') fetchCalls('following');
  else if (tab==='hot') fetchCalls('hot');
  else fetchCalls(tab);
}

async function publishCall() {
  if (!currentUser) { alert('Please login first.'); return; }
  const msg = document.getElementById('pub-msg');
  const body = {
    asset: document.getElementById('f-asset').value,
    direction: document.getElementById('f-dir').value,
    entry: parseFloat(document.getElementById('f-entry').value),
    tp: parseFloat(document.getElementById('f-tp').value),
    sl: parseFloat(document.getElementById('f-sl').value),
    expiry: Math.floor(Date.now()/1000) + (parseInt(document.getElementById('f-expiry').value)||24)*3600
  };
  try {
    const r = await fetch('/tx/publish_call', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d = await r.json();
    if (d.error) { msg.className='msg err'; msg.textContent=d.error; return; }
    msg.className='msg ok'; msg.textContent=`✅ #${d.call_id} published · R:R ${d.rr}x`;
    await refreshAll();
  } catch(e) { msg.className='msg err'; msg.textContent='Network error'; }
}

async function endorse(callId, btn) {
  if (!currentUser) { alert('Login first.'); return; }
  const r = await fetch('/tx/endorse_call', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({call_id:callId})});
  const d = await r.json();
  if (d.error) { alert('❌ '+d.error); return; }
  document.getElementById('end-'+callId).textContent = d.endorsements;
  btn.textContent='ENDORSED';
  btn.disabled=true;
}

async function resolveCall(callId) {
  if (!currentUser) { alert('Login first.'); return; }
  const r = await fetch('/tx/resolve_call', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({call_id:callId})});
  const d = await r.json();
  if (d.error) { alert('❌ '+d.error); return; }
  alert(`✅ ${d.result.replace('_',' ').toUpperCase()} at $${Number(d.price).toLocaleString()}`);
  await refreshAll();
}

async function counterCall(callId) {
  if (!currentUser) { alert('Login first.'); return; }
  if (!confirm('Counter this call with opposite direction?')) return;
  const r = await fetch('/tx/counter_call', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({original_call_id:callId})});
  const d = await r.json();
  if (d.error) { alert('❌ '+d.error); return; }
  alert(`✅ Counter-call #${d.call_id} created — ${d.direction.toUpperCase()} ${d.asset} · R:R ${d.rr}x`);
  await refreshAll();
}

async function doReact(callId, emoji) {
  if (!currentUser) { alert('Login first.'); return; }
  const r = await fetch('/tx/react', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({call_id:callId,emoji})});
  const d = await r.json();
  if (d.ok) loadReactions(callId);
}

async function addComment(callId) {
  if (!currentUser) { alert('Login first.'); return; }
  const input = document.getElementById('ci-'+callId);
  const text = input.value.trim(); if (!text) return;
  const r = await fetch('/tx/add_comment', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({call_id:callId,text})});
  const d = await r.json();
  if (d.ok) { input.value=''; loadComments(callId); }
  else alert('❌ '+(d.error||'Failed'));
}

function showTip(username) {
  if (!currentUser) { alert('Login first.'); return; }
  if (username===currentUser) { alert('Cannot tip yourself.'); return; }
  tipTargetUser = username;
  document.getElementById('tipTarget').textContent = '→ '+username;
  document.getElementById('tipMsg').className='msg';
  document.getElementById('tipModal').classList.add('open');
  document.getElementById('tipOverlay').classList.add('open');
}

function closeTip() {
  document.getElementById('tipModal').classList.remove('open');
  document.getElementById('tipOverlay').classList.remove('open');
  tipTargetUser='';
}

async function submitTip() {
  const msg = document.getElementById('tipMsg');
  const amount = parseFloat(document.getElementById('tipAmount').value);
  if (!amount || amount<=0) { msg.className='msg err'; msg.textContent='Enter a valid amount'; return; }
  const r = await fetch('/tx/tip', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:tipTargetUser,amount})});
  const d = await r.json();
  if (d.error) { msg.className='msg err'; msg.textContent='❌ '+d.error; return; }
  msg.className='msg ok'; msg.textContent=`✅ Tipped ${amount} ACRED · tx: ${d.tx_hash.slice(0,14)}...`;
  setTimeout(()=>{ closeTip(); fetchLeaderboard(); }, 1500);
}

async function followTrader() {
  if (!currentUser) { alert('Login first.'); return; }
  const target = document.getElementById('follow-target').value.trim();
  if (!target) { document.getElementById('follow-msg').className='msg err'; document.getElementById('follow-msg').textContent='Enter a username.'; return; }
  const r = await fetch('/tx/follow', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target})});
  const d = await r.json();
  const msg = document.getElementById('follow-msg');
  if (d.error) { msg.className='msg err'; msg.textContent=d.error; return; }
  msg.className='msg ok'; msg.textContent=`✅ ${d.action} ${target}`;
}

async function setWallet() {
  if (!currentUser) { alert('Login first.'); return; }
  const address = document.getElementById('wallet-address').value.trim();
  if (!address) { document.getElementById('wallet-msg').className='msg err'; document.getElementById('wallet-msg').textContent='Address required'; return; }
  const r = await fetch('/tx/set_wallet', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address})});
  const d = await r.json();
  const msg = document.getElementById('wallet-msg');
  if (d.error) { msg.className='msg err'; msg.textContent=d.error; return; }
  msg.className='msg ok'; msg.textContent='✅ Wallet updated!';
}

async function loadProfile() {
  if (!currentUser) { document.getElementById('feed-profile').innerHTML='<div style="color:var(--muted);padding:20px">Login to view profile.</div>'; return; }
  const r = await fetch('/api/profile/'+encodeURIComponent(currentUser));
  const d = await r.json();
  if (d.error) return;
  const avatar = d.avatar ? `<img src="${d.avatar}" class="profile-avatar">` : `<div class="profile-avatar">👤</div>`;
  document.getElementById('feed-profile').innerHTML = `
  <div class="profile-card">
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      ${avatar}
      <div>
        <div style="font-family:var(--mono);font-size:15px;font-weight:700">${d.username}</div>
        <div style="color:var(--muted);margin-top:2px">${d.bio||'No bio yet.'}</div>
        ${d.streak>=2 ? `<div style="color:var(--orange);font-family:var(--mono);font-size:12px;margin-top:4px">🔥 ${d.streak} win streak</div>` : ''}
      </div>
    </div>
    <div class="profile-stats">
      <div class="profile-stat"><div class="num">${d.rep}</div><div class="lbl">Rep</div></div>
      <div class="profile-stat"><div class="num" style="color:var(--long)">${d.wins}</div><div class="lbl">Wins</div></div>
      <div class="profile-stat"><div class="num" style="color:var(--short)">${d.losses}</div><div class="lbl">Losses</div></div>
      <div class="profile-stat"><div class="num">${d.followers.length}</div><div class="lbl">Followers</div></div>
      <div class="profile-stat"><div class="num">${d.tips_received}</div><div class="lbl">Tips recv</div></div>
    </div>
    <div style="font-family:var(--mono);font-size:11px;color:var(--muted)">Wallet: ${d.wallet||'Not set'}</div>
    ${d.following.length ? `<div style="margin-top:12px"><div class="sec-label">Following</div><div style="display:flex;flex-wrap:wrap;gap:6px">${d.following.map(f=>`<span class="tag">${f}</span>`).join('')}</div></div>` : ''}
    <div style="margin-top:16px">
      <div class="form-row"><label>Bio</label><textarea id="editBio" rows="2">${d.bio||''}</textarea></div>
      <button class="submit-btn" onclick="saveProfile()">SAVE BIO</button>
      <div id="profileMsg" class="msg"></div>
    </div>
  </div>`;
}

async function saveProfile() {
  const bio = document.getElementById('editBio').value.trim();
  const msg = document.getElementById('profileMsg');
  const r = await fetch('/tx/update_profile', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bio})});
  const d = await r.json();
  if (d.ok) { msg.className='msg ok'; msg.textContent='✅ Profile updated'; setTimeout(loadProfile,800); }
  else { msg.className='msg err'; msg.textContent='❌ '+(d.error||'Failed'); }
}

// Init
checkSession();
updateTicker();
setInterval(updateTicker, 5000);
setInterval(()=>{ if(currentUser) refreshAll(); }, 10000);
</script>
</body>
</html>
"""

# -------------------- ROUTE FOR ROOT --------------------
@app.route("/")
def index():
    return render_template_string(UI)

# -------------------- RPC CONNECTIVITY CHECK --------------------
def check_rpc():
    print("\n🔍 Checking RPC connectivity...")
    result = rpc("status", [])
    if "error" in result and result["error"] == "rpc_unavailable":
        print("❌ RPC unavailable – rewards will be simulated (no real on-chain tx).")
        print("   Check if the node is running on port 50002 and the endpoint is correct.\n")
    else:
        print("✅ RPC connected successfully.\n")
    return result

# -------------------- SEED DEMO DATA --------------------
seed_demo_data()

# -------------------- START --------------------
if __name__ == "__main__":
    print("\n🔷 ALPHACRED running at http://localhost:50004")
    print("🔷 RPC endpoint: http://localhost:50002 (auto-discovery)")
    if TREASURY_WALLET:
        print(f"🔷 Treasury wallet set: {TREASURY_WALLET} (admin)")
    else:
        print("🔷 No treasury wallet set. Admin features disabled.")
    check_rpc()
    print("🔷 9 custom transaction types + Claim Reward queue.")
    print("🔷 Reputation system: +5 for publish, +2 for endorse, +10 for TP hit.")
    print("🔷 Bonus: 10 ACRED when user first reaches 100 rep.")
    print("🔷 Security: Password hashing, rate limiting, session hardening, input sanitization, wallet validation & uniqueness.\n")
    app.run(host="0.0.0.0", port=50004, debug=False)
