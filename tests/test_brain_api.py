"""Юниты мозга на HTTP: инструменты, агентный цикл, история диалога.

Всё офлайн: провод к модели фейковый, сеть не трогается. Часть тестов
инструментов намеренно гоняет НАСТОЯЩИЙ bash — именно там жили две поломки,
которые нашли скептики (фоновой процесс держал шаг до таймаута, а setsid
переживал /stop), и подменённый Popen их бы не поймал.

Запуск: uv run python tests/test_brain_api.py
"""
import copy
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["TGAGENT_BACKEND"] = "claude-cli"   # фабрику здесь не трогаем

import tg_agent.brain as brain
import tg_agent.brain_api as ba

brain.WORKDIR = Path(tempfile.mkdtemp(prefix="tg-agent-api-test-"))
brain._session_env = lambda: {}       # герметичность: без реального systemctl

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))


# ─────────────────────────────── фейковый провод ─────────────────────────────
class FakeBrain(ba.ApiBrain):
    """ApiBrain с заготовленными ходами вместо HTTP.

    deepcopy тела обязателен: цикл дописывает в тот же список, и без копии
    тест «видит будущее» — на этом уже наступали.
    """

    NAME = API = "fake"
    DEF_MODEL = "fake-model"

    def __init__(self, turns=None):
        super().__init__()
        self.turns = list(turns or [])
        self.sent = []

    def _post(self, body, deadline, baseline):
        self._check(baseline)
        self.sent.append(copy.deepcopy(body))
        assert self.turns, "цикл запросил ход, которого тест не готовил"
        return self.turns.pop(0)

    # провод — как у Anthropic, он строже
    def _url(self): return "http://fake/v1/messages"
    def _headers(self): return {}
    def _user(self, text): return {"role": "user", "content": text}

    def _body(self, model, system, msgs):
        return {"model": model, "system": system, "messages": msgs}

    _take = ba.AnthropicBrain._take
    _answer = ba.AnthropicBrain._answer
    _repair_tail = ba.AnthropicBrain._repair_tail


