"""Форматирование блоков и длинных Telegram-сообщений."""

from __future__ import annotations

from blockchain import Block


TELEGRAM_SAFE_LIMIT = 3800


def format_amount(amount: float) -> str:
    return format(amount, ".15g")


def format_block(block: Block) -> str:
    kind = " (генезис)" if block.index == 0 else ""
    return (
        f"Блок #{block.index}{kind}\n"
        f"Время: {block.timestamp}\n"
        f"Перевод: {block.sender} → {block.recipient}\n"
        f"Сумма: ${format_amount(block.amount)}\n"
        f"Hash: {block.hash}\n"
        f"Previous hash: {block.previous_hash}"
    )


def split_chain(blocks: list[Block], limit: int = TELEGRAM_SAFE_LIMIT) -> list[str]:
    if not blocks:
        return ["Цепочка пуста."]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        entry = format_block(block)
        candidate = entry if not current else f"{current}\n\n{entry}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # Поля проекта ограничены, но оставляем безопасный fallback.
        while len(entry) > limit:
            chunks.append(entry[:limit])
            entry = entry[limit:]
        current = entry
    if current:
        chunks.append(current)
    return chunks

