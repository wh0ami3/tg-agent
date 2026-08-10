"""Мозг агента — claude CLI (headless) с «руками»: внешним CLI, который
двигает мышь, печатает и снимает экран (см. TGAGENT_HANDS_CMD).

Диалоговая память: --continue в своей папке состояния, отдельной от любых
других сессий claude на машине.

Запуск CLI — stream-json: по ходу работы видны tool_use-события (лента
«что агент сейчас делает»), финальный текст — из события result.
Таймаут — киллер-таймер с гашением ВСЕЙ группы процессов: агентный CLI
плодит подпроцессы, обычный kill оставил бы их действовать на компьютере.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .paths import CLAUDE_WORKDIR

WORKDIR = CLAUDE_WORKDIR

# Папка с CLI «рук». Пусто — команда ищется в обычном PATH, что и нужно
# большинству. Переменная нужна, когда CLI лежит в venv или вне PATH
# (например, в юните systemd с урезанным окружением).
HANDS_BIN = Path(os.environ.get("TGAGENT_HANDS_BIN", "").strip() or ".")

TIMEOUT = int(os.environ.get("TGAGENT_TIMEOUT", "900") or "900")

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="brain")

# живые группы процессов CLI: агент обязан забрать их с собой при выходе,
# иначе осиротевший claude продолжит действовать на компьютере без пульта
_LIVE: set[int] = set()
_LIVE_LOCK = threading.Lock()

# Язык отчётов агента. Пусто — отвечает на языке задачи (владелец пишет
# по-русски → отчёт по-русски, по-английски → по-английски). Любая строка
# сюда: "English", "Deutsch", "日本語" — модель говорит на всех, переводить
# руками не нужно.
REPLY_LANG = os.environ.get("TGAGENT_REPLY_LANG", "").strip()

# Имя CLI «рук» подставляется: форк с другим бинарём не переписывает промпт.
HANDS_CMD = os.environ.get("TGAGENT_HANDS_CMD", "jarvis-computer").strip() or "jarvis-computer"


def system_style(hands: str = "", reply_lang: str = "") -> str:
    """Системный промпт агента. Английский — чтобы форк на любом языке читался
    и правился; язык ОТЧЁТОВ задаётся отдельно и может быть любым."""
    cmd = hands or HANDS_CMD
    lang = reply_lang or REPLY_LANG
    speak = (
        f"Reply in {lang}."
        if lang
        else "Reply in the same language the task was written in."
    )
    return (
        "You are a desktop agent. You operate the owner's computer "
        "(Arch Linux, KDE Wayland) from Telegram. Carry out actions YOURSELF "
        f"via Bash and the `{cmd}` CLI (it is on PATH): "
        f"`{cmd} clickon \"what to click, in words\"` — DEFAULT way to click. "
        "A local targeting model finds the element on screen and clicks it. It "
        "already knows where things are, so you do NOT need a screenshot before "
        "it and you do NOT need one after it to confirm — a zero exit code "
        "means it found the element and clicked. Chain these freely. "
        f"`{cmd} screenshot` — screenshot WITH the cursor (prints the png path); "
        f"`{cmd} find \"description\"` — prints coordinates only; "
        f"`{cmd} click X Y [left|right|middle|double]`, `move X Y`, "
        f"`type \"text\"`, `key \"ctrl+alt+t\"`, `scroll 120` (negative scrolls up). "
        "Mouse motion and typing are already human-like (smooth glide, natural "
        "rhythm) — do not emulate that yourself and do not rush it. "
        "SPEED MATTERS. Do not screenshot after every action — that is slow and "
        "usually pointless. Take a screenshot only when you actually need to "
        "see something: at the start if you don't know what is on screen, when "
        f"`{cmd} clickon` reports not found (exit code 3), when a step clearly "
        "did not work, or at the end when the answer itself is on the screen. "
        "Prefer keyboard over mouse where it is faster — `key \"ctrl+l\"` to "
        "reach a browser address bar beats hunting for it visually. "
        "Only when you fall back to raw coordinates: screenshot afterwards and "
        "check you hit it. If not, do NOT hammer the same spot — re-read the "
        "coordinates (aim at the CENTRE of the element) and look for overlays "
        "or banners on top, closing those first. "
        "Long text (>120 chars) is pasted from the clipboard by `type` "
        "(reported as \"pasted\"); pasting works in GUI fields, but do not use "
        "`type` for long input into a terminal or vim, and verify with a "
        "screenshot after pasting. "
        "Ordinary tasks (launching programs, files, network) — plain Bash. "
        f"{speak} Be brief, no markdown formatting; when done, report the "
        "result and how you verified it. "
        "To send an image to the chat (a screenshot, say), add this as its OWN "
        "line in your reply: [PHOTO:/absolute/path.png] — the bot will send the "
        "file and strip the line from the text. "
        "You have full access to the computer and the owner consented in "
        "advance: act immediately, without asking permission. The single "
        "exception: if an action is plainly catastrophic and irreversible "
        "(mass deletion, wiping the system) and almost certainly not what was "
        "meant — ask once, in one sentence."
    )


# Совместимость: модуль-уровневая константа для тех, кто импортирует её напрямую
SYSTEM_STYLE = system_style()


class Aborted(RuntimeError):
    """Прогон CLI остановлен командой хозяина (/stop или /reset)."""


def claude_path() -> str | None:
    """claude может не быть в PATH systemd-сессии — ищем сами (как в мосте)."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".claude" / "local" / "claude",
    ):
        if candidate.exists():
            return str(candidate)

    # npm-глобал живёт под nvm; свежайшая нода по номерам версии, не по алфавиту
    def _ver(p: Path) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", p.parts[-3]))

    nvm = sorted(Path.home().glob(".nvm/versions/node/*/bin/claude"), key=_ver, reverse=True)
    if nvm:
        return str(nvm[0])
    return None


