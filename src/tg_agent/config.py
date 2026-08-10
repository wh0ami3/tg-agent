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
    m = load().get("TGAGENT_MODEL", "").strip()
    if backend() == "openai":
        # имя модели локального движка нам заранее неизвестно: qwen3:8b,
        # llama-3.3-70b-versatile, anthropic/claude-sonnet-5 — отдаём как есть
        return m
    m = m.lower()
    return m if m in MODELS else DEFAULT_MODEL


def gemini_key() -> str:
    """Ключ Gemini. Сначала окружение, потом env-файл: в systemd-юните
    удобнее переменной, в конфиге — когда ключ общий на машину."""
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or _parse(GEMINI_ENV).get("GEMINI_API_KEY", "").strip()
    )


# ─────────────────────────────── сменный мозг ────────────────────────────────
BACKENDS = ("claude-cli", "anthropic", "openai")


def backend() -> str:
    """Какой мозг думает за агента.

    Окружение поверх конфига — в systemd-юните переменной удобнее. Неизвестное
    имя молча падает в claude-cli: у хозяина должен подниматься бот, а не
    трейсбек из-за опечатки в одной букве.
    """
    b = (os.environ.get("TGAGENT_BACKEND", "").strip()
         or load().get("TGAGENT_BACKEND", "").strip()).lower()
    return b if b in BACKENDS else "claude-cli"


def api_key(which: str = "") -> str:
    """Ключ мозга. Читается В МОМЕНТ запроса — ротация без перезапуска."""
    which = which or backend()
    var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(which, "")
    cfg = load()
    return (os.environ.get("TGAGENT_API_KEY", "").strip()
            or (os.environ.get(var, "").strip() if var else "")
            or cfg.get("TGAGENT_API_KEY", "").strip()
            or (cfg.get(var, "").strip() if var else ""))


def api_base() -> str:
    """Свой endpoint. Локальные движки: Ollama — http://ПК:11434/v1,
    LM Studio — :1234/v1, vLLM — :8000/v1. Для anthropic — база БЕЗ /v1."""
    return (os.environ.get("TGAGENT_API_BASE", "").strip()
            or load().get("TGAGENT_API_BASE", "").strip())


def model_ok(m: str) -> bool:
    """claude-cli и anthropic знают только свои алиасы — «/model gpt-99»
    по-прежнему отбивается. У openai имена произвольные, их задаёт движок."""
    return bool(m.strip()) if backend() == "openai" else m.strip().lower() in MODELS


def normalize_model(m: str) -> str:
    return m.strip() if backend() == "openai" else m.strip().lower()
