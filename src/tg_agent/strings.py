"""Строки, которые видит хозяин в чате. Язык — любой.

Три уровня, от простого к гибкому:

1. Встроенные переводы: en (дефолт) и ru. Проверены живьём.
2. Любой другой язык — JSON-файл `<код>.json` в папке локалей
   (`~/.jarvis/tg-agent-locales`, переопределяется TGAGENT_LOCALE_DIR).
   Переводить всё не обязательно: недостающие ключи берутся из английского,
   поэтому частичный перевод не ломает бота, а просто смешивает языки.
3. Язык ответов САМОГО агента (TGAGENT_REPLY_LANG) — свободная строка,
   уходит в системный промпт: модель говорит на любом языке без переводов
   с нашей стороны. Это и есть настоящая поддержка «всех языков»:
   переводить руками надо только 20 служебных фраз бота.

Ключи — стабильные, тексты — данные. Тесты проверяют поведение по ключам,
а не по конкретным фразам.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

LANG = os.environ.get("TGAGENT_LANG", "en").strip().lower()

LOCALE_DIR = Path(
    os.environ.get("TGAGENT_LOCALE_DIR")
    or Path.home() / ".jarvis" / "tg-agent-locales"
)

COMMANDS = "/model, /screen, /stop, /reset"

EN: dict[str, str] = {
    "welcome": (
        "At your service. This chat is now your remote for the computer.\n"
        "Type or send a voice note telling me what to do. Commands: {commands}"
    ),
    "already_up": "Already here. Commands: {commands}",
    "restarted": (
        "I restarted. The task I was running got interrupted — send it again "
        "if you still need it."
    ),
    "model_current": "Brain model: {model}\nChange it: /model sonnet | opus | haiku",
    "model_set": "Model is now {model}.",
    "model_unknown": "I don't know the model «{model}». Options: sonnet, opus, haiku.",
    "unknown_command": "I don't know that command. Available: {commands}",
    "stopping": "⛔ Stopping the current task.",
    "nothing_to_stop": "Nothing to stop — no task is running.",
    "reset_and_stopped": "⛔ Task stopped, conversation started fresh.",
    "reset_only": "Conversation started fresh.",
    "working": "⏳ Working…",
    "aborted_by_owner": "⛔ Stopped at your request.",
    "interrupted_by_restart": "⚠ interrupted by an agent restart",
    "failed": "Didn't work: {error}",
    "done": "Done.",
    "not_done": "Didn't work.",
    "voice_too_big": "That voice note is too large for me to download.",
    "voice_failed": "I couldn't transcribe that voice note.",
    "voice_empty": "I didn't catch anything — no speech in that voice note.",
    "heard": "Heard: «{text}»",
    "screenshot_failed": "Screenshot failed: {error}",
    "file_unreadable": "Can't read the file: {path}",
    "photo_too_big": "That image is over 10 MB — can't send it: {path}",
}

RU: dict[str, str] = {
    "welcome": (
        "К вашим услугам, сэр. Этот чат теперь ваш пульт от ноутбука.\n"
        "Пишите или наговаривайте, что сделать. Команды: {commands}"
    ),
    "already_up": "Уже на связи, сэр. Команды: {commands}",
    "restarted": (
        "Перезапустился. Задача, которую я делал, была прервана — "
        "повторите, если нужно."
    ),
    "model_current": "Модель мозга: {model}\nСменить: /model sonnet | opus | haiku",
    "model_set": "Модель теперь {model}.",
    "model_unknown": "Не знаю модель «{model}». Варианты: sonnet, opus, haiku.",
    "unknown_command": "Не знаю такой команды. Есть: {commands}",
    "stopping": "⛔ Останавливаю текущую задачу.",
    "nothing_to_stop": "Нечего останавливать — задач в работе нет.",
    "reset_and_stopped": "⛔ Задачу остановил, диалог начат заново.",
    "reset_only": "Диалог начат заново.",
    "working": "⏳ Выполняю…",
    "aborted_by_owner": "⛔ Остановлено по вашей команде.",
    "interrupted_by_restart": "⚠ прервано перезапуском агента",
    "failed": "Не вышло: {error}",
    "done": "Готово.",
    "not_done": "Не вышло.",
    "voice_too_big": "Голосовое слишком большое — не смогу скачать.",
    "voice_failed": "Не смог распознать голосовое.",
    "voice_empty": "Не расслышал — речи в голосовом не нашлось.",
    "heard": "Услышал: «{text}»",
    "screenshot_failed": "Скриншот не вышел: {error}",
    "file_unreadable": "Файл не читается: {path}",
    "photo_too_big": "Картинка больше 10 МБ — не отправить: {path}",
}

BUILTIN: dict[str, dict[str, str]] = {"en": EN, "ru": RU}

_cache: dict[str, dict[str, str]] = {}


def _load_file(lang: str) -> dict[str, str]:
    """<код>.json из папки локалей. Битый или чужой формат — молча пропускаем:
    сломанный перевод не должен ронять бота, английский всегда есть."""
    path = LOCALE_DIR / f"{lang}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def table(lang: str | None = None) -> dict[str, str]:
    """Таблица строк для языка: файл поверх встроенного, дырки — из английского."""
    code = (lang if lang is not None else LANG).strip().lower()
    # ru-RU / ru_RU / ru — один и тот же язык
    code = code.replace("_", "-").split("-")[0] or "en"
    if code in _cache:
        return _cache[code]
    merged = {**EN, **BUILTIN.get(code, {}), **_load_file(code)}
    _cache[code] = merged
    return merged


def available() -> list[str]:
    """Языки, которые бот реально может показать: встроенные + найденные файлы."""
    found = set(BUILTIN)
    try:
        found.update(p.stem.lower() for p in LOCALE_DIR.glob("*.json"))
    except OSError:
        pass
    return sorted(found)


def t(key: str, **kw: object) -> str:
    """Строка по ключу. Неизвестный ключ — сам ключ, недостающая подстановка —
    строка как есть: кривой текст в чате лучше упавшей задачи."""
    tpl = table().get(key)
    if tpl is None:
        return key
    try:
        return tpl.format(commands=COMMANDS, **kw)
    except (KeyError, IndexError):
        return tpl
