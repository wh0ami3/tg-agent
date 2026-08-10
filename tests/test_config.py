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
    # У Windows нет POSIX-битов: там файл защищают ACL, а не режим. Проверять
    # 0600 на нём бессмысленно — но и молчать нельзя, см. README про Windows.
    if os.name != "posix":
        print("  ⏭ права 0600 — POSIX-режимов нет на этой ОС")
    else:
        ok("права 0600", oct(cfg.CONFIG.stat().st_mode & 0o777) == "0o600",
           oct(cfg.CONFIG.stat().st_mode & 0o777))

    cfg.persist("TGAGENT_MODEL", "haiku")
    lines = cfg.CONFIG.read_text(encoding="utf-8").splitlines()
    ok("перезапись не плодит дублей", sum(1 for l in lines if l.startswith("TGAGENT_MODEL=")) == 1)
    ok("токен пережил перезапись модели", cfg.token() == "123:abc")

    cfg.persist("TGAGENT_MODEL", "")
    ok("пустое значение удаляет строку", "TGAGENT_MODEL" not in cfg.CONFIG.read_text(encoding="utf-8"))
    ok("дефолт модели — sonnet", cfg.model() == "sonnet")


def test_injection():
    print("— инъекция через перевод строки —")
    before = cfg.CONFIG.read_text(encoding="utf-8")
    cfg.persist("TGAGENT_MODEL", "opus\nTGAGENT_TELEGRAM_TOKEN=stolen")
    ok("значение с \\n не записано", cfg.CONFIG.read_text(encoding="utf-8") == before)
    cfg.persist("EVIL\nKEY", "1")
    ok("ключ с \\n не записан", cfg.CONFIG.read_text(encoding="utf-8") == before)


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
    cfg.GEMINI_ENV.write_text("JARVIS_BRAIN=claude\nGEMINI_API_KEY=g-key-1\n# comment\n", encoding="utf-8")
    ok("ключ читается", cfg.gemini_key() == "g-key-1")


def test_parse_tolerance():
    print("— парсер терпит рукописный файл —")
    cfg.GEMINI_ENV.write_text("  KEY1 = v1 \nбез-равно\n#KEY2=x\nKEY3=a=b\n", encoding="utf-8")
    d = cfg._parse(cfg.GEMINI_ENV)
    ok("пробелы срезаются", d.get("KEY1") == "v1")
    ok("строка без = пропускается", "без-равно" not in d)
    ok("комментарий пропускается", "#KEY2" not in d and "KEY2" not in d)
    ok("= в значении цел", d.get("KEY3") == "a=b")


def test_backend():
    print("— выбор мозга —")
    import os
    os.environ.pop("TGAGENT_BACKEND", None)
    cfg.persist("TGAGENT_BACKEND", "")
    ok("по умолчанию claude-cli", cfg.backend() == "claude-cli")

    cfg.persist("TGAGENT_BACKEND", "openai")
    ok("читается из конфига", cfg.backend() == "openai")

    os.environ["TGAGENT_BACKEND"] = "anthropic"
    ok("окружение важнее конфига", cfg.backend() == "anthropic")

    os.environ["TGAGENT_BACKEND"] = "ANTHROPIC"
    ok("регистр не важен", cfg.backend() == "anthropic")

    # опечатка не должна ронять бота — молча откатываемся на рабочий дефолт
    os.environ["TGAGENT_BACKEND"] = "anthropik"
    ok("неизвестное имя → claude-cli", cfg.backend() == "claude-cli")

    os.environ.pop("TGAGENT_BACKEND", None)
    cfg.persist("TGAGENT_BACKEND", "")


def test_model_by_backend():
    print("— модель зависит от мозга —")
    import os
    cfg.persist("TGAGENT_MODEL", "qwen3:8b")

    os.environ["TGAGENT_BACKEND"] = "claude-cli"
    ok("строгий мозг отбивает чужое имя", cfg.model() == cfg.DEFAULT_MODEL)
    ok("и model_ok его не пускает", not cfg.model_ok("qwen3:8b"))
    ok("а свой алиас пускает", cfg.model_ok("Opus"))
    ok("нормализация к нижнему регистру", cfg.normalize_model("Opus") == "opus")

    os.environ["TGAGENT_BACKEND"] = "openai"
    ok("openai отдаёт имя как есть", cfg.model() == "qwen3:8b")
    ok("и пускает любое непустое", cfg.model_ok("llama-3.3-70b-versatile"))
    ok("пустое не пускает даже openai", not cfg.model_ok("   "))
    ok("регистр у openai сохраняется", cfg.normalize_model("Qwen3:8B") == "Qwen3:8B")

    os.environ.pop("TGAGENT_BACKEND", None)
    cfg.persist("TGAGENT_MODEL", "")


def test_api_credentials():
    print("— ключи и адрес API —")
    import os
    for v in ("TGAGENT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
              "TGAGENT_API_BASE", "TGAGENT_BACKEND"):
        os.environ.pop(v, None)
    cfg.persist("TGAGENT_API_KEY", "")
    cfg.persist("ANTHROPIC_API_KEY", "")
    cfg.persist("TGAGENT_API_BASE", "")

    ok("нет ключа — пусто", cfg.api_key("anthropic") == "")

    cfg.persist("ANTHROPIC_API_KEY", "из-конфига")
    ok("ключ из конфига по имени провайдера", cfg.api_key("anthropic") == "из-конфига")

    os.environ["ANTHROPIC_API_KEY"] = "из-окружения"
    ok("окружение важнее конфига", cfg.api_key("anthropic") == "из-окружения")

    os.environ["TGAGENT_API_KEY"] = "общий"
    ok("общий ключ важнее всех", cfg.api_key("anthropic") == "общий")

    ok("ключ чужого провайдера не подхватывается",
       cfg.api_key("openai") == "общий")

    os.environ.pop("TGAGENT_API_KEY", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    ok("openai без ключа пуст", cfg.api_key("openai") == "")

    ok("адрес по умолчанию пуст", cfg.api_base() == "")
    cfg.persist("TGAGENT_API_BASE", "http://пк:11434/v1")
    ok("адрес из конфига", cfg.api_base() == "http://пк:11434/v1")
    os.environ["TGAGENT_API_BASE"] = "http://other:8000/v1"
    ok("адрес из окружения важнее", cfg.api_base() == "http://other:8000/v1")
    os.environ.pop("TGAGENT_API_BASE", None)
    cfg.persist("TGAGENT_API_BASE", "")
    cfg.persist("ANTHROPIC_API_KEY", "")


test_roundtrip()
test_injection()
test_chat_id()
test_model_validation()
test_gemini_key()
test_parse_tolerance()
test_backend()
test_model_by_backend()
test_api_credentials()

print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
if FAIL:
    print("Провалены:", FAIL)
    sys.exit(1)
