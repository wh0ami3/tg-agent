"""Куда агент кладёт свои файлы.

Раньше всё жило в ~/.jarvis — папке соседнего проекта автора. Для чужой
машины это бессмыслица, поэтому теперь стандартные XDG-места:

  конфиг   $XDG_CONFIG_HOME/tg-agent   (обычно ~/.config/tg-agent)
  состояние $XDG_STATE_HOME/tg-agent   (обычно ~/.local/state/tg-agent)

Конфиг — то, что правит человек (токен, модель, язык). Состояние — то, что
пишет сам агент (маркеры, история диалога с мозгом): переживает перезапуск,
но потерять его не жалко.

Обе папки переопределяются: TGAGENT_CONFIG_DIR и TGAGENT_STATE_DIR.
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(env_var: str, default: str) -> Path:
    raw = os.environ.get(env_var, "").strip()
    return Path(raw) if raw else Path.home() / default


CONFIG_DIR = Path(
    os.environ.get("TGAGENT_CONFIG_DIR", "").strip()
    or _xdg("XDG_CONFIG_HOME", ".config") / "tg-agent"
)

STATE_DIR = Path(
    os.environ.get("TGAGENT_STATE_DIR", "").strip()
    or _xdg("XDG_STATE_HOME", ".local/state") / "tg-agent"
)

# Правит человек
CONFIG_FILE = CONFIG_DIR / "config.env"
LOCALE_DIR = CONFIG_DIR / "locales"

# Пишет агент
CLAUDE_WORKDIR = STATE_DIR / "claude"
INFLIGHT = STATE_DIR / "inflight"


def ensure() -> None:
    """Создать папки на старте.

    Без этого маркер «задача в полёте» на свежей установке молча не пишется
    (touch завёрнут в suppress(OSError)), и хозяин не узнаёт о задаче,
    прерванной жёстким рестартом.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
