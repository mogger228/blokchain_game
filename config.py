"""Конфигурация проекта без чтения секретов во время импорта."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "chain.db"


class ConfigurationError(ValueError):
    """Ошибка пользовательской конфигурации."""


def resolve_db_path(
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Возвращает явный путь БД и не выбирает legacy-файл из чужого CWD молча."""
    env = os.environ if environ is None else environ
    current_dir = (Path.cwd() if cwd is None else cwd).resolve()
    root = (PROJECT_DIR if project_dir is None else project_dir).resolve()

    configured = env.get("BLOCKCHAIN_DB_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = current_dir / path
        return path.resolve()

    default_path = root / "chain.db"
    legacy_path = current_dir / "chain.db"
    if current_dir != root and legacy_path.exists() and legacy_path.resolve() != default_path.resolve():
        raise ConfigurationError(
            "Обнаружен legacy-файл chain.db в текущем каталоге. "
            "Укажите нужный файл явно через BLOCKCHAIN_DB_PATH."
        )
    return default_path


@dataclass(frozen=True)
class BotConfig:
    token: str
    admin_id: int
    db_path: Path


def load_bot_config(environ: Mapping[str, str] | None = None) -> BotConfig:
    if environ is None:
        # Читаем .env рядом с кодом, независимо от каталога запуска.
        # Переменные окружения имеют приоритет над настройками файла.
        env = {
            key: value
            for key, value in dotenv_values(PROJECT_DIR / ".env", encoding="utf-8-sig").items()
            if value is not None
        }
        env.update(os.environ)
    else:
        env = environ
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigurationError("Не задан TELEGRAM_BOT_TOKEN.")

    raw_admin_id = env.get("TELEGRAM_ADMIN_ID", "").strip()
    try:
        admin_id = int(raw_admin_id)
    except ValueError as exc:
        raise ConfigurationError("TELEGRAM_ADMIN_ID должен быть целым числом.") from exc
    if admin_id <= 0:
        raise ConfigurationError("TELEGRAM_ADMIN_ID должен быть положительным числом.")

    return BotConfig(token=token, admin_id=admin_id, db_path=resolve_db_path(env))
