"""Юниты локализации: встроенные языки, файлы локалей, откат на английский,
устойчивость к битым переводам.

Запуск: uv run python tests/test_strings.py
Всё офлайн.
"""
import json
import sys
import tempfile
from pathlib import Path

import tg_agent.strings as S

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))


def fresh(lang, locale_dir=None):
    """Чистый выбор языка: кэш таблиц сбрасывается, иначе тесты видят чужой."""
    S._cache.clear()
    S.LANG = lang
    if locale_dir is not None:
        S.LOCALE_DIR = locale_dir


def test_builtin():
    print("— встроенные языки —")
    ok("英/рус наборы имеют одинаковые ключи", set(S.EN) == set(S.RU),
       str(set(S.EN) ^ set(S.RU)))
    ok("пустых строк нет", all(v.strip() for v in {**S.EN, **S.RU}.values()))

    fresh("en")
    ok("дефолт — английский", S.t("done") == S.EN["done"])
    fresh("ru")
    ok("ru отдаёт русский", S.t("done") == S.RU["done"])


def test_locale_normalisation():
    print("— нормализация кода языка —")
    for code in ("ru", "RU", "ru-RU", "ru_RU", "  Ru-latn  "):
        fresh(code)
        ok(f"«{code}» → русский", S.t("done") == S.RU["done"])
    fresh("")
    ok("пустой код → английский", S.t("done") == S.EN["done"])


def test_unknown_language_falls_back():
    print("— незнакомый язык —")
    fresh("kl")  # гренландского у нас нет
    ok("неизвестный язык не падает", S.t("done") == S.EN["done"])
    ok("и не теряет ключи", set(S.table()) == set(S.EN))


def test_locale_file():
    print("— файл локали —")
    tmp = Path(tempfile.mkdtemp(prefix="tg-agent-loc-"))
    (tmp / "de.json").write_text(
        json.dumps({"done": "Fertig.", "working": "⏳ Arbeite…"}), encoding="utf-8"
    )
    fresh("de", tmp)
    ok("перевод из файла подхвачен", S.t("done") == "Fertig.")
    ok("частичный перевод дополняется английским", S.t("not_done") == S.EN["not_done"])
    ok("файл виден в списке языков", "de" in S.available())

    # файл перекрывает встроенный язык
    (tmp / "ru.json").write_text(json.dumps({"done": "Всё."}), encoding="utf-8")
    fresh("ru", tmp)
    ok("файл важнее встроенного", S.t("done") == "Всё.")
    ok("остальное осталось встроенным", S.t("not_done") == S.RU["not_done"])


def test_broken_locale_file():
    print("— битый файл не роняет бота —")
    tmp = Path(tempfile.mkdtemp(prefix="tg-agent-loc-bad-"))
    (tmp / "fr.json").write_text("{ это не json", encoding="utf-8")
    fresh("fr", tmp)
    ok("битый json игнорируется", S.t("done") == S.EN["done"])

    (tmp / "es.json").write_text(json.dumps(["не", "словарь"]), encoding="utf-8")
    fresh("es", tmp)
    ok("json не-словарь игнорируется", S.t("done") == S.EN["done"])

    (tmp / "it.json").write_text(json.dumps({"done": 42, "working": "Lavoro…"}), encoding="utf-8")
    fresh("it", tmp)
    ok("нестроковое значение отброшено", S.t("done") == S.EN["done"])
    ok("а строковое рядом — принято", S.t("working") == "Lavoro…")


def test_substitution():
    print("— подстановки —")
    fresh("en")
    ok("{commands} подставляется всегда", "/stop" in S.t("welcome"))
    ok("именованный параметр", "opus" in S.t("model_set", model="opus"))
    ok("ошибка попадает в текст", "boom" in S.t("failed", error="boom"))
    ok("неизвестный ключ возвращает сам ключ", S.t("no-such-key") == "no-such-key")
    ok("нехватка параметра не роняет", S.t("model_set") == S.EN["model_set"])


def main():
    fresh("en")
    orig_dir = S.LOCALE_DIR
    try:
        test_builtin()
        test_locale_normalisation()
        test_unknown_language_falls_back()
        test_locale_file()
        test_broken_locale_file()
        test_substitution()
    finally:
        S.LOCALE_DIR = orig_dir
        fresh("en")
    print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
    if FAIL:
        print("Провалены:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
