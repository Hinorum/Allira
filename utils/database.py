import sqlite3
import logging
import time
import asyncio
import threading
import base64
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = "allira.db"
_local = threading.local()


def get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_all():
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_count INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                chat_type TEXT,
                speaker TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens_used INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                admin_id INTEGER,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                winner_id INTEGER,
                winner_name TEXT,
                mode TEXT,
                total_rounds INTEGER DEFAULT 0,
                total_players INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tournament_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER,
                user_id INTEGER,
                username TEXT,
                total_score INTEGER DEFAULT 0,
                eliminated_round INTEGER,
                final_position INTEGER,
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                user_id INTEGER,
                chat_id INTEGER,
                last_response_time REAL DEFAULT 0,
                message_count_minute INTEGER DEFAULT 0,
                minute_start REAL DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS marketapp_profit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL,
                profit_ton REAL NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                raw_response TEXT
            );

            CREATE TABLE IF NOT EXISTS marketapp_rent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT UNIQUE,
                category TEXT,
                nft_address TEXT,
                nft_name TEXT,
                collection_address TEXT,
                ts INTEGER NOT NULL,
                src TEXT,
                dst TEXT,
                price_nano TEXT,
                currency TEXT DEFAULT 'GRAM',
                is_extend INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                source TEXT DEFAULT 'marketapp',
                wallet TEXT DEFAULT '',
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS blockchain_sync_state (
                address TEXT PRIMARY KEY,
                last_synced_lt TEXT,
                last_synced_hash TEXT,
                last_synced_utime INTEGER DEFAULT 0,
                last_sync_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_tournaments_chat ON tournaments(chat_id);
            CREATE INDEX IF NOT EXISTS idx_tournament_players_tid ON tournament_players(tournament_id);
            CREATE INDEX IF NOT EXISTS idx_marketapp_profit_period ON marketapp_profit(period, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_rent_events_ts ON marketapp_rent_events(ts);
            CREATE INDEX IF NOT EXISTS idx_rent_events_wallet ON marketapp_rent_events(wallet);
        """)

        cols = [row["name"] for row in conn.execute("PRAGMA table_info(marketapp_rent_events)")]
        if "wallet" not in cols:
            conn.execute("ALTER TABLE marketapp_rent_events ADD COLUMN wallet TEXT DEFAULT ''")
            logger.info("Миграция: добавлена колонка wallet в marketapp_rent_events")

        conn.execute(
            "UPDATE marketapp_rent_events SET wallet = dst WHERE wallet = '' AND dst <> ''"
        )
        conn.execute(
            "UPDATE marketapp_rent_events SET wallet = dst "
            "WHERE wallet <> '' AND wallet <> dst AND dst <> '' AND dst LIKE '0:%'"
        )

    logger.info("База данных инициализирована")


def _sync_init_db():
    init_db()


def _sync_upsert_user(user_id: int, username: str = None, first_name: str = None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_seen, message_count)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                first_name = COALESCE(excluded.first_name, users.first_name),
                last_seen = CURRENT_TIMESTAMP,
                message_count = users.message_count + 1
        """, (user_id, username, first_name))


def _sync_log_message(user_id: int, chat_id: int, chat_type: str, speaker: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO messages (user_id, chat_id, chat_type, speaker)
            VALUES (?, ?, ?, ?)
        """, (user_id, chat_id, chat_type, speaker))
        increment_stat("total_messages")


def _sync_check_rate_limit(user_id: int, chat_id: int, cooldown: float = 3.0, max_per_minute: int = 5) -> bool:
    now = time.time()
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_response_time, message_count_minute, minute_start FROM rate_limits WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        ).fetchone()

        if not row:
            conn.execute(
                "INSERT INTO rate_limits (user_id, chat_id, last_response_time, message_count_minute, minute_start) VALUES (?, ?, ?, 1, ?)",
                (user_id, chat_id, now, now)
            )
            return True

        last_time = row["last_response_time"]
        count = row["message_count_minute"]
        minute_start = row["minute_start"]

        if now - last_time < cooldown:
            return False

        if now - minute_start > 60:
            count = 0
            minute_start = now

        if count >= max_per_minute:
            return False

        conn.execute("""
            UPDATE rate_limits SET last_response_time=?, message_count_minute=?, minute_start=?
            WHERE user_id=? AND chat_id=?
        """, (now, count + 1, minute_start, user_id, chat_id))
        return True


async def upsert_user(user_id: int, username: str = None, first_name: str = None):
    await asyncio.to_thread(_sync_upsert_user, user_id, username, first_name)


async def log_message(user_id: int, chat_id: int, chat_type: str, speaker: str):
    await asyncio.to_thread(_sync_log_message, user_id, chat_id, chat_type, speaker)


async def check_rate_limit(user_id: int, chat_id: int, cooldown: float = 3.0, max_per_minute: int = 5) -> bool:
    return await asyncio.to_thread(_sync_check_rate_limit, user_id, chat_id, cooldown, max_per_minute)


def _sync_save_tournament(chat_id: int, admin_id: int, mode: str, total_rounds: int,
                     total_players: int, winner_id: int = None, winner_name: str = None) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO tournaments (chat_id, admin_id, ended_at, winner_id, winner_name, mode, total_rounds, total_players)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)
        """, (chat_id, admin_id, winner_id, winner_name, mode, total_rounds, total_players))
        return cursor.lastrowid


def _sync_save_tournament_players(tournament_id: int, players: dict):
    with get_db() as conn:
        for user_id, data in players.items():
            conn.execute("""
                INSERT INTO tournament_players (tournament_id, user_id, username, total_score, eliminated_round, final_position)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                tournament_id, user_id, data.get("username", ""),
                data.get("total_score", 0),
                data.get("eliminated_round"),
                data.get("final_position")
            ))


async def save_tournament(chat_id: int, admin_id: int, mode: str, total_rounds: int,
                     total_players: int, winner_id: int = None, winner_name: str = None) -> int:
    return await asyncio.to_thread(_sync_save_tournament, chat_id, admin_id, mode, total_rounds, total_players, winner_id, winner_name)


async def save_tournament_players(tournament_id: int, players: dict):
    await asyncio.to_thread(_sync_save_tournament_players, tournament_id, players)


def _sync_get_tournament_history(limit: int = 10) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT t.*, 
                   (SELECT COUNT(*) FROM tournament_players WHERE tournament_id=t.id) as player_count
            FROM tournaments t
            ORDER BY t.ended_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def _sync_get_tournament_leaderboard(limit: int = 10) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT username, 
                   COUNT(*) as wins,
                   SUM(total_players) as total_participants
            FROM tournaments t
            JOIN tournament_players tp ON tp.tournament_id = t.id
            WHERE tp.user_id = t.winner_id AND t.winner_id IS NOT NULL
            GROUP BY t.winner_id
            ORDER BY wins DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def _sync_get_top_speakers(limit: int = 10) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT speaker, COUNT(*) as count
            FROM messages
            WHERE timestamp > datetime('now', '-7 days')
            GROUP BY speaker
            ORDER BY count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def _sync_get_active_users(days: int = 7, limit: int = 10) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT username, first_name, message_count
            FROM users
            WHERE last_seen > datetime('now', ?)
            ORDER BY message_count DESC
            LIMIT ?
        """, (f"-{days} days", limit)).fetchall()
        return [dict(r) for r in rows]


async def get_tournament_history(limit: int = 10) -> list:
    return await asyncio.to_thread(_sync_get_tournament_history, limit)


async def get_tournament_leaderboard(limit: int = 10) -> list:
    return await asyncio.to_thread(_sync_get_tournament_leaderboard, limit)


async def get_top_speakers(limit: int = 10) -> list:
    return await asyncio.to_thread(_sync_get_top_speakers, limit)


async def get_active_users(days: int = 7, limit: int = 10) -> list:
    return await asyncio.to_thread(_sync_get_active_users, days, limit)


def _sync_increment_stat(key: str, amount: int = 1):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO bot_stats (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = bot_stats.value + ?,
                updated_at = CURRENT_TIMESTAMP
        """, (key, amount, amount))


def increment_stat(key: str, amount: int = 1):
    _sync_increment_stat(key, amount)


def _sync_get_stat(key: str) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM bot_stats WHERE key=?", (key,)).fetchone()
        return row["value"] if row else 0


def get_stat(key: str) -> int:
    return _sync_get_stat(key)


def _sync_get_total_users() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"] if row else 0


def get_total_users() -> int:
    return _sync_get_total_users()


def _sync_get_total_tournaments() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM tournaments").fetchone()
        return row["cnt"] if row else 0


def get_total_tournaments() -> int:
    return _sync_get_total_tournaments()


def _sync_get_messages_today() -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE timestamp > datetime('now', 'start of day')"
        ).fetchone()
        return row["cnt"] if row else 0


def get_messages_today() -> int:
    return _sync_get_messages_today()


def _sync_is_user_banned(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row["is_banned"]) if row else False


def is_user_banned(user_id: int) -> bool:
    return _sync_is_user_banned(user_id)


def _sync_save_marketapp_profit(period: str, profit_ton: float, raw_response: str = None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO marketapp_profit (period, profit_ton, raw_response)
            VALUES (?, ?, ?)
        """, (period, profit_ton, raw_response))


def _sync_get_latest_profit(period: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("""
            SELECT profit_ton, recorded_at
            FROM marketapp_profit
            WHERE period = ?
            ORDER BY recorded_at DESC
            LIMIT 1
        """, (period,)).fetchone()
        return dict(row) if row else None


def _sync_get_profit_for_period(period: str, days_back: int) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT profit_ton, recorded_at
            FROM marketapp_profit
            WHERE period = ? AND recorded_at > datetime('now', ?)
            ORDER BY recorded_at DESC
        """, (period, f"-{days_back} days")).fetchall()
        return [dict(r) for r in rows]


def _sync_get_previous_profit(period: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("""
            SELECT profit_ton, recorded_at
            FROM marketapp_profit
            WHERE period = ?
            ORDER BY recorded_at DESC
            LIMIT 1 OFFSET 1
        """, (period,)).fetchone()
        return dict(row) if row else None


async def save_marketapp_profit(period: str, profit_ton: float, raw_response: str = None):
    await asyncio.to_thread(_sync_save_marketapp_profit, period, profit_ton, raw_response)


async def get_latest_profit(period: str) -> dict | None:
    return await asyncio.to_thread(_sync_get_latest_profit, period)


async def get_profit_for_period(period: str, days_back: int) -> list:
    return await asyncio.to_thread(_sync_get_profit_for_period, period, days_back)


async def get_previous_profit(period: str) -> dict | None:
    return await asyncio.to_thread(_sync_get_previous_profit, period)


def _sync_save_rent_event(event: dict, wallet: str = "") -> bool:
    if not event.get("tx_hash"):
        return False
    with get_db() as conn:
        return _sync_upsert_rent_event(conn, event, wallet) > 0


def _normalize_addr(addr: str) -> str:
    addr = (addr or "").strip()
    if addr.startswith("0:"):
        return addr.lower()
    if len(addr) == 48 and addr[:2] in ("EQ", "UQ"):
        try:
            urlsafe = addr.replace("-", "+").replace("_", "/")
            padding = (4 - len(urlsafe) % 4) % 4
            urlsafe += "=" * padding
            decoded = base64.b64decode(urlsafe)
            return "0:" + decoded[2:34].hex()
        except Exception:
            pass
    return addr.lower()


def _canonical_tx_hash(tx_hash: str) -> str:
    h = (tx_hash or "").strip()
    if not h:
        return ""
    if h.startswith("0x"):
        h = h[2:]
    raw = None
    if len(h) == 64:
        try:
            raw = bytes.fromhex(h)
        except ValueError:
            raw = None
    if raw is None:
        b = h.replace("-", "+").replace("_", "/")
        b += "=" * ((4 - len(b) % 4) % 4)
        try:
            raw = base64.b64decode(b)
        except Exception:
            raw = None
    if raw is not None and len(raw) == 32:
        return raw.hex()
    return h.lower()


def _sync_find_rent_event(conn, ts: int, src: str, dst: str, wallet: str = ""):
    row = conn.execute(
        "SELECT id, wallet FROM marketapp_rent_events "
        "WHERE ts=? AND src=? AND dst=? LIMIT 1",
        (ts, _normalize_addr(src), _normalize_addr(dst))
    ).fetchone()
    if row is None:
        return None
    norm_wallet = _normalize_addr(wallet)
    if row["wallet"] != norm_wallet:
        conn.execute(
            "UPDATE marketapp_rent_events SET wallet=? WHERE id=?",
            (norm_wallet, row["id"]),
        )
    return row


def _sync_clear_rent_events(wallet: str = ""):
    with get_db() as conn:
        if wallet:
            conn.execute("DELETE FROM marketapp_rent_events WHERE wallet=?", (_normalize_addr(wallet),))
        else:
            conn.execute("DELETE FROM marketapp_rent_events")


def _sync_upsert_rent_event(conn, event: dict, wallet: str = "") -> int:
    tx_hash = event.get("tx_hash")
    if not tx_hash:
        return 0
    canon_hash = _canonical_tx_hash(tx_hash)
    ts = int(event.get("ts", 0) or 0)
    src = _normalize_addr(event.get("src", ""))
    dst = _normalize_addr(event.get("dst", ""))
    wallet = _normalize_addr(wallet)

    existing = None
    if canon_hash:
        existing = conn.execute(
            "SELECT id FROM marketapp_rent_events WHERE tx_hash=? LIMIT 1", (canon_hash,)
        ).fetchone()
    if existing is None:
        existing = _sync_find_rent_event(conn, ts, src, dst, wallet)
    if existing:
        conn.execute("""
            UPDATE marketapp_rent_events
            SET tx_hash=?, category=?, nft_address=?, nft_name=?, collection_address=?,
                price_nano=?, currency=?, is_extend=?, duration=?, wallet=?, source='marketapp'
            WHERE id=?
        """, (
            canon_hash or tx_hash,
            event.get("category", ""),
            event.get("address", ""),
            event.get("name", ""),
            event.get("collection_address", ""),
            event.get("price_nano", "0"),
            event.get("currency", "GRAM"),
            1 if event.get("is_extend") else 0,
            int(event.get("duration", 0) or 0),
            wallet,
            existing["id"],
        ))
        return 1

    cursor = conn.execute("""
        INSERT INTO marketapp_rent_events
            (tx_hash, category, nft_address, nft_name, collection_address, ts, src, dst, price_nano, currency, is_extend, duration, wallet)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        canon_hash or tx_hash,
        event.get("category", ""),
        event.get("address", ""),
        event.get("name", ""),
        event.get("collection_address", ""),
        ts,
        src,
        dst,
        event.get("price_nano", "0"),
        event.get("currency", "GRAM"),
        1 if event.get("is_extend") else 0,
        int(event.get("duration", 0) or 0),
        wallet,
    ))
    return cursor.rowcount


