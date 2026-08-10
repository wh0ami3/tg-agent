"""Юниты конфига: load/persist, атомарность, права, отказ на \n, дефолты.

Запуск: uv run --project /home/jesse/Projects/tg-agent python tests/test_config.py
Всё офлайн, реальный ~/.jarvis не трогается (пути подменяются на tmp).
"""
import os
import sys
import tempfile
from pathlib import Path

import tg_agent.config as cfg

_TMP = Path(tempfile.mkdtemp(prefix="tg-agent-test-"))
cfg.CONFIG = _TMP / "tg-agent.env"
cfg.GEMINI_ENV = _TMP / "env"

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))


def test_roundtrip():
    print("— persist/load —")
    cfg.persist("TGAGENT_TELEGRAM_TOKEN", "123:abc")
    cfg.persist("TGAGENT_MODEL", "opus")
    ok("значения читаются", cfg.token() == "123:abc" and cfg.model() == "opus")
    ok("права 0600", oct(cfg.CONFIG.stat().st_mode & 0o777) == "0o600",
       oct(cfg.CONFIG.stat().st_mode & 0o777))

    cfg.persist("TGAGENT_MODEL", "haiku")
    lines = cfg.CONFIG.read_text().splitlines()
    ok("перезапись не плодит дублей", sum(1 for l in lines if l.startswith("TGAGENT_MODEL=")) == 1)
    ok("токен пережил перезапись модели", cfg.token() == "123:abc")

    cfg.persist("TGAGENT_MODEL", "")
    ok("пустое значение удаляет строку", "TGAGENT_MODEL" not in cfg.CONFIG.read_text())
    ok("дефолт модели — sonnet", cfg.model() == "sonnet")


def test_injection():
    print("— инъекция через перевод строки —")
    before = cfg.CONFIG.read_text()
    cfg.persist("TGAGENT_MODEL", "opus\nTGAGENT_TELEGRAM_TOKEN=stolen")
    ok("значение с \\n не записано", cfg.CONFIG.read_text() == before)
    cfg.persist("EVIL\nKEY", "1")
    ok("ключ с \\n не записан", cfg.CONFIG.read_text() == before)


def test_chat_id():
    print("— chat_id —")
    ok("нет ключа — None", cfg.chat_id() is None)
    cfg.persist("TGAGENT_CHAT_ID", "555")
    ok("парсится", cfg.chat_id() == 555)
    cfg.persist("TGAGENT_CHAT_ID", "-100123")
    ok("отрицательный (группа) парсится", cfg.chat_id() == -100123)
    cfg.persist("TGAGENT_CHAT_ID", "мусор")
    ok("мусор — None", cfg.chat_id() is None)
    cfg.persist("TGAGENT_CHAT_ID", "")


def test_model_validation():
    print("— валидация модели —")
    cfg.persist("TGAGENT_MODEL", "gpt-99")
    ok("незнакомая модель — дефолт", cfg.model() == "sonnet")
    cfg.persist("TGAGENT_MODEL", "OPUS")
    ok("регистр не важен", cfg.model() == "opus")
    cfg.persist("TGAGENT_MODEL", "")


def test_gemini_key():
    print("— GEMINI_API_KEY из env Джарвиса —")
    ok("нет файла — пусто", cfg.gemini_key() == "")
    cfg.GEMINI_ENV.write_text("JARVIS_BRAIN=claude\nGEMINI_API_KEY=g-key-1\n# comment\n")
    ok("ключ читается", cfg.gemini_key() == "g-key-1")


def test_parse_tolerance():
    print("— парсер терпит рукописный файл —")
    cfg.GEMINI_ENV.write_text("  KEY1 = v1 \nбез-равно\n#KEY2=x\nKEY3=a=b\n")
    d = cfg._parse(cfg.GEMINI_ENV)
    ok("пробелы срезаются", d.get("KEY1") == "v1")
    ok("строка без = пропускается", "без-равно" not in d)
    ok("комментарий пропускается", "#KEY2" not in d and "KEY2" not in d)
    ok("= в значении цел", d.get("KEY3") == "a=b")


test_roundtrip()
test_injection()
test_chat_id()
test_model_validation()
test_gemini_key()
test_parse_tolerance()

print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
if FAIL:
    print("Провалены:", FAIL)
    sys.exit(1)