def turn(text="", calls=(), stop=None):
    """Ход модели в формате Anthropic."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for i, (name, args) in enumerate(calls):
        content.append({"type": "tool_use", "id": f"tu_{i}", "name": name, "input": args})
    return {"content": content, "stop_reason": stop or ("tool_use" if calls else "end_turn")}


def run(b, text="сделай", cont=False, events=None):
    return b._run_sync(text, "fake-model", cont,
                       lambda t, d: (events if events is not None else []).append((t, d)))


# ──────────────────────────────── инструменты ────────────────────────────────
def test_bash_basic():
    print("— Bash: базовое —")
    b = FakeBrain()
    env = brain.child_env()
    dl = time.monotonic() + 60

    out = b._bash("echo привет", env, dl, b._stop_gen)
    ok("вывод возвращается", out == "привет", out)

    # -c, а не -lc: в неинтерактивном не-логин шелле в $- нет ни i, ни l
    out = b._bash("echo $-", env, dl, b._stop_gen)
    ok("bash -c, а не -lc", "i" not in out and "l" not in out, out)

    out = b._bash("exit 3", env, dl, b._stop_gen)
    ok("код возврата в ответе", out.startswith("exit=3"), out)

    out = b._bash("true", env, dl, b._stop_gen)
    ok("пустой вывод не пустой строкой", out == "выполнено, код 0, вывод пуст", out)

    out = b._bash("", env, dl, b._stop_gen)
    ok("пустая команда — отказ без запуска", "непустой" in out, out)

    big = b._bash(f"python3 -c \"print('x'*{ba.MAX_OUT * 2})\"", env, dl, b._stop_gen)
    ok("длинный вывод обрезан серединой",
       len(big) < ba.MAX_OUT + 200 and "[вырезано]" in big, str(len(big)))


def test_bash_background():
    """Слом №1 скептиков: communicate() ждал EOF на пайпе, а не смерти
    процесса — фоновой процесс держал шаг до TOOL_TIMEOUT."""
    print("— Bash: фоновой процесс не держит шаг —")
    b = FakeBrain()
    env = brain.child_env()
    t0 = time.monotonic()
    out = b._bash("sleep 20 & echo launched", env, time.monotonic() + 60, b._stop_gen)
    dt = time.monotonic() - t0
    ok("вернулись мгновенно, не дожидаясь фонового", dt < 3, f"{dt:.1f} с")
    ok("вывод получен", "launched" in out, out)


def test_bash_setsid_abort():
    """Слом №1, вторая половина: ушедший в свою сессию процесс переживал
    killpg, и /stop врал хозяину, вися до таймаута."""
    print("— Bash: /stop не ждёт сбежавший процесс —")
    b = FakeBrain()
    env = brain.child_env()
    base = b._stop_gen

    def stopper():
        time.sleep(0.4)
        b.abort()

    threading.Thread(target=stopper, daemon=True).start()
    t0 = time.monotonic()
    try:
        b._bash("setsid sleep 25; echo x", env, time.monotonic() + 60, base)
        ok("прервано по /stop", False, "команда досчиталась")
    except brain.Aborted:
        dt = time.monotonic() - t0
        ok("прервано по /stop", True)
        ok("быстро, а не через TOOL_TIMEOUT", dt < 4, f"{dt:.1f} с")


def test_bash_handshake():
    """Гонка: /stop прилетел ровно между Popen и регистрацией pid."""
    print("— Bash: рукопожатие со /stop —")
    b = FakeBrain()
    env = brain.child_env()
    base = b._stop_gen
    mark = Path(tempfile.mkdtemp(prefix="tg-hs-")) / "PWNED"
    b._stop_gen = base + 1      # сдвиг ДО запуска
    try:
        b._bash(f"touch {mark}", env, time.monotonic() + 30, base)
        ok("устаревшее поколение — Aborted", False, "команда исполнилась")
    except brain.Aborted:
        ok("устаревшее поколение — Aborted", True)
    time.sleep(0.2)
    ok("побочный эффект не состоялся", not mark.exists())


def test_bash_timeout():
    print("— Bash: таймаут команды —")
    b = FakeBrain()
    old, ba.TOOL_TIMEOUT = ba.TOOL_TIMEOUT, 1
    try:
        out = b._bash("sleep 8", brain.child_env(), time.monotonic() + 60, b._stop_gen)
    finally:
        ba.TOOL_TIMEOUT = old
    ok("маркер таймаута в ответе", "таймаут" in out, out)
    ok("процесс прибран, а не зомби", "exit=None" not in out, out)


def test_read():
    print("— Read —")
    b = FakeBrain()
    tmp = Path(tempfile.mkdtemp(prefix="tg-read-"))
    ok("нет файла", "нет файла" in b._read(str(tmp / "нет.png")))
    ok("пустой путь — отказ", "требует file_path" in b._read(""))

    txt = tmp / "a.txt"; txt.write_text("привет")
    ok("не картинка — отсылка к Bash", "через Bash" in b._read(str(txt)))

    png = tmp / "s.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    got = b._read(str(png))
    ok("png отдаётся картинкой", isinstance(got, dict) and got["media_type"] == "image/png")
    ok("и содержит base64", isinstance(got, dict) and got.get("b64"))

    big = tmp / "big.png"; big.write_bytes(b"0" * (ba.MAX_IMAGE + 10))
    ok("слишком большой снимок — отказ", "не буду" in b._read(str(big)))


def test_whitelist():
    """Слом №2, пробой А4: галлюцинированный read_file с командой в аргументе
    исполнялся как shell — это канал инъекции через экран."""
    print("— белый список инструментов —")
    b = FakeBrain()
    mark = Path(tempfile.mkdtemp(prefix="tg-wl-")) / "PWNED"
    call = ba._mkcall("x", "read_file", {"file_path": f"touch {mark}"}, "0")
    out = b._call_tool(call, brain.child_env(), time.monotonic() + 30, b._stop_gen)
    ok("неизвестный инструмент не исполняется", "нет инструмента" in str(out), str(out))
    time.sleep(0.2)
    ok("побочного эффекта нет", not mark.exists())

    bad = ba._mkcall("y", "Bash", {"__bad__": "не json"}, "1")
    ok("битые аргументы — вежливый отказ",
       "не JSON" in str(b._call_tool(bad, {}, time.monotonic() + 5, b._stop_gen)))


def test_detail_not_arg():
    """detail — витрина для чата, arg — то, что исполняется. Их подмена
    местами и была каналом инъекции."""
    print("— витрина отвязана от исполнения —")
    c = ba._mkcall("i", "Read", {"file_path": "/tmp/s.png"}, "0")
    ok("arg берётся по схеме", c.arg == "/tmp/s.png")
    c2 = ba._mkcall("i", "Bash", {"command": "echo x", "file_path": "/ложь"}, "0")
    ok("для Bash исполняется command", c2.arg == "echo x")


# ──────────────────────────────────── цикл ───────────────────────────────────
def test_loop_basic():
    print("— цикл: инструмент → результат → финал —")
    ev = []
    b = FakeBrain([
        turn(calls=[("Bash", {"command": "echo раз"})]),
        turn(text="Готово, сэр."),
    ])
    out = run(b, events=ev)
    ok("финальный текст", out == "Готово, сэр.", out)
    ok("прогресс ушёл в чат", ev == [("Bash", "echo раз")], str(ev))
    ok("было два обращения к модели", len(b.sent) == 2, str(len(b.sent)))
    second = b.sent[1]["messages"]
    ok("результат вернулся одним user-ходом",
       second[-1]["role"] == "user" and second[-1]["content"][0]["type"] == "tool_result")


def test_loop_two_calls_one_turn():
    print("— два вызова в одном ходе —")
    b = FakeBrain([
        turn(calls=[("Bash", {"command": "echo a"}), ("Bash", {"command": "echo b"})]),
        turn(text="ок"),
    ])
    run(b)
    last = b.sent[1]["messages"][-1]
    ok("оба результата в ОДНОМ user-ходе",
       last["role"] == "user" and len(last["content"]) == 2, str(last)[:120])
    ok("порядок сохранён",
       [c["tool_use_id"] for c in last["content"]] == ["tu_0", "tu_1"])


def test_loop_stop_before_request():
    print("— устаревший снимок стопа —")
    b = FakeBrain([turn(text="не должно дойти")])
    base = b._stop_gen
    b._stop_gen = base + 1
    try:
        b._run_sync("x", "fake-model", False, lambda t, d: None, base)
        ok("Aborted до запроса", False, "цикл отработал")
    except brain.Aborted:
        ok("Aborted до запроса", True)
    ok("к модели не ходили вовсе", not b.sent)


def test_loop_abort_from_progress():
    print("— /stop прямо во время вызова —")
    b = FakeBrain([
        turn(calls=[("Bash", {"command": "echo раз"})]),
        turn(text="не должно дойти"),
    ])
    base = b._stop_gen

    def on_tool(t, d):
        b._stop_gen += 1          # хозяин нажал /stop ровно сейчас

    try:
        b._run_sync("x", "fake-model", False, on_tool, base)
        ok("цикл прерван", False, "дошёл до конца")
    except brain.Aborted:
        ok("цикл прерван", True)
    ok("второго обращения к модели не было", len(b.sent) == 1, str(len(b.sent)))


def test_loop_repeat_guard():
    """Слом №2, пробой А5: модель делала 40 настоящих кликов подряд."""
    print("— детектор зацикливания —")
    same = ("Bash", {"command": "echo одно и то же"})
    b = FakeBrain([turn(calls=[same]) for _ in range(8)])
    try:
        run(b)
        ok("зацикливание прервано", False, "цикл дошёл до конца")
    except RuntimeError as e:
        ok("зацикливание прервано", "зациклился" in str(e), str(e))


def test_loop_per_turn_cap():
    print("— потолок вызовов за ход —")
    many = [("Bash", {"command": f"echo {i}"}) for i in range(ba.MAX_PER_TURN + 5)]
    b = FakeBrain([turn(calls=many), turn(text="ок")])
    run(b)
    results = b.sent[1]["messages"][-1]["content"]
    ok("ответ есть на КАЖДЫЙ вызов", len(results) == len(many), str(len(results)))
    refused = [c for c in results if "слишком много" in str(c.get("content"))]
    ok("лишние получили отказ, а не исполнение", len(refused) == 5, str(len(refused)))


def test_loop_max_steps():
    print("— потолок шагов —")
    old, ba.MAX_STEPS = ba.MAX_STEPS, 3
    try:
        b = FakeBrain([turn(calls=[("Bash", {"command": f"echo {i}"})]) for i in range(3)])
        try:
            run(b)
            ok("MAX_STEPS прерывает", False, "цикл не прервался")
        except RuntimeError as e:
            ok("MAX_STEPS прерывает", "шагов" in str(e), str(e))
    finally:
        ba.MAX_STEPS = old


def test_loop_empty_final():
    """Слом №2, пробой А11: пустой ход выдавался хозяину как «Готово»."""
    print("— пустой ответ модели — это провал —")
    b = FakeBrain([turn(text="")])
    try:
        run(b)
        ok("пустой финал — ошибка", False, "вернулся как успех")
    except RuntimeError as e:
        ok("пустой финал — ошибка", "пустой ответ" in str(e), str(e))


def test_loop_text_toolcall():
    """Слом №2, пробой А1: вызов текстом отдавался хозяину сырым XML."""
    print("— вызов инструмента пришёл текстом —")
    b = FakeBrain([
        turn(text='<tool_call>{"name":"Bash","arguments":{"command":"echo текстом"}}</tool_call>'),
        turn(text="ок"),
    ])
    ev = []
    out = run(b, events=ev)
    ok("вызов распознан и исполнен", ev == [("Bash", "echo текстом")], str(ev))
    ok("итог получен", out == "ок")
    last = b.sent[1]["messages"][-1]
    ok("ответ ушёл обычным user-ходом, без чужого id",
       last["role"] == "user" and isinstance(last["content"], str), str(last)[:120])


def test_loop_deadline():
    print("— общий таймаут —")
    old, brain.TIMEOUT = brain.TIMEOUT, 0
    try:
        b = FakeBrain([turn(text="ок")])
        try:
            run(b)
            ok("таймаут срабатывает", False, "цикл отработал")
        except RuntimeError as e:
            ok("таймаут срабатывает", "превысил таймаут" in str(e), str(e))
    finally:
        brain.TIMEOUT = old


# ─────────────────────────────────── история ─────────────────────────────────
def test_history_roundtrip():
    print("— история переживает рестарт —")
    b = FakeBrain([turn(calls=[("Bash", {"command": "echo раз"})]), turn(text="первый")])
    run(b, "задача один", cont=False)

    b2 = FakeBrain([turn(text="второй")])
    b2.API = b.API
    run(b2, "задача два", cont=True)
    msgs = b2.sent[0]["messages"]
    texts = [m for m in msgs if isinstance(m.get("content"), str)]
    ok("прошлая задача в контексте",
       any("задача один" in m["content"] for m in texts), str(texts)[:160])
    ok("новая задача последняя", msgs[-1]["content"] == "задача два")


def test_history_no_images():
    """Слом №2, пробой А9: мегабайты base64 копились в файле и в запросах."""
    print("— картинки не копятся —")
    b = FakeBrain()
    h = b._hist()
    h.clear()
    h.append({"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "A" * 5000}}]})
    raw = h.path.read_text(encoding="utf-8")
    ok("base64 в файл не попал", "AAAA" not in raw, raw[:80])
    ok("вместо картинки заглушка", "снимок экрана" in raw)


def test_history_safe_prefix():
    """Слом №3: обрезка по счётчику оставляла tool_result без своего tool_use
    и это давало 400 НАВСЕГДА. Проверяем на многих длинах — одна «удачная»
    прошла бы случайно."""
    print("— обрезка только по безопасной границе —")
    bad = 0
    for n in range(81, 141):
        msgs = [{"role": "user", "content": "старт"}]
        for i in range(n):
            if i % 2:
                msgs.append({"role": "user",
                             "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                          "content": "ок"}]})
            else:
                msgs.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": f"t{i}", "name": "Bash",
                     "input": {"command": "echo"}}]})
        old, ba.HISTORY_BYTES = ba.HISTORY_BYTES, 400
        try:
            got = ba._trim(list(msgs))
        finally:
            ba.HISTORY_BYTES = old
        if got and not ba._safe_start(got[0]):
            bad += 1
    ok("на всех длинах начало валидное", bad == 0, f"плохих длин: {bad}")


def test_history_repair_tail():
    """Слом №3: убитый посреди петли прогон оставлял висячий tool_use."""
    print("— висячий вызов достраивается —")
    b = FakeBrain()
    msgs = [{"role": "user", "content": "задача"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}]}]
    fixed = b._repair_tail(list(msgs))
    ok("заглушка дописана", len(fixed) == 3 and fixed[-1]["role"] == "user")
    ok("id совпадает", fixed[-1]["content"][0]["tool_use_id"] == "t1")
    ok("модель узнаёт причину", "прервано" in fixed[-1]["content"][0]["content"])


def test_history_broken_file():
    print("— битый файл не роняет —")
    b = FakeBrain()
    h = b._hist()
    h.clear()
    h.append({"role": "user", "content": "целая"})
    with h.path.open("a", encoding="utf-8") as f:
        f.write('{"role": "user", "content": "обор')   # SIGKILL при записи
    msgs = h.load()
    ok("загрузка не упала", isinstance(msgs, list))
    ok("целая строка уцелела", any(m.get("content") == "целая" for m in msgs), str(msgs))


def test_history_foreign_api():
    print("— история от другого API —")
    b = FakeBrain()
    h = b._hist()
    h.clear()
    h.append({"role": "user", "content": "своя"})
    other = ba._History(h.path, "чужой-api")
    ok("чужой штамп — чистый старт, без падения", other.load() == [])


def test_reset_clears():
    print("— /reset обнуляет историю —")
    b = FakeBrain()
    h = b._hist()
    h.clear()
    h.append({"role": "user", "content": "было"})
    b.reset()
    ok("после reset пусто", b._hist().load() == [])


def main():
    test_bash_basic()
    test_bash_background()
    test_bash_setsid_abort()
    test_bash_handshake()
    test_bash_timeout()
    test_read()
    test_whitelist()
    test_detail_not_arg()
    test_loop_basic()
    test_loop_two_calls_one_turn()
    test_loop_stop_before_request()
    test_loop_abort_from_progress()
    test_loop_repeat_guard()
    test_loop_per_turn_cap()
    test_loop_max_steps()
    test_loop_empty_final()
    test_loop_text_toolcall()
    test_loop_deadline()
    test_history_roundtrip()
    test_history_no_images()
    test_history_safe_prefix()
    test_history_repair_tail()
    test_history_broken_file()
    test_history_foreign_api()
    test_reset_clears()
    print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
    if FAIL:
        print("Провалены:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