def _sync_enrich_blockchain_event(conn, event: dict, wallet: str = "") -> int:
    tx_hash = event.get("tx_hash")
    canon_hash = _canonical_tx_hash(tx_hash) if tx_hash else None
    ts = int(event.get("ts", 0) or 0)
    src = _normalize_addr(event.get("src", ""))
    dst = _normalize_addr(event.get("dst", ""))

    existing = None
    if canon_hash:
        existing = conn.execute(
            "SELECT id FROM marketapp_rent_events WHERE tx_hash=? LIMIT 1", (canon_hash,)
        ).fetchone()
    if existing is None:
        existing = _sync_find_rent_event(conn, ts, src, dst, wallet)
    if existing is None:
        return 0
    conn.execute("""
        UPDATE marketapp_rent_events
        SET category=?, nft_address=?, nft_name=?, collection_address=?,
            is_extend=?, duration=?, wallet=?, source='marketapp'
        WHERE id=?
    """, (
        event.get("category", ""),
        event.get("address", ""),
        event.get("name", ""),
        event.get("collection_address", ""),
        1 if event.get("is_extend") else 0,
        int(event.get("duration", 0) or 0),
        _normalize_addr(wallet),
        existing["id"],
    ))
    return 1


def _sync_enrich_blockchain_events(events: list, wallet: str = "") -> int:
    with get_db() as conn:
        saved = 0
        for event in events:
            saved += _sync_enrich_blockchain_event(conn, event, wallet)
        return saved


