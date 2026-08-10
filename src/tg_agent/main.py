"""Точка входа: конфиг → бот → вечный long-polling.

Запускается из systemd --user (tg-agent.service); логи забирает journald. SIGTERM/SIGINT — чистая остановка:
фоновые задачи мягко гасятся (успевают пометить прогресс в чате),
цикл останавливается, живые CLI мозга убиваются (иначе осиротевший
claude продолжил бы действовать на компьютере без пульта).

Нет токена — не рестарт-петля systemd (Restart=always + SystemExit крутили
бы юнит вечно каждые 5 с), а тихое ожидание: токен появится в конфиге —
бот поднимется сам.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import traceback

from . import brain, config, paths, stt
from .telegram import AgentBot


async def _main(bot: AgentBot) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runner = asyncio.create_task(bot.run())
    stopper = asyncio.create_task(stop.wait())
    done, _pending = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    if stopper in done:
        # сначала мягко гасим фоновые задачи: HTTP-клиент внутри run() ещё
        # жив, и «⚠ прервано перезапуском» успевает долететь до чата
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bot.shutdown_tasks(), 6)
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        print("[tg-agent] остановлен по сигналу", flush=True)
    else:
        stopper.cancel()
        await runner  # пробрасывает исключение цикла (не должно случаться)


def run() -> None:
    paths.ensure()
    token = config.token()
    if not token:
        print(
            f"[tg-agent] нет TGAGENT_TELEGRAM_TOKEN в {config.CONFIG} — "
            "жду появления (проверка раз в 60 с)",
            flush=True,
        )
        while not token:
            time.sleep(60)
            token = config.token()
        print("[tg-agent] токен появился — поднимаюсь", flush=True)
    owner = config.chat_id()
    print(
        f"[tg-agent] старт: модель {config.model()}, "
        f"хозяин {'зафиксирован' if owner else 'ещё не зафиксирован (жду /start)'}",
        flush=True,
    )
    if not brain.Brain.available():
        print("[tg-agent] ВНИМАНИЕ: claude CLI не найден — задачи будут падать", flush=True)
    bot = AgentBot(token, owner)
    code = 0
    try:
        asyncio.run(_main(bot))
    except Exception:  # noqa: BLE001 — перед os._exit трейсбек надо напечатать самим
        traceback.print_exc()
        code = 1
    finally:
        brain.shutdown()
        stt.shutdown()
        print("[tg-agent] выход", flush=True)
        # os._exit: non-daemon потоки пулов (зависший запрос к Gemini)
        # иначе джойнятся интерпретатором БЕЗ таймаута — юнит вместо чистой
        # остановки ловил бы SIGKILL по TimeoutStopSec
        os._exit(code)


if __name__ == "__main__":
    run()
