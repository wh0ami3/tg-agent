"""Руки: мышь, клавиатура, скриншоты.

Раньше руки были только внешней командой (`jarvis-computer` из соседнего
проекта автора) — из-за этого репозиторий не работал ни у кого. Теперь
реализация едет в комплекте как консольная команда `tg-agent-hands`,
и её же по умолчанию вызывает модель.

Внешний CLI никуда не делся: TGAGENT_HANDS_CMD по-прежнему переключает на
любую реализацию контракта — она нужна там, где pyautogui бессилен
(в первую очередь Wayland, где ввод идёт через портал RemoteDesktop).

Кого чем обслуживаем:

  Windows        pyautogui       из коробки
  macOS          pyautogui       нужны разрешения Accessibility + Screen Recording
  Linux X11      pyautogui       нужен python-xlib
  Linux Wayland  внешний CLI     pyautogui не умеет, см. README
"""

from __future__ import annotations

from .backend import Hands, HandsUnavailable, current_platform

__all__ = ["Hands", "HandsUnavailable", "current_platform"]