def _session_env() -> dict[str, str]:
    """Свежий env графической сессии из systemd --user.

    Юнит агента мог стартовать ДО того, как Плазма импортировала
    WAYLAND_DISPLAY/DISPLAY в user-менеджер (ребут), или пережить релогин
    (wayland-0 → wayland-1) — снятый на старте os.environ протухает,
    поэтому перечитываем окружение на каждый запуск рук."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — нет systemd (ручной запуск) — не беда
        return {}
    if out.returncode != 0:
        return {}
    env: dict[str, str] = {}
    for line in out.stdout.splitlines():
        k, eq, v = line.partition("=")
        # show-environment экранирует спецсимволы как $'…' — такие значения
        # не парсим (в нужных нам DISPLAY/WAYLAND/DBUS их не бывает)
        if eq and k and not v.startswith("$'"):
            env[k] = v
    return env


def child_env() -> dict[str, str]:
    """env для claude и CLI рук: поверх os.environ — свежий env сессии,
    PATH дополняется папкой рук (если задана) и папкой claude (рядом
    лежит node для шебанга)."""
    env = dict(os.environ)
    env.update(_session_env())
    extra: list[str] = []
    if str(HANDS_BIN) != "." and HANDS_BIN.is_dir():
        extra.append(str(HANDS_BIN))
    if (c := claude_path()) is not None:
        extra.append(str(Path(c).parent))
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


def _kill_tree(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:  # группы уже нет — мешать это не должно
        pass


def kill_live_clis() -> None:
    with _LIVE_LOCK:
        pids = list(_LIVE)
    for pid in pids:
        _kill_tree(pid)


def shutdown() -> None:
    """Уборка при остановке агента: убить живые CLI, отпустить пул потоков.
    wait=False обязателен — иначе join держит процесс до конца самого
    долгого CLI."""
    kill_live_clis()
    _POOL.shutdown(wait=False, cancel_futures=True)


def _tool_detail(tool_input: dict) -> str:
    """Человеко-читаемая строка действия из input'а инструмента."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "pattern", "url", "prompt", "description"):
        if val := tool_input.get(key):
            return str(val)
    return ""