def _sync_save_rent_events(events: list, wallet: str = "") -> int:
    with get_db() as conn:
        saved = 0
        for event in events:
            saved += _sync_upsert_rent_event(conn, event, wallet)
        return saved


def _sync_get_rent_events(since_ts: int = 0, is_extend: bool = None, wallet: str = "") -> list:
    with get_db() as conn:
        params = []
        query = "SELECT * FROM marketapp_rent_events WHERE 1=1"
        if wallet:
            query += " AND wallet = ?"
            params.append(_normalize_addr(wallet))
        query += " AND ts >= ?"
        params.append(since_ts)
        if is_extend is not None:
            query += " AND is_extend = ?"
            params.append(1 if is_extend else 0)
        query += " ORDER BY ts DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


async def save_rent_event(event: dict, wallet: str = "") -> bool:
    return await asyncio.to_thread(_sync_save_rent_event, event, wallet)


async def save_rent_events(events: list, wallet: str = "") -> int:
    return await asyncio.to_thread(_sync_save_rent_events, events, wallet)


async def get_rent_events(since_ts: int = 0, is_extend: bool = None, wallet: str = "") -> list:
    return await asyncio.to_thread(_sync_get_rent_events, since_ts, is_extend, wallet)


def _sync_save_blockchain_rent_events(events: list, wallet: str = "") -> int:
    with get_db() as conn:
        saved = 0
        for ev in events:
            tx_hash = ev.get("tx_hash")
            if not tx_hash:
                continue
            canon_hash = _canonical_tx_hash(tx_hash)
            ts = int(ev.get("ts", 0) or 0)
            src = _normalize_addr(ev.get("src", ""))
            dst = _normalize_addr(ev.get("dst", ""))
            wallet = _normalize_addr(wallet)
            if canon_hash:
                existing = conn.execute(
                    "SELECT id, wallet FROM marketapp_rent_events WHERE tx_hash=? LIMIT 1", (canon_hash,)
                ).fetchone()
                if existing:
                    if existing["wallet"] != wallet:
                        conn.execute(
                            "UPDATE marketapp_rent_events SET wallet=? WHERE id=?",
                            (wallet, existing["id"]),
                        )
                    continue
            if _sync_find_rent_event(conn, ts, src, dst, wallet):
                continue
            cursor = conn.execute("""
                INSERT OR IGNORE INTO marketapp_rent_events
                    (tx_hash, ts, src, dst, price_nano, source, wallet)
                VALUES (?, ?, ?, ?, ?, 'blockchain', ?)
            """, (
                canon_hash or tx_hash,
                ts,
                src,
                dst,
                str(ev.get("value_nano", "0")),
                wallet,
            ))
            saved += cursor.rowcount
        return saved


