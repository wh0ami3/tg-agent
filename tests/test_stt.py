"""Юниты STT: ffmpeg-конвейер и Gemini-распознавание. Всё офлайн:
subprocess.run и HTTP замоканы, сеть и ключи не трогаются.

Запуск: uv run --project /home/jesse/Projects/tg-agent python tests/test_stt.py
"""
import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

import tg_agent.config as cfg
import tg_agent.stt as stt

_TMP = Path(tempfile.mkdtemp(prefix="tg-agent-stt-test-"))
cfg.CONFIG = _TMP / "tg-agent.env"
cfg.GEMINI_ENV = _TMP / "env"
cfg.GEMINI_ENV.write_text("GEMINI_API_KEY=g-key-test\n")

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    def __init__(self):
        self.calls = []
        self.result = FakeProc(stdout=b"RIFF-wav-bytes")

    def __call__(self, cmd, input=None, capture_output=None, timeout=None):
        self.calls.append({"cmd": cmd, "input": input})
        return self.result


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.resp = FakeResp(payload={"candidates": [{"content": {"parts": [{"text": " привет "}]}}]})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.resp


fake_run = FakeRun()
stt.subprocess.run = fake_run
fake_http = FakeHTTP()
stt._HTTP = fake_http


def test_to_wav():
    print("— ffmpeg-конвейер —")
    out = stt._to_wav(b"ogg-bytes")
    ok("байты через pipe", fake_run.calls[0]["input"] == b"ogg-bytes")
    cmd = fake_run.calls[0]["cmd"]
    ok("wav 16k mono", "16000" in cmd and "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1")
    ok("результат — stdout ffmpeg", out == b"RIFF-wav-bytes")

    fake_run.result = FakeProc(returncode=1, stderr="кодек не найден".encode())
    try:
        stt._to_wav(b"x")
        ok("ошибка ffmpeg — RuntimeError", False)
    except RuntimeError as e:
        ok("ошибка ffmpeg — RuntimeError", "кодек" in str(e))
    fake_run.result = FakeProc(stdout=b"RIFF-wav-bytes")


def test_gemini():
    print("— Gemini STT —")
    fake_http.calls.clear()
    text = asyncio.run(stt.transcribe(b"ogg-bytes"))
    ok("текст распознан и обрезан", text == "привет")
    call = fake_http.calls[0]
    ok("ключ — заголовком, не в URL", call["headers"].get("x-goog-api-key") == "g-key-test"
       and "g-key-test" not in call["url"])
    part = call["json"]["contents"][0]["parts"][0]
    ok("wav ушёл base64", part["inline_data"]["data"] == base64.b64encode(b"RIFF-wav-bytes").decode())
    prompt = call["json"]["contents"][0]["parts"][1]["text"]
    ok("без TGAGENT_STT_LANG подсказки языка нет (автоопределение)",
       "expected:" not in prompt, prompt)
    old_lang = stt.LANG
    stt.LANG = "de-DE"
    try:
        fake_http.calls.clear()
        asyncio.run(stt.transcribe(b"ogg"))
        hinted = fake_http.calls[0]["json"]["contents"][0]["parts"][1]["text"]
    finally:
        stt.LANG = old_lang
    ok("заданный язык уходит подсказкой", "expected: de-DE" in hinted, hinted)
    ok("температура 0", call["json"]["generationConfig"]["temperature"] == 0)


def test_empty():
    print("— тишина и safety —")
    fake_http.resp = FakeResp(payload={"candidates": []})
    ok("пустые candidates — ''", asyncio.run(stt.transcribe(b"x")) == "")
    fake_http.resp = FakeResp(payload={})
    ok("совсем пустой ответ — ''", asyncio.run(stt.transcribe(b"x")) == "")
    fake_http.resp = FakeResp(payload={"candidates": [{"content": {"parts": [{"text": "привет"}]}}]})


def test_no_key():
    print("— без ключа —")
    cfg.GEMINI_ENV.write_text("JARVIS_BRAIN=claude\n")
    try:
        asyncio.run(stt.transcribe(b"x"))
        ok("нет ключа — RuntimeError", False)
    except RuntimeError as e:
        ok("нет ключа — RuntimeError", "GEMINI_API_KEY" in str(e))
    cfg.GEMINI_ENV.write_text("GEMINI_API_KEY=g-key-test\n")


test_to_wav()
test_gemini()
test_empty()
test_no_key()

print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} ❌")
if FAIL:
    print("Провалены:", FAIL)
    sys.exit(1)