class Brain:
    """claude -p с --continue: один диалог на агента, /reset двигает поколение.

    Маркер .continue в WORKDIR переживает рестарт агента: multi-шаговая
    задача после systemctl restart продолжает ТУ ЖЕ сессию claude,
    а не начинает беседу с амнезией."""

    def __init__(self) -> None:
        self._gen = 0
        self._lock = asyncio.Lock()
        # поколение стопа: /stop ЛАТЧИТСЯ (abort += 1), а не флаг-Event —
        # флаг стирался бы clear'ом следующего прогона, и задача из очереди
        # стартовала бы сразу после подтверждённой остановки
        self._stop_gen = 0
        self._busy = 0  # ask'и в полёте/очереди (для честного ответа /stop)
        WORKDIR.mkdir(parents=True, exist_ok=True)
        # диалог уже был до рестарта — продолжаем его
        self._asked_gen = self._gen if self._marker().exists() else -1

    @staticmethod
    def _marker() -> Path:
        return WORKDIR / ".continue"

    @property
    def stop_gen(self) -> int:
        """Снимок поколения стопа: берётся при СТАРТЕ задачи (у голосовых —
        до скачивания и STT), чтобы /stop, нажатый во время подготовки,
        отменил и её, а не только уже живой CLI."""
        return self._stop_gen

    def reset(self) -> None:
        self._gen += 1
        self._marker().unlink(missing_ok=True)

    def abort(self) -> bool:
        """/stop: поднять поколение стопа и убить живой CLI.

        Поколение растёт ВСЕГДА — ask'и, начатые до этого момента (включая
        ждущих в очереди на _lock и ещё не дошедших до Popen), увидят сдвиг
        и лягут Aborted; задачи, присланные ПОСЛЕ /stop, не затрагиваются.
        True — что-то реально работало (жило или стояло в очереди)."""
        self._stop_gen += 1
        with _LIVE_LOCK:
            pids = list(_LIVE)
        for pid in pids:
            _kill_tree(pid)
        return bool(pids) or self._busy > 0

    @staticmethod
    def available() -> bool:
        return claude_path() is not None

    def _cmd(self, text: str, model: str, continue_session: bool) -> list[str]:
        cmd = [
            claude_path() or "claude", "-p", text,
            # собираем на каждый запуск: язык отчётов можно поменять в env
            # без перезапуска агента
            "--append-system-prompt", system_style(),
            # headless: спросить разрешение не у кого; полный доступ —
            # осознанный выбор Джесси для своего агента на своей машине
            "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose",
        ]
        if model:
            cmd += ["--model", model]
        if continue_session:
            cmd.append("--continue")
        return cmd

    def _run_sync(self, text: str, model: str, continue_session: bool, emit,
                  stop_baseline: int | None = None) -> str:
        if stop_baseline is None:
            stop_baseline = self._stop_gen
        # /stop мог прилететь, пока мы стояли в очереди — не запускаем CLI
        if self._stop_gen != stop_baseline:
            raise Aborted("остановлено командой хозяина")
        proc = subprocess.Popen(
            self._cmd(text, model, continue_session),
            cwd=WORKDIR,
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with _LIVE_LOCK:
            _LIVE.add(proc.pid)
        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            _kill_tree(proc.pid)

        killer = threading.Timer(TIMEOUT, _on_timeout)
        killer.start()
        # stderr — отдельным потоком: полный пайп заблокировал бы CLI
        err_parts: list[str] = []
        t_err = threading.Thread(target=lambda: err_parts.append(proc.stderr.read()), daemon=True)
        t_err.start()
        final: str | None = None
        err_text = ""  # текст из result c is_error: «лимит подписки» и т.п.
        last_text = ""  # текст последнего assistant-хода — резерв, если result не пришёл
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if j.get("type") == "assistant":
                    parts: list[str] = []
                    for block in (j.get("message") or {}).get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            emit(str(block.get("name") or "?"), _tool_detail(block.get("input") or {}))
                        elif block.get("type") == "text" and block.get("text"):
                            parts.append(block["text"])
                    if parts:
                        last_text = "\n".join(parts)
                elif j.get("type") == "result":
                    if j.get("is_error"):
                        # текст ошибки CLI живёт ТОЛЬКО здесь (stderr при
                        # API-ошибках пуст): лимит подписки, нет модели…
                        err_text = str(j.get("result") or "").strip()
                    else:
                        final = j.get("result")
            proc.wait()
            t_err.join(5)
            if self._stop_gen != stop_baseline:
                raise Aborted("остановлено командой хозяина")
            if timed_out.is_set():
                raise RuntimeError(f"мозг превысил таймаут {TIMEOUT} с — задачу пришлось прервать")
            if err_text and final is None:
                raise RuntimeError(err_text[:500])
            if proc.returncode != 0:
                err = (err_parts[0] if err_parts else "").strip()
                raise RuntimeError(err[-500:] or "claude -p завершился с ошибкой")
        finally:
            killer.cancel()
            with _LIVE_LOCK:
                _LIVE.discard(proc.pid)
        return (final if final is not None else last_text).strip()

    async def ask(self, text: str, model: str, on_tool=None,
                  stop_gen: int | None = None) -> str:
        """Задача мозгу; on_tool(tool, detail) прилетает в event loop
        (переброс из потока парсинга — call_soon_threadsafe). stop_gen —
        снимок поколения стопа на момент СТАРТА задачи (см. stop_gen)."""
        baseline = self._stop_gen if stop_gen is None else stop_gen
        self._busy += 1
        try:
            async with self._lock:
                if self._stop_gen != baseline:
                    # /stop пришёл, пока задача стояла в очереди/готовилась
                    raise Aborted("остановлено командой хозяина")
                gen = self._gen
                loop = asyncio.get_running_loop()

                def emit(tool: str, detail: str) -> None:
                    if on_tool is not None:
                        loop.call_soon_threadsafe(on_tool, tool, detail)

                reply = await loop.run_in_executor(
                    _POOL, self._run_sync, text, model, self._asked_gen == gen, emit, baseline
                )
                self._asked_gen = gen
                # сессия claude состоялась — после рестарта продолжим её же
                with contextlib.suppress(OSError):
                    self._marker().touch()
                return reply
        finally:
            self._busy -= 1


async def take_screenshot() -> str:
    """Скриншот через CLI рук (с курсором); команда печатает путь к файлу."""
    exe = str(HANDS_BIN / HANDS_CMD)
    if not Path(exe).exists():
        exe = HANDS_CMD
    proc = await asyncio.create_subprocess_exec(
        exe, "screenshot",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError:
        proc.kill()
        raise RuntimeError("скриншот не успел за 30 с")
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip()[-200:] or f"код {proc.returncode}")
    lines = out.decode(errors="replace").strip().splitlines()
    path = lines[-1].strip() if lines else ""
    if not path or not Path(path).is_file():
        raise RuntimeError(f"{HANDS_CMD} не вернул путь к снимку")
    return path
