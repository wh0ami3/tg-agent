"""Юниты бота: авторизация по chat_id, команды, задачи с прогрессом,
голосовые, [PHOTO:], разрезка длинных, offset, гигиена токена в логах.

Запуск: uv run --project /home/jesse/Projects/tg-agent python tests/test_telegram.py
Всё офлайн: HTTP, мозг, STT и скриншот замоканы.
"""
import asyncio
import os
import contextlib
import io
import sys
import tempfile
from pathlib import Path

import tg_agent.config as cfg
import tg_agent.strings as S
import tg_agent.telegram as tg_mod
from tg_agent.telegram import AgentBot, _split_photos

os.environ["TGAGENT_BACKEND"] = "claude-cli"   # пин движка: забытый в env бэкенд не должен менять эти тесты

_TMP = Path(tempfile.mkdtemp(prefix="tg-agent-tg-test-"))
cfg.CONFIG = _TMP / "tg-agent.env"
cfg.GEMINI_ENV = _TMP / "env"

tg_mod._PROGRESS_EVERY = 0.01  # прогресс-цикл в тестах не должен спать 2.5 с
tg_mod._INFLIGHT = _TMP / "inflight"  # маркер не в реальном ~/.jarvis

PASS, FAIL = [], []


def prefix(key):
    """Часть строки до подстановки — сравнение не зависит от языка."""
    return S.t(key, error="\x00", path="\x00").split("\x00")[0]

TOKEN = "1234567:SECRET-token-must-not-leak"


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))


