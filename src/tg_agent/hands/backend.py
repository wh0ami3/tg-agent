"""Реализация рук поверх pyautogui.

Движения человеческие намеренно, и не ради красоты: резкие телепорты курсора
и мгновенная печать — то, по чему часть сайтов отличает автоматизацию от
человека. Плюс интерфейсы, рассчитанные на живого пользователя (меню с
задержкой раскрытия, drag-подсказки), на телепорт реагируют иначе.

pyautogui импортируется ЛЕНИВО: на голой установке без extra «hands» пакет
должен спокойно импортироваться, а внятная ошибка появиться только когда
руками реально попробовали воспользоваться.
"""

from __future__ import annotations

import os
import platform
import random
import sys
import time
from pathlib import Path

# Длиннее — вставляем из буфера: посимвольная печать полотна занимает минуты
# и ломается о автодополнение в полях ввода.
PASTE_THRESHOLD = 120


class HandsUnavailable(RuntimeError):
    """Руки на этой машине не поднять — с объяснением, что делать."""


def current_platform() -> str:
    """windows | macos | linux-x11 | linux-wayland | unknown"""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            return "linux-wayland"
        if os.environ.get("DISPLAY"):
            return "linux-x11"
        return "unknown"
    return "unknown"


def _explain_unavailable(plat: str, err: Exception | None = None) -> str:
    if plat == "linux-wayland":
        return (
            "Wayland: pyautogui здесь не работает — композитор не отдаёт "
            "синтетический ввод приложениям. Нужен внешний CLI через портал "
            "RemoteDesktop; укажите его в TGAGENT_HANDS_CMD (см. README)."
        )
    if plat == "macos":
        return (
            "macOS: нет разрешений. Системные настройки → Конфиденциальность "
            "и безопасность → добавьте терминал (или Python) в «Универсальный "
            f"доступ» И в «Запись экрана», затем перезапустите агента. ({err})"
        )
    if plat == "unknown":
        return (
            "Не определился графический сеанс (нет DISPLAY/WAYLAND_DISPLAY). "
            "На сервере без экрана руки работать не могут."
        )
    return (
        "pyautogui недоступен. Поставьте extra «hands»: `uv sync --extra hands` "
        f"(на Linux/X11 нужен ещё системный пакет python-xlib). ({err})"
    )


def _load_pyautogui():
    plat = current_platform()
    if plat == "linux-wayland":
        raise HandsUnavailable(_explain_unavailable(plat))
    try:
        import pyautogui  # noqa: PLC0415 — лениво, см. докстринг модуля
    except Exception as e:  # noqa: BLE001 — на macOS без разрешений это не ImportError
        raise HandsUnavailable(_explain_unavailable(plat, e)) from e
    # Без этого случайный увод курсора в угол экрана роняет процесс
    # FailSafeException'ом посреди задачи.
    pyautogui.FAILSAFE = False
    # Свои паузы, дефолтная только мешает.
    pyautogui.PAUSE = 0
    return pyautogui


