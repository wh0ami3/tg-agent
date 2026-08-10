"""Распознавание голосовых: ogg/opus → ffmpeg wav 16k mono → Gemini STT.

Локальных LLM в этом проекте нет: VRAM ноута (4 ГБ) занята Whisper'ом
Джарвиса — второй large-v3 не влезет. Gemini слушает wav напрямую;
ключ GEMINI_API_KEY читается из ~/.jarvis/env на каждый запрос
(источник истины там — ротация ключа подхватывается без рестарта).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config

# Пусто (дефолт) — язык определяется автоматически: модель слышит любой.
# Ставить стоит, только если распознавание путает похожие языки — тогда
# подсказка вида "ru-RU" / "de-DE" / "ja-JP" повышает точность.
LANG = os.environ.get("TGAGENT_STT_LANG", "").strip()
GEMINI_STT_MODEL = os.environ.get("TGAGENT_STT_MODEL", "gemini-flash-latest")

# один клиент на процесс: TLS-рукопожатие на каждый запрос стоило бы ~0.3-0.5 с
_HTTP = httpx.Client(timeout=60)

# СВОЙ пул, не asyncio.to_thread: дефолтный executor джойнится в teardown
# asyncio.run БЕЗ таймаута (3.11) — SIGTERM во время зависшего запроса к
# Gemini вешал бы остановку юнита дольше TimeoutStopSec → SIGKILL
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stt")


def shutdown() -> None:
    _POOL.shutdown(wait=False, cancel_futures=True)
    # закрытие клиента рвёт сокет зависшего запроса к Gemini — воркер
    # разблокируется; финальную страховку даёт os._exit в main (non-daemon
    # потоки пула иначе джойнились бы интерпретатором без таймаута)
    with contextlib.suppress(Exception):
        _HTTP.close()


def _to_wav(data: bytes) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-fflags", "+bitexact",
            "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
        ],
        input=data,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr.decode(errors='replace')[-300:]}")
    return proc.stdout


def _recognize_gemini(wav: bytes, key: str) -> str:
    hint = f" (expected: {LANG})" if LANG else ""
    prompt = (
        "Transcribe this audio verbatim in its original language"
        f"{hint}. Output ONLY the transcription text, "
        "no comments or quotes. If there is no speech, output nothing."
    )
    resp = _HTTP.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_STT_MODEL}:generateContent",
        # ключ — заголовком, не query: URL попадает в текст HTTPStatusError и в логи
        headers={"x-goog-api-key": key},
        json={
            "contents": [{"parts": [
                {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}},
                {"text": prompt},
            ]}],
            "generationConfig": {"temperature": 0},
        },
        timeout=60,
    )
    resp.raise_for_status()
    # candidates бывает пустым (safety-блок, тишина) — это «нет речи», не IndexError
    cands = resp.json().get("candidates") or [{}]
    parts = cands[0].get("content", {}).get("parts", [])
    return " ".join(p.get("text", "") for p in parts).strip()


def _transcribe_sync(data: bytes) -> str:
    key = config.gemini_key()
    if not key:
        raise RuntimeError("нет GEMINI_API_KEY в ~/.jarvis/env")
    return _recognize_gemini(_to_wav(data), key)


async def transcribe(data: bytes) -> str:
    """Распознанный текст ('' — речи не найдено)."""
    return await asyncio.get_running_loop().run_in_executor(_POOL, _transcribe_sync, data)
