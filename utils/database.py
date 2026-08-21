import sqlite3
import logging
import time
import threading
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

            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_tournaments_chat ON tournaments(chat_id);
            CREATE INDEX IF NOT EXISTS idx_tournament_players_tid ON tournament_players(tournament_id);
        """)
    logger.info("База данных инициализирована")


def upsert_user(user_id: int, username: str = None, first_name: str = None):
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


def log_message(user_id: int, chat_id: int, chat_type: str, speaker: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO messages (user_id, chat_id, chat_type, speaker)
            VALUES (?, ?, ?, ?)
        """, (user_id, chat_id, chat_type, speaker))
        increment_stat("total_messages")


def check_rate_limit(user_id: int, chat_id: int, cooldown: float = 3.0, max_per_minute: int = 5) -> bool:
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


def save_tournament(chat_id: int, admin_id: int, mode: str, total_rounds: int,
                     total_players: int, winner_id: int = None, winner_name: str = None) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO tournaments (chat_id, admin_id, ended_at, winner_id, winner_name, mode, total_rounds, total_players)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)
        """, (chat_id, admin_id, winner_id, winner_name, mode, total_rounds, total_players))
        return cursor.lastrowid


def save_tournament_players(tournament_id: int, players: dict):
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


def get_tournament_history(limit: int = 10) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT t.*, 
                   (SELECT COUNT(*) FROM tournament_players WHERE tournament_id=t.id) as player_count
            FROM tournaments t
            ORDER BY t.ended_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_tournament_leaderboard(limit: int = 10) -> list:
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


def get_top_speakers(limit: int = 10) -> list:
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


def get_active_users(days: int = 7, limit: int = 10) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT username, first_name, message_count
            FROM users
            WHERE last_seen > datetime('now', ?)
            ORDER BY message_count DESC
            LIMIT ?
        """, (f"-{days} days", limit)).fetchall()
        return [dict(r) for r in rows]


def increment_stat(key: str, amount: int = 1):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO bot_stats (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = bot_stats.value + ?,
                updated_at = CURRENT_TIMESTAMP
        """, (key, amount, amount))


def get_stat(key: str) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM bot_stats WHERE key=?", (key,)).fetchone()
        return row["value"] if row else 0


def get_total_users() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"] if row else 0


def get_total_tournaments() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM tournaments").fetchone()
        return row["cnt"] if row else 0


def get_messages_today() -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE timestamp > datetime('now', 'start of day')"
        ).fetchone()
        return row["cnt"] if row else 0


def is_user_banned(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row["is_banned"]) if row else False
