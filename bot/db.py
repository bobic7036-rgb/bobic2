import time  # noqa: F401 (доступно як db.time)
import aiosqlite
from bot.config import DB_PATH, DEFAULTS

_db = None


async def init_db():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        base_until INTEGER DEFAULT 0,
        pro_until INTEGER DEFAULT 0,
        deposit REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        symbol TEXT,
        side TEXT,            -- LONG / SHORT
        entry REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        score INTEGER,        -- шанс %
        reason TEXT,
        leverage INTEGER DEFAULT 1,
        margin REAL DEFAULT 0,
        status TEXT DEFAULT 'OPEN',   -- OPEN / TP1 / TP2 / SL
        pnl REAL DEFAULT 0,
        created_at INTEGER,
        closed_at INTEGER
    );
    """)
    await _db.commit()
    # seed defaults
    for k, v in DEFAULTS.items():
        cur = await _db.execute("SELECT 1 FROM settings WHERE key=?", (k,))
        if not await cur.fetchone():
            await _db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, str(v)))
    await _db.commit()


async def get_setting(key: str):
    cur = await _db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else str(DEFAULTS.get(key, ""))


async def set_setting(key: str, value):
    await _db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    await _db.commit()


async def get_int(key: str) -> int:
    try:
        return int(float(await get_setting(key)))
    except (ValueError, TypeError):
        return int(DEFAULTS.get(key, 0))


async def ensure_user(user_id: int, username: str = ""):
    await _db.execute(
        "INSERT OR IGNORE INTO users(user_id, username) VALUES(?,?)", (user_id, username)
    )
    await _db.commit()


async def get_user(user_id: int):
    await ensure_user(user_id)
    cur = await _db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return await cur.fetchone()


async def set_subscription(user_id: int, plan: str, days: int):
    col = "base_until" if plan == "base" else "pro_until"
    cur = await _db.execute(f"SELECT {col} FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    now = int(time.time())
    base = max(row[col] or 0, now)
    await _db.execute(
        f"UPDATE users SET {col}=? WHERE user_id=?", (base + days * 86400, user_id)
    )
    await _db.commit()


async def revoke_subscription(user_id: int, plan: str):
    col = "base_until" if plan == "base" else "pro_until"
    await _db.execute(f"UPDATE users SET {col}=0 WHERE user_id=?", (user_id,))
    await _db.commit()


async def set_deposit(user_id: int, deposit: float):
    await _db.execute("UPDATE users SET deposit=? WHERE user_id=?", (deposit, user_id))
    await _db.commit()


async def active_subscribers(plan: str):
    col = "base_until" if plan == "base" else "pro_until"
    now = int(time.time())
    cur = await _db.execute(
        f"SELECT user_id, username FROM users WHERE {col} > ?", (now,)
    )
    return await cur.fetchall()


async def add_trade(t: dict):
    await _db.execute(
        """INSERT INTO trades(user_id,symbol,side,entry,sl,tp1,tp2,score,reason,
           leverage,margin,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            t["user_id"], t["symbol"], t["side"], t["entry"], t["sl"], t["tp1"],
            t["tp2"], t["score"], t["reason"], t.get("leverage", 1),
            t.get("margin", 0), "OPEN", int(time.time()),
        ),
    )
    await _db.commit()


async def open_trades(user_id: int = None):
    if user_id is None:
        cur = await _db.execute("SELECT * FROM trades WHERE status='OPEN'")
    else:
        cur = await _db.execute(
            "SELECT * FROM trades WHERE status='OPEN' AND user_id=?", (user_id,)
        )
    return await cur.fetchall()


async def recent_trades(user_id: int, limit: int = 10):
    cur = await _db.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return await cur.fetchall()


async def close_trade(trade_id: int, status: str, price: float):
    cur = await _db.execute("SELECT * FROM trades WHERE id=?", (trade_id,))
    t = await cur.fetchone()
    if not t:
        return
    side = 1 if t["side"] == "LONG" else -1
    move = (price - t["entry"]) / t["entry"] * side   # спрямований рух ціни у %
    margin = t["margin"] or 0
    if status == "SL":
        # збиток = маржа * рух_проти * плече, обмежено маржею (ліквідація не врахована)
        loss = min(max(-move, 0.001) * t["leverage"], 1.0)
        pnl = -margin * loss if margin else -abs(move) * 10
    elif status == "TP1":
        # закриваємо половину позиції на TP1
        gain = max(move, 0.0) * t["leverage"]
        pnl = (margin / 2) * gain if margin else gain
    elif status == "TP2":
        # решта позиції закривається на TP2
        gain = max(move, 0.0) * t["leverage"]
        pnl = (margin / 2) * gain if margin else gain
    else:
        pnl = 0
    await _db.execute(
        "UPDATE trades SET status=?, pnl=pnl+?, closed_at=? WHERE id=?",
        (status, pnl, int(time.time()), trade_id),
    )
    await _db.commit()


async def day_pnl(user_id: int):
    """PnL за останні 24 години"""
    since = int(time.time()) - 86400
    cur = await _db.execute(
        "SELECT COALESCE(SUM(pnl),0) s, COUNT(*) c FROM trades "
        "WHERE user_id=? AND closed_at >= ?",
        (user_id, since),
    )
    return await cur.fetchone()


async def total_pnl(user_id: int):
    cur = await _db.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE user_id=?", (user_id,)
    )
    return await cur.fetchone()


async def all_users_with_open():
    cur = await _db.execute(
        "SELECT DISTINCT user_id FROM trades WHERE status IN ('OPEN','TP1')"
    )
    return await cur.fetchall()