def _sync_get_sync_state(address: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM blockchain_sync_state WHERE address = ?", (address,)
        ).fetchone()
        return dict(row) if row else None


def _sync_set_sync_state(address: str, lt: str, hash_val: str, utime: int):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO blockchain_sync_state (address, last_synced_lt, last_synced_hash, last_synced_utime, last_sync_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(address) DO UPDATE SET
                last_synced_lt = excluded.last_synced_lt,
                last_synced_hash = excluded.last_synced_hash,
                last_synced_utime = excluded.last_synced_utime,
                last_sync_at = CURRENT_TIMESTAMP
        """, (address, lt, hash_val, utime))


def _sync_get_all_rent_events(wallet: str = "") -> list:
    with get_db() as conn:
        if wallet:
            rows = conn.execute(
                "SELECT * FROM marketapp_rent_events WHERE wallet=? ORDER BY ts DESC",
                (_normalize_addr(wallet),)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM marketapp_rent_events ORDER BY ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]


async def save_blockchain_rent_events(events: list, wallet: str = "") -> int:
    return await asyncio.to_thread(_sync_save_blockchain_rent_events, events, wallet)


async def get_sync_state(address: str) -> dict | None:
    return await asyncio.to_thread(_sync_get_sync_state, address)


async def set_sync_state(address: str, lt: str, hash_val: str, utime: int):
    return await asyncio.to_thread(_sync_set_sync_state, address, lt, hash_val, utime)


async def get_all_rent_events(wallet: str = "") -> list:
    return await asyncio.to_thread(_sync_get_all_rent_events, wallet)


async def clear_rent_events(wallet: str = ""):
    return await asyncio.to_thread(_sync_clear_rent_events, wallet)


async def enrich_blockchain_events(events: list, wallet: str = "") -> int:
    return await asyncio.to_thread(_sync_enrich_blockchain_events, events, wallet)
