"""Конфиг агента: ~/.jarvis/tg-agent.env (токен, хозяин, модель).

Отдельный от ~/.jarvis/env файл: у Джарвиса свой конфиг, у агента свой.
Общий только GEMINI_API_KEY — он читается из env Джарвиса при каждом
обращении (источник истины там: Джесси ротирует ключ в одном месте).

Запись — атомарная (tmp 0600 + replace), с отказом на перевод строки
в ключе или значении: наследие грабли env-инъекции в мосте (KEY=VALUE
с \n внутри значения дописывал бы чужую строку в файл).
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG = Path.home() / ".jarvis" / "tg-agent.env"
JARVIS_ENV = Path.home() / ".jarvis" / "env"

# /model показывает первые три; fable принимается, но не рекламируется
MODELS = ("sonnet", "opus", "haiku", "fable")
DEFAULT_MODEL = "sonnet"


def _parse(path: Path) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, eq, val = line.partition("=")
        if eq and (k := key.strip()) and not k.startswith("#"):
            out[k] = val.strip()
    return out


def load() -> dict[str, str]:
    return _parse(CONFIG)


def persist(key: str, value: str) -> None:
    """KEY=VALUE в tg-agent.env; пустое value удаляет строку.

    Сбой чтения существующего файла — отказ от записи: иначе один тумблер
    затёр бы токен. Ошибку записи пробрасываем — вызывающий должен знать,
    что настройка не сохранилась.
    """
    if any(c in "\n\r" for c in key + value):
        print(f"[cfg] {key!r}: перевод строки в ключе/значении — отказ", flush=True)
        return
    try:
        lines = CONFIG.read_text().splitlines() if CONFIG.exists() else []
    except OSError as e:
        print(f"[cfg] не читается {CONFIG} ({e}) — настройку не сохраняю", flush=True)
        return
    lines = [l for l in lines if l.partition("=")[0].strip() != key]
    if value:
        lines.append(f"{key}={value}")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_name(f".tg-agent.env.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, CONFIG)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def token() -> str:
    return load().get("TGAGENT_TELEGRAM_TOKEN", "").strip()


def chat_id() -> int | None:
    raw = load().get("TGAGENT_CHAT_ID", "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def model() -> str:
    m = load().get("TGAGENT_MODEL", "").strip().lower()
    return m if m in MODELS else DEFAULT_MODEL


def gemini_key() -> str:
    return _parse(JARVIS_ENV).get("GEMINI_API_KEY", "").strip()
