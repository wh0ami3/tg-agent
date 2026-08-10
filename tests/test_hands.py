"""Юниты рук: определение платформы, контракт CLI, коды возврата, ритм печати.

Всё офлайн и на любой машине: pyautogui подменяется фейком, поэтому тесты
одинаково проходят на Linux, Windows и macOS и ничего не кликают по-настоящему.

Запуск: uv run python tests/test_hands.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))


# ------------------------------------------------------------------ фейк GUI
class FakeImage:
    def __init__(self):
        self.saved = None

    def save(self, path):
        self.saved = Path(path)
        Path(path).write_bytes(b"PNG")


class FakeGui:
    """Минимальный pyautogui: пишет всё, что от него просили."""

    FAILSAFE = True
    PAUSE = 0.1

    def __init__(self):
        self.calls = []
        self.pos = (10, 10)
        self.image = FakeImage()
        self.easeOutQuad = "ease"

    def position(self):
        return self.pos

    def size(self):
        return (1920, 1080)

    def moveTo(self, x, y, duration=None, tween=None):
        self.calls.append(("moveTo", x, y, round(duration or 0, 3)))
        self.pos = (x, y)

    def click(self, button="left"):
        self.calls.append(("click", button))

    def doubleClick(self):
        self.calls.append(("doubleClick",))

    def scroll(self, amount):
        self.calls.append(("scroll", amount))

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", *keys))

    def write(self, ch):
        self.calls.append(("write", ch))

    def screenshot(self):
        return self.image


fake_gui = FakeGui()
sys.modules["pyautogui"] = fake_gui

fake_clip = types.ModuleType("pyperclip")
fake_clip.copied = []
fake_clip.copy = lambda t: fake_clip.copied.append(t)
sys.modules["pyperclip"] = fake_clip

# графический сеанс X11: иначе на машине сборки платформа определится как unknown
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ.setdefault("DISPLAY", ":0")

from tg_agent.hands import backend as B  # noqa: E402
from tg_agent.hands import cli as C  # noqa: E402

# рисование курсора требует настоящий PIL поверх настоящей картинки — не наша забота
B.Hands._draw_cursor = staticmethod(lambda img, x, y: None)


def reset():
    fake_gui.calls.clear()
    fake_clip.copied.clear()
    fake_gui.pos = (10, 10)


# -------------------------------------------------------------------- тесты
def test_platform():
    print("— определение платформы —")
    orig = B.platform.system
    try:
        B.platform.system = lambda: "Windows"
        ok("Windows", B.current_platform() == "windows")
        B.platform.system = lambda: "Darwin"
        ok("macOS", B.current_platform() == "macos")
        B.platform.system = lambda: "Linux"
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        ok("Linux + Wayland", B.current_platform() == "linux-wayland")
        del os.environ["WAYLAND_DISPLAY"]
        ok("Linux + X11", B.current_platform() == "linux-x11")
        d = os.environ.pop("DISPLAY")
        ok("без графического сеанса — unknown", B.current_platform() == "unknown")
        os.environ["DISPLAY"] = d
    finally:
        B.platform.system = orig


def test_wayland_refuses_early():
    print("— на Wayland руки честно отказывают —")
    orig = B.current_platform
    try:
        B.current_platform = lambda: "linux-wayland"
        try:
            B._load_pyautogui()
            ok("Wayland → HandsUnavailable", False)
        except B.HandsUnavailable as e:
            ok("Wayland → HandsUnavailable", True)
            ok("в тексте есть подсказка про TGAGENT_HANDS_CMD", "TGAGENT_HANDS_CMD" in str(e))
    finally:
        B.current_platform = orig


def test_move_and_click():
    print("— мышь —")
    reset()
    h = B.Hands()
    h.click(500, 400)
    kinds = [c[0] for c in fake_gui.calls]
    ok("перед кликом — подъезд", kinds[0] == "moveTo")
    ok("клик после подъезда", kinds[-1] == "click")

    move = fake_gui.calls[0]
    ok("подъезд не мгновенный", move[3] > 0, str(move))
    ok("длительность ограничена сверху", move[3] <= 0.45, str(move))

    reset()
    h.click(11, 11)
    near = fake_gui.calls[0][3]
    reset()
    h.click(1900, 1000)
    far = fake_gui.calls[0][3]
    ok("дальше — дольше", far > near, f"{near} → {far}")

    reset()
    h.click(300, 300, "double")
    ok("двойной клик — doubleClick", any(c[0] == "doubleClick" for c in fake_gui.calls))
    reset()
    h.click(300, 300, "right")
    ok("правая кнопка передана", ("click", "right") in fake_gui.calls)


def test_keys():
    print("— клавиши —")
    h = B.Hands()
    orig = B.current_platform
    try:
        B.current_platform = lambda: "linux-x11"
        reset()
        h.key("ctrl+alt+t")
        ok("сочетание — hotkey", ("hotkey", "ctrl", "alt", "t") in fake_gui.calls)
        reset()
        h.key("enter")
        ok("одиночная — press", ("press", "enter") in fake_gui.calls)
        reset()
        h.key("cmd+v")
        ok("cmd на Linux → win", ("hotkey", "win", "v") in fake_gui.calls)

        B.current_platform = lambda: "macos"
        reset()
        h.key("cmd+v")
        ok("cmd на macOS → command", ("hotkey", "command", "v") in fake_gui.calls)

        try:
            h.key("  ")
            ok("пустое сочетание — ValueError", False)
        except ValueError:
            ok("пустое сочетание — ValueError", True)
    finally:
        B.current_platform = orig


def test_typing():
    print("— печать —")
    h = B.Hands()
    reset()
    res = h.type_text("hi")
    ok("короткое печатается посимвольно", res == "typed"
       and [c for c in fake_gui.calls if c[0] == "write"] == [("write", "h"), ("write", "i")])

    reset()
    long_text = "x" * (B.PASTE_THRESHOLD + 1)
    res = h.type_text(long_text)
    ok("длинное уходит в буфер", res == "pasted" and fake_clip.copied == [long_text])
    ok("и вставляется сочетанием", any(c[0] == "hotkey" for c in fake_gui.calls))
    ok("посимвольно при этом НЕ печатается",
       not any(c[0] == "write" for c in fake_gui.calls))

    # буфер сломан (нет xclip) — откат на посимвольную, а не отказ
    reset()
    broken = lambda t: (_ for _ in ()).throw(RuntimeError("нет xclip"))
    orig_copy, fake_clip.copy = fake_clip.copy, broken
    try:
        res = h.type_text(long_text)
    finally:
        fake_clip.copy = orig_copy
    ok("сломанный буфер — откат на посимвольную", res == "typed"
       and any(c[0] == "write" for c in fake_gui.calls))


def test_screenshot():
    print("— скриншот —")
    h = B.Hands()
    tmp = Path(tempfile.mkdtemp(prefix="tg-agent-hands-")) / "sub" / "shot.png"
    out = h.screenshot(tmp)
    ok("файл сохранён по заданному пути", out == tmp and tmp.exists())
    ok("папка создана при необходимости", tmp.parent.is_dir())


def test_cli_contract():
    print("— контракт CLI —")
    ok("clickon без модели зрения → код 3", C.main(["clickon", "кнопка"]) == C.EXIT_NOT_FOUND)
    ok("find без модели зрения → код 3", C.main(["find", "поле"]) == C.EXIT_NOT_FOUND)

    reset()
    ok("click → 0", C.main(["click", "100", "200"]) == C.EXIT_OK)
    ok("кнопка по умолчанию левая", ("click", "left") in fake_gui.calls)
    reset()
    ok("scroll → 0", C.main(["scroll", "-120"]) == C.EXIT_OK)
    ok("отрицательная прокрутка проходит", ("scroll", -120) in fake_gui.calls)

    reset()
    ok("key с мусором → код 2", C.main(["key", "+++"]) == C.EXIT_ARGS)

    # руки не поднялись — отдельный код, чтобы агент отличал от «не найдено»
    orig = C.Hands
    try:
        def boom():
            raise B.HandsUnavailable("нет разрешений")
        C.Hands = boom
        ok("руки недоступны → код 4", C.main(["click", "1", "1"]) == C.EXIT_UNAVAILABLE)
    finally:
        C.Hands = orig


def test_cli_screenshot_prints_path_last():
    print("— путь к снимку последней строкой —")
    import io
    import contextlib

    tmp = Path(tempfile.mkdtemp(prefix="tg-agent-hands-cli-")) / "s.png"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = C.main(["screenshot", str(tmp)])
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    ok("код 0", code == C.EXIT_OK)
    ok("последняя строка — путь", lines and lines[-1].strip() == str(tmp), str(lines))
    ok("путь существует", tmp.exists())


def main():
    test_platform()
    test_wayland_refuses_early()
    test_move_and_click()
    test_keys()
    test_typing()
    test_screenshot()
    test_cli_contract()
    test_cli_screenshot_prints_path_last()
    print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
    if FAIL:
        print("Провалены:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
