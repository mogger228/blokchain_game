"""SQLite-хранилище блоков, игроков и ключей идемпотентности."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from blockchain import Block, create_genesis_block
from config import DEFAULT_DB_PATH, resolve_db_path


# Совместимость со старым кодом, импортировавшим эту константу.
DB_FILE = str(DEFAULT_DB_PATH)
_db_path_override: Path | None = None


def configure_db_path(path: str | Path | None) -> None:
    """Задаёт путь БД для текущего процесса; None возвращает обычное разрешение."""
    global _db_path_override
    _db_path_override = None if path is None else Path(path).expanduser().resolve()


def get_db_path() -> Path:
    return _db_path_override if _db_path_override is not None else resolve_db_path()


def db_exists() -> bool:
    return get_db_path().is_file()


def is_initialized() -> bool:
    """Проверяет таблицу blocks, не создавая отсутствующий файл."""
    path = get_db_path()
    if not path.is_file():
        return False
    conn = get_connection(path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'blocks'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_connection(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = get_db_path() if path is None else Path(path)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> bool:
    """Аддитивно создаёт схему и один генезис; True означает новый генезис."""
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    created_genesis = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blocks (
                "index" INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                amount REAL NOT NULL,
                previous_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                registered_at TEXT NOT NULL,
                initial_block_index INTEGER NOT NULL UNIQUE,
                FOREIGN KEY(initial_block_index) REFERENCES blocks("index")
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_actions (
                action_key TEXT PRIMARY KEY,
                actor_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                block_index INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(block_index) REFERENCES blocks("index")
            )
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        if count == 0:
            _insert_block(conn, create_genesis_block())
            created_genesis = True
        conn.commit()
        return created_genesis
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_block(conn: sqlite3.Connection, block: Block) -> None:
    """Вставляет блок без commit: транзакцией владеет вызывающий use case."""
    conn.execute(
        """
        INSERT INTO blocks ("index", timestamp, sender, recipient, amount, previous_hash, hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            block.index,
            block.timestamp,
            block.sender,
            block.recipient,
            block.amount,
            block.previous_hash,
            block.hash,
        ),
    )


def save_block(block: Block, conn: sqlite3.Connection | None = None) -> None:
    """Legacy helper: сохраняет и коммитит как прежняя реализация."""
    if conn is not None:
        _insert_block(conn, block)
        conn.commit()
        return

    own = get_connection()
    try:
        own.execute("BEGIN IMMEDIATE")
        _insert_block(own, block)
        own.commit()
    except Exception:
        own.rollback()
        raise
    finally:
        own.close()


def _row_to_block(row: tuple) -> Block:
    index, timestamp, sender, recipient, amount, previous_hash, hash_ = row
    return Block(index, timestamp, sender, recipient, amount, previous_hash, hash_)


def _get_all_blocks(conn: sqlite3.Connection) -> list[Block]:
    rows = conn.execute(
        'SELECT "index", timestamp, sender, recipient, amount, previous_hash, hash '
        'FROM blocks ORDER BY "index" ASC'
    ).fetchall()
    return [_row_to_block(row) for row in rows]


def get_all_blocks(conn: sqlite3.Connection | None = None) -> list[Block]:
    if conn is not None:
        return _get_all_blocks(conn)
    own = get_connection()
    try:
        return _get_all_blocks(own)
    finally:
        own.close()


def _get_last_block(conn: sqlite3.Connection) -> Block | None:
    row = conn.execute(
        'SELECT "index", timestamp, sender, recipient, amount, previous_hash, hash '
        'FROM blocks ORDER BY "index" DESC LIMIT 1'
    ).fetchone()
    return None if row is None else _row_to_block(row)


def get_last_block() -> Block | None:
    conn = get_connection()
    try:
        return _get_last_block(conn)
    finally:
        conn.close()


def _get_block_by_index(conn: sqlite3.Connection, block_index: int) -> Block | None:
    row = conn.execute(
        'SELECT "index", timestamp, sender, recipient, amount, previous_hash, hash '
        'FROM blocks WHERE "index" = ?',
        (block_index,),
    ).fetchone()
    return None if row is None else _row_to_block(row)


def get_block_by_index(block_index: int) -> Block | None:
    conn = get_connection()
    try:
        return _get_block_by_index(conn, block_index)
    finally:
        conn.close()


def _get_player_by_id(conn: sqlite3.Connection, telegram_id: int) -> tuple | None:
    return conn.execute(
        "SELECT telegram_id, username, registered_at, initial_block_index "
        "FROM players WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()


def _get_player_by_username(conn: sqlite3.Connection, username: str) -> tuple | None:
    return conn.execute(
        "SELECT telegram_id, username, registered_at, initial_block_index "
        "FROM players WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()


def _insert_player(
    conn: sqlite3.Connection,
    telegram_id: int,
    username: str,
    registered_at: str,
    initial_block_index: int,
) -> None:
    conn.execute(
        "INSERT INTO players (telegram_id, username, registered_at, initial_block_index) "
        "VALUES (?, ?, ?, ?)",
        (telegram_id, username, registered_at, initial_block_index),
    )


def _get_action(conn: sqlite3.Connection, action_key: str) -> tuple | None:
    return conn.execute(
        "SELECT action_key, actor_id, action_type, request_fingerprint, block_index, created_at "
        "FROM processed_actions WHERE action_key = ?",
        (action_key,),
    ).fetchone()


def _insert_action(
    conn: sqlite3.Connection,
    action_key: str,
    actor_id: int,
    action_type: str,
    request_fingerprint: str,
    block_index: int,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO processed_actions "
        "(action_key, actor_id, action_type, request_fingerprint, block_index, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (action_key, actor_id, action_type, request_fingerprint, block_index, created_at),
    )


def _list_players(conn: sqlite3.Connection, limit: int, offset: int) -> list[tuple]:
    return conn.execute(
        "SELECT telegram_id, username, registered_at, initial_block_index "
        "FROM players ORDER BY username COLLATE NOCASE LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def _count_players(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
