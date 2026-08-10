"""`tg-agent-hands` — руки в комплекте, реализация контракта из README.

Это та самая команда, которую модель вызывает через Bash. Контракт держим
дословно: имена подкоманд, порядок аргументов и КОДЫ ВОЗВРАТА — часть
публичного интерфейса, на них завязан системный промпт.

Коды возврата:
  0  сделано
  2  неверные аргументы
  3  элемент не найден (только clickon/find)
  4  руки на этой машине не поднимаются (Wayland, нет разрешений, нет extra)
  1  всё прочее
"""

from __future__ import annotations

import argparse
import sys

from .backend import Hands, HandsUnavailable, current_platform, selftest

EXIT_OK = 0
EXIT_ARGS = 2
EXIT_NOT_FOUND = 3
EXIT_UNAVAILABLE = 4
EXIT_ERROR = 1


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tg-agent-hands",
        description="Мышь, клавиатура и экран для tg-agent. "
                    "Реализует контракт рук: см. README проекта.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screenshot", help="снимок экрана с курсором; печатает путь")
    s.add_argument("path", nargs="?", help="куда сохранить (по умолчанию — в папку состояния)")

    c = sub.add_parser("click", help="клик по координатам")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.add_argument("button", nargs="?", default="left",
                   choices=["left", "right", "middle", "double"])

    m = sub.add_parser("move", help="передвинуть курсор")
    m.add_argument("x", type=int)
    m.add_argument("y", type=int)

    t = sub.add_parser("type", help="напечатать текст (длинный — из буфера)")
    t.add_argument("text")

    k = sub.add_parser("key", help='нажать сочетание, напр. "ctrl+alt+t"')
    k.add_argument("combo")

    sc = sub.add_parser("scroll", help="прокрутка (отрицательное — вверх)")
    sc.add_argument("amount", type=int)

    # Заявлены в контракте, но требуют модели зрения — её в комплекте нет.
    # Отвечаем «не найдено» (код 3), и агент по промпту сам переходит на
    # координаты. Форк с прицелом просто подменяет команду целиком.
    for name, help_ in (("clickon", "найти элемент по описанию и кликнуть"),
                        ("find", "найти элемент по описанию")):
        v = sub.add_parser(name, help=f"{help_} (нужна модель зрения — не входит в комплект)")
        v.add_argument("description")

    sub.add_parser("selftest", help="проверить, поднимаются ли руки на этой машине")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    if args.cmd in ("clickon", "find"):
        print(
            f"{args.cmd}: нужна модель зрения, в комплект она не входит — "
            "целься по координатам со скриншота "
            "(или подставь свою реализацию через TGAGENT_HANDS_CMD)",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND

    try:
        hands = Hands()
    except HandsUnavailable as e:
        print(f"руки недоступны на «{current_platform()}»: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    try:
        if args.cmd == "screenshot":
            # путь — ПОСЛЕДНЕЙ строкой: так его читает вызывающая сторона
            print(hands.screenshot(args.path))
        elif args.cmd == "click":
            hands.click(args.x, args.y, args.button)
        elif args.cmd == "move":
            hands.move(args.x, args.y)
        elif args.cmd == "type":
            print(hands.type_text(args.text))
        elif args.cmd == "key":
            hands.key(args.combo)
        elif args.cmd == "scroll":
            hands.scroll(args.amount)
        else:  # pragma: no cover — argparse не пропустит
            return EXIT_ARGS
    except ValueError as e:
        print(f"{args.cmd}: {e}", file=sys.stderr)
        return EXIT_ARGS
    except Exception as e:  # noqa: BLE001 — код возврата важнее трейсбека
        print(f"{args.cmd}: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
