"""Конфиг агента: токен, хозяин, модель — в config.env (см. paths.py).

Ключ Gemini для распознавания голосовых по умолчанию берётся оттуда же.
TGAGENT_GEMINI_ENV_FILE указывает на ДРУГОЙ env-файл, если ключ уже живёт
в одном месте на всю машину и ротируется там: читается на каждый запрос,
поэтому смена ключа подхватывается без перезапуска.

Запись — атомарная (tmp 0600 + replace), с отказом на перевод строки
в ключе или значении: KEY=VALUE с \n внутри значения дописал бы в файл
чужую строку.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import CONFIG_FILE

CONFIG = CONFIG_FILE

# Откуда брать GEMINI_API_KEY. Пусто — из своего же конфига.
_GEMINI_ENV_RAW = os.environ.get("TGAGENT_GEMINI_ENV_FILE", "").strip()
GEMINI_ENV = Path(_GEMINI_ENV_RAW) if _GEMINI_ENV_RAW else CONFIG

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
    """Ключ Gemini. Сначала окружение, потом env-файл: в systemd-юните
    удобнее переменной, в конфиге — когда ключ общий на машину."""
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or _parse(GEMINI_ENV).get("GEMINI_API_KEY", "").strip()
    )