class FakeResp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True, "result": {"message_id": 42}}
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Пишет все запросы; getUpdates отдаёт заготовленные пачки по очереди."""

    def __init__(self, batches=()):
        self.sent = []  # (url, {json|data|files})
        self.batches = list(batches)
        self.file_bytes = b"OGG-DATA"
        self._mid = 100

    async def get(self, url, params=None, timeout=None):
        if "getUpdates" in url:
            batch = self.batches.pop(0) if self.batches else []
            return FakeResp(payload={"ok": True, "result": batch})
        if "getFile" in url:
            return FakeResp(payload={"ok": True, "result": {"file_path": "voice/f_1.oga"}})
        if "/file/" in url:
            return FakeResp(content=self.file_bytes)
        return FakeResp()

    async def post(self, url, json=None, data=None, files=None, timeout=None):
        self.sent.append((url, json if json is not None else {"data": data, "files": files}))
        self._mid += 1
        return FakeResp(payload={"ok": True, "result": {"message_id": self._mid}})

    def texts(self, method="sendMessage"):
        return [j.get("text", "") for u, j in self.sent if method in u and isinstance(j, dict)]


class FakeBrain:
    def __init__(self, reply="Готово, сэр."):
        self.reply = reply
        self.asked = []
        self.resets = 0
        self.aborts = 0
        self.abort_ret = False
        self.hang = False
        self.stop_gen = 0

    async def ask(self, text, model, on_tool=None, stop_gen=None):
        self.asked.append((text, model))
        if self.hang:
            await asyncio.Event().wait()  # висит до отмены (рестарт-сценарий)
        if on_tool:
            on_tool("Bash", "jarvis-computer screenshot")
            await asyncio.sleep(0.05)  # прогресс-циклу есть когда проснуться
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def reset(self):
        self.resets += 1

    def abort(self):
        self.aborts += 1
        return self.abort_ret


def upd(uid, chat_id, text=None, voice=None):
    msg = {"chat": {"id": chat_id}}
    if text is not None:
        msg["text"] = text
    if voice is not None:
        msg["voice"] = voice
    return {"update_id": uid, "message": msg}


async def wait_bg(bot):
    for t in list(bot._bg):
        with contextlib.suppress(Exception):
            await t


def make_bot(chat_id=555, reply="Готово, сэр."):
    bot = AgentBot(TOKEN, chat_id)
    bot._brain = FakeBrain(reply)
    return bot


async def test_auth():
    print("— авторизация: первый /start — хозяин —")
    cfg.persist("TGAGENT_CHAT_ID", "")
    bot = AgentBot(TOKEN)  # хозяина нет
    http = FakeHTTP()

    await bot._handle_update(http, upd(1, 111, "привет"))
    ok("текст незнакомца до /start не захватывает пульт", bot._chat_id is None)
    ok("и не получает ответа", not http.sent)

    await bot._handle_update(http, upd(2, 111, voice={"file_id": "v1", "file_size": 10}))
    ok("голосовое незнакомца до /start игнорируется", bot._chat_id is None and not http.sent)

    await bot._handle_update(http, upd(3, 555, "/start"))
    ok("первый /start фиксирует chat_id", bot._chat_id == 555)
    ok("chat_id персистится", cfg.chat_id() == 555)
    ok("хозяин получил приветствие", len(http.sent) == 1 and http.sent[0][1]["chat_id"] == 555)

    await bot._handle_update(http, upd(4, 777, "выключи компьютер"))
    ok("чужой чат игнорируется", len(http.sent) == 1)

    await bot._handle_update(http, upd(5, 555, "/start"))
    ok("повторный /start — короткий ответ, не мозг", len(http.sent) == 2
       and not bot._brain._lock.locked())
    cfg.persist("TGAGENT_CHAT_ID", "")


async def test_task_flow():
    print("— задача: прогресс → итог —")
    bot = make_bot()
    http = FakeHTTP()
    await bot._handle_update(http, upd(10, 555, "открой ютуб"))
    await wait_bg(bot)
    ok("мозг получил задачу и модель", bot._brain.asked == [("открой ютуб", cfg.model())])
    texts = http.texts()
    ok("прогресс-сообщение поставлено", texts and texts[0].startswith("⏳"))
    edits = http.texts("editMessageText")
    ok("прогресс редактировался действием", any("Bash" in t for t in edits), str(edits))
    ok("финальная метка ✅", any(t.startswith("✅") for t in edits), str(edits))
    ok("итог дошёл отдельным сообщением", "Готово, сэр." in texts)


async def test_task_error():
    print("— сбой мозга — честный отчёт —")
    bot = make_bot(reply=RuntimeError("клод упал"))
    http = FakeHTTP()
    await bot._handle_update(http, upd(11, 555, "сделай что-нибудь"))
    await wait_bg(bot)
    ok("ошибка в чате", any(S.t("failed", error="клод упал") in t for t in http.texts()))
    ok("метка ⚠", any(t.startswith("⚠") for t in http.texts("editMessageText")))


async def test_photo_marker():
    print("— [PHOTO:] из ответа мозга —")
    shot = _TMP / "shot.png"
    shot.write_bytes(b"PNG-BYTES")
    bot = make_bot(reply=f"Вот экран.\n[PHOTO:{shot}]\nВсё открыто.")
    http = FakeHTTP()
    await bot._handle_update(http, upd(12, 555, "скриншот"))
    await wait_bg(bot)
    photos = [j for u, j in http.sent if "sendPhoto" in u]
    ok("фото отправлено", len(photos) == 1 and photos[0]["files"]["photo"][1] == b"PNG-BYTES")
    ok("маркер вырезан из текста", any(
        "Вот экран." in t and "[PHOTO:" not in t for t in http.texts()))

    clean, ps = _split_photos("нет такого\n[PHOTO:/нет/файла.png]")
    ok("несуществующий путь остаётся текстом", ps == [] and "[PHOTO:" in clean)
    clean2, ps2 = _split_photos(f"[PHOTO:{shot}] хвост")
    ok("маркер не в своей строке — не срабатывает", ps2 == [], str(ps2))


async def test_voice():
    print("— голосовое: скачивание → STT → задача —")
    bot = make_bot()
    http = FakeHTTP()
    seen = {}

    async def fake_transcribe(data):
        seen["data"] = data
        return "открой ютуб"

    orig = tg_mod.stt.transcribe
    tg_mod.stt.transcribe = fake_transcribe
    try:
        await bot._handle_update(http, upd(13, 555, voice={"file_id": "v1", "file_size": 999}))
        await wait_bg(bot)
    finally:
        tg_mod.stt.transcribe = orig
    ok("скачанные байты ушли в STT", seen.get("data") == b"OGG-DATA")
    ok("распознанное показано", any(S.t("heard", text="открой ютуб") in t for t in http.texts()))
    ok("мозг получил распознанный текст", bot._brain.asked
       and bot._brain.asked[0][0] == "открой ютуб")


async def test_voice_edge():
    print("— голосовое: тишина и великан —")
    bot = make_bot()
    http = FakeHTTP()

    async def silent(data):
        return ""

    orig = tg_mod.stt.transcribe
    tg_mod.stt.transcribe = silent
    try:
        await bot._handle_update(http, upd(14, 555, voice={"file_id": "v1", "file_size": 5}))
        await wait_bg(bot)
    finally:
        tg_mod.stt.transcribe = orig
    ok("тишина — «не расслышал», мозг не дёргался",
       any(S.t("voice_empty") in t for t in http.texts()) and not bot._brain.asked)

    http2 = FakeHTTP()
    await bot._handle_update(http2, upd(15, 555, voice={"file_id": "v1", "file_size": 999_999_999}))
    await wait_bg(bot)
    ok("великан отбит без скачивания", any(S.t("voice_too_big") in t for t in http2.texts())
       and not any("getFile" in u for u, _ in http2.sent))


async def test_model_cmd():
    print("— /model —")
    cfg.persist("TGAGENT_MODEL", "")
    bot = make_bot()
    http = FakeHTTP()
    await bot._handle_update(http, upd(20, 555, "/model"))
    ok("показ текущей и вариантов", any(
        "sonnet" in t and "opus" in t and "haiku" in t for t in http.texts()))
    await bot._handle_update(http, upd(21, 555, "/model opus"))
    ok("смена персистится", cfg.model() == "opus")
    await bot._handle_update(http, upd(22, 555, "/model gpt-99"))
    ok("незнакомая модель отбита", cfg.model() == "opus"
       and any(S.t("model_unknown", model="gpt-99") in t for t in http.texts()))
    await bot._handle_update(http, upd(23, 555, "/model@my_bot haiku"))
    ok("команда с @botname работает", cfg.model() == "haiku")
    cfg.persist("TGAGENT_MODEL", "")


async def test_screen_reset():
    print("— /screen и /reset —")
    shot = _TMP / "screen.png"
    shot.write_bytes(b"SCREEN-PNG")

    async def fake_shot():
        return str(shot)

    orig = tg_mod.brain.take_screenshot
    tg_mod.brain.take_screenshot = fake_shot
    bot = make_bot()
    http = FakeHTTP()
    try:
        await bot._handle_update(http, upd(30, 555, "/screen"))
        await wait_bg(bot)
    finally:
        tg_mod.brain.take_screenshot = orig
    photos = [j for u, j in http.sent if "sendPhoto" in u]
    ok("скриншот ушёл фото", len(photos) == 1 and photos[0]["files"]["photo"][1] == b"SCREEN-PNG")

    async def broken_shot():
        raise RuntimeError("spectacle упал")

    tg_mod.brain.take_screenshot = broken_shot
    http2 = FakeHTTP()
    try:
        await bot._handle_update(http2, upd(31, 555, "/screen"))
        await wait_bg(bot)
    finally:
        tg_mod.brain.take_screenshot = orig
    ok("сбой скриншота — текст в чат", any(prefix("screenshot_failed") in t for t in http2.texts()))

    await bot._handle_update(http2, upd(32, 555, "/reset"))
    ok("/reset двигает поколение мозга", bot._brain.resets == 1)
    ok("/reset глушит живой прогон", bot._brain.aborts == 1)

    await bot._handle_update(http2, upd(33, 555, "/чепуха"))
    ok("незнакомая команда — подсказка", any(S.t("unknown_command") in t for t in http2.texts()))


async def test_stop():
    print("— /stop —")
    bot = make_bot()
    http = FakeHTTP()
    await bot._handle_update(http, upd(34, 555, "/stop"))
    ok("нечего останавливать — честный ответ",
       any(S.t("nothing_to_stop") in t for t in http.texts()) and bot._brain.aborts == 1)

    bot._brain.abort_ret = True
    await bot._handle_update(http, upd(35, 555, "/stop"))
    ok("живой прогон — «останавливаю»", any(S.t("stopping") in t for t in http.texts()))

    # Aborted из мозга = «остановлено», метка ⛔
    bot2 = make_bot(reply=tg_mod.brain.Aborted("остановлено"))
    http2 = FakeHTTP()
    await bot2._handle_update(http2, upd(36, 555, "закрой всё"))
    await wait_bg(bot2)
    ok("в чате «Остановлено по вашей команде»",
       any(S.t("aborted_by_owner") in t for t in http2.texts()))
    ok("метка ⛔ на прогрессе",
       any(t.startswith("⛔") for t in http2.texts("editMessageText")))


async def test_restart_resilience():
    print("— рестарт: маркер в полёте, уведомление, мягкое гашение —")
    # 1) обычная задача чистит маркер за собой
    bot = make_bot()
    http = FakeHTTP()
    await bot._handle_update(http, upd(37, 555, "открой ютуб"))
    await wait_bg(bot)
    ok("маркер снят после задачи", not tg_mod._INFLIGHT.exists())

    # 2) зависшая задача + shutdown_tasks = правка прогресса «прервано»
    bot2 = make_bot()
    bot2._brain.hang = True
    http2 = FakeHTTP()
    await bot2._handle_update(http2, upd(38, 555, "вечная задача"))
    await asyncio.sleep(0.05)
    ok("маркер стоит, пока задача в полёте", tg_mod._INFLIGHT.exists())
    await bot2.shutdown_tasks()
    ok("прогресс помечен «прервано перезапуском»",
       any(S.t("interrupted_by_restart") in t for t in http2.texts("editMessageText")),
       str(http2.texts("editMessageText")))
    ok("маркер снят при мягком гашении", not tg_mod._INFLIGHT.exists())

    # 3) маркер пережил жёсткий рестарт (SIGKILL) → уведомление на старте
    tg_mod._INFLIGHT.touch()
    bot3 = make_bot()
    http3 = FakeHTTP()
    await bot3._announce_restart(http3)
    ok("хозяин уведомлён о прерванной задаче",
       any(S.t("restarted") in t for t in http3.texts()))
    ok("маркер очищен", not tg_mod._INFLIGHT.exists())

    # 4) без хозяина уведомления нет
    tg_mod._INFLIGHT.touch()
    bot4 = AgentBot(TOKEN)
    http4 = FakeHTTP()
    await bot4._announce_restart(http4)
    ok("без хозяина — молчим и маркер цел", not http4.sent and tg_mod._INFLIGHT.exists())
    tg_mod._INFLIGHT.unlink()


async def test_inflight_parallel():
    print("— параллельные задачи: маркер и /stop во время подготовки —")
    bot = make_bot()
    http = FakeHTTP()

    async def hang_transcribe(data):
        await asyncio.sleep(30)
        return ""

    orig = tg_mod.stt.transcribe
    tg_mod.stt.transcribe = hang_transcribe
    try:
        await bot._handle_update(http, upd(50, 555, voice={"file_id": "v1", "file_size": 5}))
        await asyncio.sleep(0.02)
        # /stop во время распознавания: CLI ещё нет, но задача готовится —
        # бот честно отвечает «останавливаю», а не «задач нет»
        await bot._handle_update(http, upd(51, 555, "/stop"))
        ok("во время STT /stop отвечает «останавливаю»",
           any(S.t("stopping") in t for t in http.texts()), str(http.texts()))
        # быстрая параллельная задача завершилась — маркер голосовой уцелел
        await bot._handle_update(http, upd(52, 555, "быстрая задача"))
        await asyncio.sleep(0.15)
        ok("маркер жив, пока голосовое в полёте", tg_mod._INFLIGHT.exists())
        await bot.shutdown_tasks()
        ok("после гашения маркер снят", not tg_mod._INFLIGHT.exists())
    finally:
        tg_mod.stt.transcribe = orig


async def test_send_split():
    print("— разрезка длинных —")
    bot = make_bot()
    http = FakeHTTP()
    await bot._send(http, "х" * 9000)
    sizes = [len(t) for t in http.texts()]
    ok("длинный текст порезан на ≤4000", sizes == [4000, 4000, 1000], str(sizes))


async def test_updates_offset():
    print("— getUpdates: offset двигается —")
    bot = make_bot()
    http = FakeHTTP(batches=[[upd(40, 555, "/start"), upd(41, 555, "/start")]])
    got = await bot._get_updates(http)
    ok("обновления получены", len(got) == 2)
    ok("offset = последний id + 1", bot._offset == 42, str(bot._offset))


def test_no_token_leak():
    print("— гигиена логов: токен не светится —")
    bot = make_bot()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            import httpx

            req = httpx.Request("GET", bot._url("getUpdates"))
            resp = httpx.Response(401, request=req)
            raise httpx.HTTPStatusError("boom", request=req, response=resp)
        except Exception as e:  # noqa: BLE001
            tg_mod._log_err("опрос не удался", e)
    out = buf.getvalue()
    ok("в логе нет токена", TOKEN not in out, out.strip())
    ok("в логе есть класс и статус", "HTTPStatusError" in out and "401" in out)


async def main():
    await test_auth()
    await test_task_flow()
    await test_task_error()
    await test_photo_marker()
    await test_voice()
    await test_voice_edge()
    await test_model_cmd()
    await test_screen_reset()
    await test_stop()
    await test_restart_resilience()
    await test_inflight_parallel()
    await test_send_split()
    await test_updates_offset()
    test_no_token_leak()

    print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
    if FAIL:
        print("Провалены:", FAIL)
        sys.exit(1)


asyncio.run(main())