class Hands:
    """Мышь, клавиатура, экран. Все методы синхронные и быстрые."""

    def __init__(self) -> None:
        self._gui = _load_pyautogui()

    # ------------------------------------------------------------------ мышь
    def move(self, x: int, y: int) -> None:
        """Плавный проезд: длительность растёт с расстоянием, но с потолком,
        иначе проход через весь экран занимал бы секунды."""
        gui = self._gui
        try:
            x0, y0 = gui.position()
        except Exception:  # noqa: BLE001 — на некоторых стойках позиция недоступна
            x0, y0 = x, y
        distance = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
        duration = min(0.45, 0.12 + distance / 4000)
        gui.moveTo(x, y, duration=duration, tween=gui.easeOutQuad)

    def click(self, x: int, y: int, button: str = "left") -> None:
        gui = self._gui
        self.move(x, y)
        # микропауза после подъезда: интерфейсы с hover-состоянием не успевают
        # перерисоваться, и клик уходит по ещё не появившемуся элементу
        time.sleep(random.uniform(0.04, 0.09))
        if button == "double":
            gui.doubleClick()
        else:
            gui.click(button=button)

    def scroll(self, amount: int) -> None:
        self._gui.scroll(amount)

    # -------------------------------------------------------------- клавиши
    def key(self, combo: str) -> None:
        """`ctrl+alt+t`, `cmd+space`, `enter`. Одиночная клавиша — тоже сюда."""
        parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
        if not parts:
            raise ValueError("пустое сочетание клавиш")
        # На macOS ctrl из чужих инструкций почти всегда означает cmd
        if current_platform() == "macos":
            parts = ["command" if p in ("cmd", "super", "meta") else p for p in parts]
        else:
            parts = ["win" if p in ("cmd", "super", "meta") else p for p in parts]
        if len(parts) == 1:
            self._gui.press(parts[0])
        else:
            self._gui.hotkey(*parts)

    def type_text(self, text: str) -> str:
        """Печать. Длинное — через буфер обмена.

        Возвращает 'typed' или 'pasted': модель по этому слову понимает,
        сработала ли вставка, и надо ли перепроверить поле скриншотом.
        """
        if len(text) > PASTE_THRESHOLD and self._paste(text):
            return "pasted"
        for ch in text:
            self._gui.write(ch)
            # живой ритм: пробел — заметная пауза, остальное дрожит
            time.sleep(random.uniform(0.055, 0.11) if ch == " " else random.uniform(0.018, 0.055))
        return "typed"

    def _paste(self, text: str) -> bool:
        try:
            import pyperclip  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — буфера нет, откатимся на посимвольную
            return False
        try:
            pyperclip.copy(text)
        except Exception:  # noqa: BLE001 — на Linux без xclip/xsel копирование падает
            return False
        time.sleep(0.05)
        self.key("command+v" if current_platform() == "macos" else "ctrl+v")
        time.sleep(0.12)
        return True

    # ---------------------------------------------------------------- экран
    def screenshot(self, path: str | Path | None = None) -> Path:
        """Снимок с ОТРИСОВАННЫМ курсором.

        pyautogui курсор не захватывает, а модели он нужен: без него не видно,
        куда наведено, и после клика невозможно понять, промазал или нет.
        """
        gui = self._gui
        img = gui.screenshot()
        try:
            self._draw_cursor(img, *gui.position())
        except Exception:  # noqa: BLE001 — не нарисовали курсор, снимок всё равно нужен
            pass
        out = Path(path) if path else Path(_default_shot_path())
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return out

    @staticmethod
    def _draw_cursor(img, x: int, y: int) -> None:
        from PIL import ImageDraw  # noqa: PLC0415 — тянется вместе с pyautogui

        d = ImageDraw.Draw(img)
        # Белая обводка под чёрной стрелкой: читается и на тёмном, и на светлом
        arrow = [(x, y), (x, y + 17), (x + 4, y + 13), (x + 7, y + 20),
                 (x + 10, y + 19), (x + 7, y + 12), (x + 12, y + 12)]
        d.line([*arrow, arrow[0]], fill=(255, 255, 255), width=5, joint="curve")
        d.polygon(arrow, fill=(20, 20, 20), outline=(255, 255, 255))


def _default_shot_path() -> str:
    from ..paths import STATE_DIR  # noqa: PLC0415 — циклический импорт на уровне модуля

    return str(STATE_DIR / "shots" / f"shot-{int(time.time() * 1000)}.png")


def selftest() -> int:
    """Быстрая проверка «руки поднимутся здесь или нет» без единого клика."""
    plat = current_platform()
    print(f"платформа: {plat}")
    try:
        hands = Hands()
    except HandsUnavailable as e:
        print(f"руки НЕ работают: {e}")
        return 1
    print(f"экран: {hands._gui.size()}")
    print(f"курсор: {hands._gui.position()}")
    print("руки работают")
    return 0


if __name__ == "__main__":
    sys.exit(selftest())
