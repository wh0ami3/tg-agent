"""Юниты мозга: сборка команды, stream-json парсинг, --continue/reset,
child_env, гашение по коду возврата. Всё офлайн: Popen замокан.

Запуск: uv run --project /home/jesse/Projects/tg-agent python tests/test_brain.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import tg_agent.brain as brain

os.environ["TGAGENT_BACKEND"] = "claude-cli"   # пин движка: забытый в env бэкенд не должен менять эти тесты

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))


brain.claude_path = lambda: "/fake/bin/claude"
brain.WORKDIR = Path(tempfile.mkdtemp(prefix="tg-agent-brain-test-"))  # маркер --continue не в реальном ~/.jarvis
_orig_session_env = brain._session_env
brain._session_env = lambda: {}  # герметичность: без реального systemctl


def line(obj):
    return json.dumps(obj) + "\n"


class FakeStderr:
    def __init__(self, text=""):
        self._text = text

    def read(self):
        return self._text


class FakePopen:
    """Отдаёт заготовленные stream-json строки; фиксирует команду."""

    calls: list[dict] = []

    def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None,
                 text=None, start_new_session=None):
        FakePopen.calls.append({"cmd": cmd, "cwd": cwd, "env": env})
        self.pid = 999_999_99
        self.returncode = FakePopen.next_returncode
        self.stdout = iter(FakePopen.next_stdout)
        self.stderr = FakeStderr(FakePopen.next_stderr)

    next_stdout: list = []
    next_stderr: str = ""
    next_returncode: int = 0

    def wait(self):
        return self.returncode


brain.subprocess.Popen = FakePopen
brain._kill_tree = lambda pid: None


def set_output(lines, returncode=0, stderr=""):
    FakePopen.next_stdout = lines
    FakePopen.next_returncode = returncode
    FakePopen.next_stderr = stderr
    FakePopen.calls.clear()


def test_cmd():
    print("— сборка команды —")
    b = brain.Brain()
    cmd = b._cmd("открой ютуб", "sonnet", False)
    ok("клод по найденному пути", cmd[0] == "/fake/bin/claude")
    ok("промпт на месте", cmd[1:3] == ["-p", "открой ютуб"])
    # имя рук берём из модуля, а не константой: дефолт менялся и ещё может
    ok("свой системный промпт", "--append-system-prompt" in cmd
       and brain.HANDS_CMD in cmd[cmd.index("--append-system-prompt") + 1])
    ok("полный доступ", "--dangerously-skip-permissions" in cmd)
    ok("stream-json + verbose", "--output-format" in cmd and "--verbose" in cmd)
    ok("модель пробрасывается", cmd[cmd.index("--model") + 1] == "sonnet")
    ok("без --continue на первом вопросе", "--continue" not in cmd)
    cmd2 = b._cmd("а теперь скриншот", "opus", True)
    ok("--continue на втором", "--continue" in cmd2)
    cmd3 = b._cmd("х", "", False)
    ok("пустая модель — без флага", "--model" not in cmd3)


def test_system_style():
    print("— системный промпт —")
    s = brain.SYSTEM_STYLE
    ok("clickon в приоритете", "clickon" in s)
    ok("подсказка «сверься, не долби»", "do NOT" in s and "CENTRE" in s)
    # скорость: агент не должен снимать экран после каждого действия —
    # локальный прицел clickon сам подтверждает попадание
    ok("clickon объявлен дефолтом", "DEFAULT way" in s)
    ok("явный запрет снимков на каждое действие",
       "not screenshot after every action" in s)
    ok("clickon не требует снимка до и после",
       "NOT need a screenshot before" in s and "NOT need one after" in s)
    ok("клавиатура предпочтительнее мыши, где быстрее", "ctrl+l" in s)
    ok("снимок только при откате на координаты", "raw coordinates" in s)
    ok("протокол [PHOTO:]", "[PHOTO:" in s)
    ok("вставка длинного текста", "pasted" in s)
    ok("ответ для Telegram", "Telegram" in s)


def test_stream_parse():
    print("— stream-json: события и финал —")
    b = brain.Brain()
    events = []
    set_output([
        line({"type": "system", "subtype": "init"}),
        line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Сейчас гляну."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "jarvis-computer screenshot"}},
        ]}}),
        "мусор не json\n",
        line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/s.png"}},
        ]}}),
        line({"type": "result", "is_error": False, "result": "Открыл ютуб, сэр."}),
    ])

    async def go():
        return await b.ask("открой ютуб", "sonnet", lambda t, d: events.append((t, d)))

    reply = asyncio.run(go())
    ok("финал из result", reply == "Открыл ютуб, сэр.")
    ok("tool_use события пришли", events == [
        ("Bash", "jarvis-computer screenshot"), ("Read", "/tmp/s.png"),
    ], str(events))
    ok("--continue не был (первый вопрос)", "--continue" not in FakePopen.calls[0]["cmd"])

    set_output([line({"type": "result", "is_error": False, "result": "Готово."})])
    reply2 = asyncio.run(b.ask("дальше", "sonnet"))
    ok("второй вопрос с --continue", "--continue" in FakePopen.calls[0]["cmd"])

    b.reset()
    set_output([line({"type": "result", "is_error": False, "result": "Готово."})])
    asyncio.run(b.ask("заново", "sonnet"))
    ok("после reset — без --continue", "--continue" not in FakePopen.calls[0]["cmd"])


def test_fallback_text():
    print("— нет result: берём текст последнего assistant —")
    b = brain.Brain()
    set_output([
        line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Первый ход."}]}}),
        line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Итог руками."}]}}),
    ])
    reply = asyncio.run(b.ask("x", ""))
    ok("текст последнего хода", reply == "Итог руками.")


def test_error():
    print("— ошибки CLI —")
    b = brain.Brain()
    set_output([], returncode=1, stderr="boom: не залогинен")

    async def go():
        try:
            await b.ask("x", "sonnet")
            return None
        except RuntimeError as e:
            return str(e)

    err = asyncio.run(go())
    ok("код ≠ 0 — RuntimeError со stderr", err is not None and "не залогинен" in err, str(err))

    # is_error=True: текст ошибки CLI живёт только в result — он и всплывает
    set_output([line({"type": "result", "is_error": True,
                      "result": "Claude AI usage limit reached|resets at 23:00"})])

    async def go2():
        try:
            await b.ask("x", "sonnet")
            return None
        except RuntimeError as e:
            return str(e)

    err2 = asyncio.run(go2())
    ok("is_error result всплывает текстом", err2 is not None and "usage limit" in err2, str(err2))

    # даже при коде ≠ 0 и пустом stderr причина берётся из result
    set_output([line({"type": "result", "is_error": True, "result": "нет такой модели"})],
               returncode=1, stderr="")
    err3 = asyncio.run(go2())
    ok("err_text приоритетнее безликого кода возврата",
       err3 is not None and "нет такой модели" in err3, str(err3))


def test_child_env():
    print("— child_env: PATH дополняется —")
    tmp = Path(tempfile.mkdtemp(prefix="tg-agent-venvbin-"))
    old = brain.HANDS_BIN
    brain.HANDS_BIN = tmp
    try:
        env = brain.child_env()
        parts = env["PATH"].split(":")
        ok("venv моста в PATH первым", parts[0] == str(tmp), parts[0])
        ok("папка claude в PATH (node для шебанга)", "/fake/bin" in parts)
        ok("исходный PATH сохранён", len(parts) > 2)
    finally:
        brain.HANDS_BIN = old


def test_tool_detail():
    print("— _tool_detail —")
    ok("command приоритетен", brain._tool_detail({"command": "ls", "file_path": "/x"}) == "ls")
    ok("не-dict — пусто", brain._tool_detail("x") == "")
    ok("пустой input — пусто", brain._tool_detail({}) == "")


def test_stop_gen_latch():
    print("— латч /stop: задача из очереди/подготовки не стартует —")
    b = brain.Brain()
    g = b.stop_gen
    b.abort()  # /stop до того, как задача добралась до мозга
    set_output([line({"type": "result", "is_error": False, "result": "ок"})])

    async def go():
        try:
            await b.ask("x", "", None, g)
            return False
        except brain.Aborted:
            return True

    ok("устаревший снимок стоп-поколения — Aborted", asyncio.run(go()))
    ok("CLI даже не запускался", FakePopen.calls == [], str(FakePopen.calls))

    set_output([line({"type": "result", "is_error": False, "result": "ок"})])
    ok("свежая задача после /stop живёт",
       asyncio.run(b.ask("y", "", None, b.stop_gen)) == "ок")


def test_abort():
    print("— /stop: abort глушит живой CLI и поднимает Aborted —")
    b = brain.Brain()
    ok("abort без живых — False", b.abort() is False)

    killed = []
    orig_kill = brain._kill_tree
    brain._kill_tree = lambda pid: killed.append(pid)
    set_output([
        line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "sleep 99"}}]}}),
    ])

    def emit(tool, detail):
        b.abort()  # хозяин нажал /stop посреди прогона

    try:
        try:
            b._run_sync("x", "", False, emit)
            ok("Aborted поднят", False)
        except brain.Aborted:
            ok("Aborted поднят", True)
    finally:
        brain._kill_tree = orig_kill
    ok("группа процессов убита", killed == [999_999_99], str(killed))


def test_continue_marker():
    print("— маркер --continue переживает рестарт —")
    b = brain.Brain()
    b.reset()  # чистый маркер
    set_output([line({"type": "result", "is_error": False, "result": "ок"})])
    asyncio.run(b.ask("первый", ""))
    ok("маркер поставлен после успешного ask", brain.Brain._marker().exists())

    b2 = brain.Brain()  # «рестарт агента»
    set_output([line({"type": "result", "is_error": False, "result": "ок"})])
    asyncio.run(b2.ask("после рестарта", ""))
    ok("новый Brain продолжает ту же сессию", "--continue" in FakePopen.calls[0]["cmd"])

    b2.reset()
    ok("reset снимает маркер", not brain.Brain._marker().exists())
    set_output([line({"type": "result", "is_error": False, "result": "ок"})])
    asyncio.run(b2.ask("заново", ""))
    ok("после reset — свежая сессия", "--continue" not in FakePopen.calls[0]["cmd"])


def test_session_env_parse():
    print("— _session_env: парсинг show-environment —")

    class FakeRun:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    orig_run = brain.subprocess.run
    brain.subprocess.run = lambda *a, **kw: FakeRun(
        0, "WAYLAND_DISPLAY=wayland-1\nDISPLAY=:0\nWEIRD=$'a\\nb'\nбезравно\n")
    try:
        env = _orig_session_env()
        ok("переменные сессии читаются", env.get("WAYLAND_DISPLAY") == "wayland-1"
           and env.get("DISPLAY") == ":0")
        ok("экранированные $'…' пропускаются", "WEIRD" not in env)
        ok("строка без = пропускается", "безравно" not in env)
        brain.subprocess.run = lambda *a, **kw: FakeRun(1, "")
        ok("сбой systemctl — пустой словарь", _orig_session_env() == {})
    finally:
        brain.subprocess.run = orig_run


test_cmd()
test_system_style()
test_stream_parse()
test_fallback_text()
test_error()
test_child_env()
test_tool_detail()
test_stop_gen_latch()
test_abort()
test_continue_marker()
test_session_env_parse()

print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
if FAIL:
    print("Провалены:", FAIL)
    sys.exit(1)
