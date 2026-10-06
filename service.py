"""Прикладные сценарии, общие для CLI и Telegram-интерфейса."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from dataclasses import dataclass

import storage
from blockchain import Block, calculate_balance, create_new_block, validate_chain


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$", re.ASCII)
STARTING_BALANCE = 100.0


class ServiceError(Exception):
    pass


class ValidationError(ServiceError):
    pass


class DatabaseNotInitializedError(ServiceError):
    pass


class NotRegisteredError(ServiceError):
    pass


class PlayerNotFoundError(ServiceError):
    pass


class InsufficientFundsError(ServiceError):
    def __init__(self, balance: float, amount: float):
        super().__init__(f"Недостаточно средств: баланс {balance}, требуется {amount}.")
        self.balance = balance
        self.amount = amount


class ChainIntegrityError(ServiceError):
    def __init__(self, block_index: int | None):
        super().__init__(f"Цепочка повреждена на блоке #{block_index}.")
        self.block_index = block_index


class ForbiddenError(ServiceError):
    pass


class IdempotencyConflictError(ServiceError):
    pass


@dataclass(frozen=True)
class Player:
    telegram_id: int
    username: str
    registered_at: str
    initial_block_index: int

    @property
    def account_name(self) -> str:
        return f"@{self.username}"


@dataclass(frozen=True)
class RegistrationResult:
    player: Player
    created: bool
    initial_block: Block | None


@dataclass(frozen=True)
class PlayerBalance:
    player: Player
    balance: float


@dataclass(frozen=True)
class PlayerPage:
    items: tuple[PlayerBalance, ...]
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _player(row: tuple) -> Player:
    return Player(int(row[0]), str(row[1]), str(row[2]), int(row[3]))


def initialize() -> bool:
    return storage.init_db()


def require_existing_database() -> None:
    if not storage.is_initialized():
        raise DatabaseNotInitializedError(
            "База данных не найдена. Сначала выполните: python main.py init"
        )


def normalize_username(value: str | None) -> str:
    if value is None:
        raise ValidationError("Нужен Telegram username.")
    username = value.strip()
    if username.startswith("@"):
        username = username[1:]
    username = username.lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValidationError(
            "Username должен содержать 5–32 латинских букв, цифр или символов _."
        )
    return username


def _amount(value: float | str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Сумма должна быть числом.") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValidationError("Сумма должна быть положительным конечным числом.")
    return amount


def _name(value: str, field: str) -> str:
    result = value.strip()
    if not result:
        raise ValidationError(f"Поле «{field}» не может быть пустым.")
    return result


def _valid_chain(conn) -> list[Block]:
    blocks = storage._get_all_blocks(conn)
    if not blocks:
        raise ChainIntegrityError(None)
    valid, broken = validate_chain(blocks)
    if not valid:
        raise ChainIntegrityError(broken)
    return blocks


def _fingerprint(action_type: str, actor_id: int, sender: str, recipient: str, amount: float) -> str:
    payload = json.dumps(
        [action_type, actor_id, sender, recipient, amount.hex()],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _existing_action(
    conn, action_key: str, actor_id: int, action_type: str, fingerprint: str
) -> Block | None:
    row = storage._get_action(conn, action_key)
    if row is None:
        return None
    _, stored_actor, stored_type, stored_fingerprint, block_index, _ = row
    if (
        int(stored_actor) != actor_id
        or stored_type != action_type
        or stored_fingerprint != fingerprint
    ):
        raise IdempotencyConflictError("Ключ подтверждения уже использован другой операцией.")
    block = storage._get_block_by_index(conn, int(block_index))
    if block is None:
        raise ChainIntegrityError(int(block_index))
    return block


def get_player(telegram_id: int) -> Player | None:
    require_existing_database()
    conn = storage.get_connection()
    try:
        row = storage._get_player_by_id(conn, telegram_id)
        return None if row is None else _player(row)
    finally:
        conn.close()


def get_player_by_username(username: str) -> Player | None:
    require_existing_database()
    normalized = normalize_username(username)
    conn = storage.get_connection()
    try:
        row = storage._get_player_by_username(conn, normalized)
        return None if row is None else _player(row)
    finally:
        conn.close()


def register_player(telegram_id: int, username: str | None) -> RegistrationResult:
    if telegram_id <= 0:
        raise ValidationError("Некорректный Telegram ID.")
    conn = storage.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = storage._get_player_by_id(conn, telegram_id)
        if current is not None:
            conn.commit()
            return RegistrationResult(_player(current), False, None)
        normalized = normalize_username(username)
        owner = storage._get_player_by_username(conn, normalized)
        if owner is not None:
            raise ValidationError("Этот username уже зарегистрирован другим игроком.")
        blocks = _valid_chain(conn)
        initial = create_new_block(blocks[-1], "system", f"@{normalized}", STARTING_BALANCE)
        registered_at = _now()
        storage._insert_block(conn, initial)
        storage._insert_player(conn, telegram_id, normalized, registered_at, initial.index)
        conn.commit()
        return RegistrationResult(
            Player(telegram_id, normalized, registered_at, initial.index), True, initial
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_player_transaction(
    telegram_id: int,
    recipient_username: str,
    amount: float | str,
    action_key: str,
) -> Block:
    recipient_name = normalize_username(recipient_username)
    value = _amount(amount)
    if not action_key or len(action_key) > 128:
        raise ValidationError("Некорректный ключ подтверждения.")
    conn = storage.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sender_row = storage._get_player_by_id(conn, telegram_id)
        if sender_row is None:
            raise NotRegisteredError("Сначала зарегистрируйтесь через /start.")
        sender = _player(sender_row)
        recipient_account = f"@{recipient_name}"
        fingerprint = _fingerprint(
            "player_transfer", telegram_id, sender.account_name, recipient_account, value
        )
        existing = _existing_action(
            conn, action_key, telegram_id, "player_transfer", fingerprint
        )
        if existing is not None:
            conn.commit()
            return existing
        recipient_row = storage._get_player_by_username(conn, recipient_name)
        if recipient_row is None:
            raise PlayerNotFoundError("Получатель ещё не зарегистрирован.")
        recipient = _player(recipient_row)
        if sender.telegram_id == recipient.telegram_id:
            raise ValidationError("Нельзя переводить самому себе.")
        blocks = _valid_chain(conn)
        balance = calculate_balance(blocks, sender.account_name)
        if value > balance:
            raise InsufficientFundsError(balance, value)
        block = create_new_block(blocks[-1], sender.account_name, recipient.account_name, value)
        storage._insert_block(conn, block)
        storage._insert_action(
            conn, action_key, telegram_id, "player_transfer", fingerprint, block.index, _now()
        )
        conn.commit()
        return block
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_admin_grant(
    admin_telegram_id: int,
    recipient_username: str,
    amount: float | str,
    action_key: str,
    configured_admin_id: int,
) -> Block:
    if admin_telegram_id != configured_admin_id:
        raise ForbiddenError("Недостаточно прав.")
    recipient_name = normalize_username(recipient_username)
    value = _amount(amount)
    if not action_key or len(action_key) > 128:
        raise ValidationError("Некорректный ключ подтверждения.")
    conn = storage.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Повторная проверка находится внутри транзакционной границы use case.
        if admin_telegram_id != configured_admin_id:
            raise ForbiddenError("Недостаточно прав.")
        recipient_account = f"@{recipient_name}"
        fingerprint = _fingerprint(
            "admin_grant", admin_telegram_id, "system", recipient_account, value
        )
        existing = _existing_action(
            conn, action_key, admin_telegram_id, "admin_grant", fingerprint
        )
        if existing is not None:
            conn.commit()
            return existing
        recipient_row = storage._get_player_by_username(conn, recipient_name)
        if recipient_row is None:
            raise PlayerNotFoundError("Игрок не найден.")
        recipient = _player(recipient_row)
        blocks = _valid_chain(conn)
        block = create_new_block(blocks[-1], "system", recipient.account_name, value)
        storage._insert_block(conn, block)
        storage._insert_action(
            conn, action_key, admin_telegram_id, "admin_grant", fingerprint, block.index, _now()
        )
        conn.commit()
        return block
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_trusted_transaction(sender: str, recipient: str, amount: float | str) -> Block:
    sender_name = _name(sender, "отправитель")
    recipient_name = _name(recipient, "получатель")
    value = _amount(amount)
    if sender_name == recipient_name:
        raise ValidationError("Нельзя переводить самому себе.")
    conn = storage.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        blocks = _valid_chain(conn)
        if sender_name != "system":
            balance = calculate_balance(blocks, sender_name)
            if value > balance:
                raise InsufficientFundsError(balance, value)
        block = create_new_block(blocks[-1], sender_name, recipient_name, value)
        storage._insert_block(conn, block)
        conn.commit()
        return block
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_player_balance(telegram_id: int) -> PlayerBalance:
    require_existing_database()
    conn = storage.get_connection()
    try:
        conn.execute("BEGIN")
        row = storage._get_player_by_id(conn, telegram_id)
        if row is None:
            raise NotRegisteredError("Игрок не зарегистрирован.")
        player = _player(row)
        blocks = storage._get_all_blocks(conn)
        result = PlayerBalance(player, calculate_balance(blocks, player.account_name))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_named_balance(user: str) -> float:
    require_existing_database()
    return calculate_balance(storage.get_all_blocks(), _name(user, "пользователь"))


def list_players_with_balances(page: int = 0, page_size: int = 10) -> PlayerPage:
    if page < 0 or page_size < 1 or page_size > 100:
        raise ValidationError("Некорректная страница.")
    require_existing_database()
    conn = storage.get_connection()
    try:
        conn.execute("BEGIN")
        total = storage._count_players(conn)
        total_pages = max(1, (total + page_size - 1) // page_size)
        safe_page = min(page, total_pages - 1)
        rows = storage._list_players(conn, page_size, safe_page * page_size)
        blocks = storage._get_all_blocks(conn)
        items: list[PlayerBalance] = []
        for row in rows:
            player = _player(row)
            items.append(PlayerBalance(player, calculate_balance(blocks, player.account_name)))
        conn.commit()
        return PlayerPage(tuple(items), safe_page, page_size, total)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_transaction(transaction_id: int) -> Block | None:
    require_existing_database()
    if transaction_id < 0:
        raise ValidationError("ID должен быть неотрицательным целым числом.")
    if transaction_id > 2**63 - 1:
        return None
    return storage.get_block_by_index(transaction_id)


def get_chain() -> list[Block]:
    require_existing_database()
    return storage.get_all_blocks()


def check_chain() -> tuple[bool, int | None]:
    return validate_chain(get_chain())
