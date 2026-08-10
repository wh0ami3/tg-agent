"""Telegram-агент: long-polling Bot API, один хозяин, задачи мозгу, отчёты.

Токен — TGAGENT_TELEGRAM_TOKEN из конфига (см. paths.py). В логи он не
попадает: URL Bot API содержит токен, поэтому ошибки httpx логируются
ТОЛЬКО классом и HTTP-статусом (str(e) у httpx включает URL — не использовать).

Авторизация: первый /start фиксирует chat_id хозяина (персист в
TGAGENT_CHAT_ID); все остальные чаты молча игнорируются (в лог — только id).

Отчётность: на задачу ставится прогресс-сообщение «⏳ Выполняю…», по ходу
работы оно редактируется последним действием мозга (не чаще раза в 2.5 с —
без спама), в конце — итог отдельным сообщением. Мозг может приложить
картинку строкой [PHOTO:/путь.png] — бот отправит её sendPhoto.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path

import httpx

from . import brain, config, stt
from .paths import INFLIGHT
from .strings import t

API = "https://api.telegram.org"
_MAX_MSG = 4000  # лимит Telegram 4096 — режем с запасом
_MAX_VOICE = 20 * 1024 * 1024  # потолок Bot API для getFile
_MAX_PHOTO = 10 * 1024 * 1024  # потолок sendPhoto
_PHOTO_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_PHOTO_RE = re.compile(r"^[ \t]*\[PHOTO:([^\]\n]+)\][ \t]*$", re.MULTILINE)
_PROGRESS_EVERY = 2.5  # частота правок прогресс-сообщения
# маркер «задача в полёте»: переживший рестарт файл = задача была прервана
# жёстко (SIGKILL/крэш) — хозяину об этом сообщается при следующем старте
_INFLIGHT = INFLIGHT


def _log_err(prefix: str, e: Exception) -> None:
    """Лог без токена: класс исключения + статус, никаких str(e) с URL."""
    status = ""
    if isinstance(e, httpx.HTTPStatusError):
        status = f" HTTP {e.response.status_code}"
    print(f"[tg-agent] {prefix}: {type(e).__name__}{status}", flush=True)


def _split_photos(text: str) -> tuple[str, list[str]]:
    """Вынимает строки [PHOTO:/путь] из ответа мозга. Не-файл остаётся текстом."""
    photos: list[str] = []

    def repl(m: re.Match) -> str:
        p = m.group(1).strip()
        path = Path(p)
        if path.suffix.lower() in _PHOTO_SUFFIXES and path.is_file():
            photos.append(p)
            return ""
        return m.group(0)

    cleaned = _PHOTO_RE.sub(repl, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, photos


class AgentBot:
    """Один бот — один хозяин — один мозг."""

    def __init__(self, token: str, chat_id: int | None = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._offset = 0
        self._brain = brain.make_brain()
        self._bg: set[asyncio.Task] = set()
        # счётчик задач в полёте (включая подготовку голосовых): маркер
        # _INFLIGHT ставится на 0→1 и снимается на →0 — завершение одной
        # параллельной задачи не стирает маркер другой
        self._active = 0

    def _begin_work(self) -> None:
        self._active += 1
        if self._active == 1:
            with contextlib.suppress(OSError):
                _INFLIGHT.touch()

    def _end_work(self) -> None:
        self._active -= 1
        if self._active <= 0:
            _INFLIGHT.unlink(missing_ok=True)

    def _url(self, method: str) -> str:
        return f"{API}/bot{self._token}/{method}"

    def _file_url(self, file_path: str) -> str:
        return f"{API}/file/bot{self._token}/{file_path}"

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def shutdown_tasks(self) -> None:
        """Мягкое гашение фоновых задач ПЕРЕД остановкой цикла: их
        CancelledError-ветки успевают пометить прогресс в чате, пока
        HTTP-клиент run() ещё жив."""
        for t in list(self._bg):
            t.cancel()
        if self._bg:
            await asyncio.wait(list(self._bg), timeout=5)

    # ------------------------------------------------------------------ цикл
    async def run(self) -> None:
        """Вечный long-polling; любой сбой — пауза и новая попытка."""
        async with httpx.AsyncClient(timeout=65) as http:
            print("[tg-agent] на связи (long-polling)", flush=True)
            await self._announce_restart(http)
            while True:
                try:
                    for upd in await self._get_updates(http):
                        await self._handle_update(http, upd)
                except asyncio.CancelledError:
                    raise
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 409:
                        # второй long-poll на этот же токен (мост Джарвиса?)
                        print(
                            "[tg-agent] конфликт 409: токен опрашивает кто-то ещё — жду 15 с",
                            flush=True,
                        )
                        await asyncio.sleep(15)
                    else:
                        _log_err("опрос не удался", e)
                        await asyncio.sleep(5)
                except Exception as e:  # noqa: BLE001 — сеть моргает, цикл живёт
                    _log_err("опрос не удался", e)
                    await asyncio.sleep(5)

    async def _announce_restart(self, http: httpx.AsyncClient) -> None:
        """Маркер пережил рестарт = задача была прервана жёстко (SIGKILL,
        крэш) и хозяин об этом не знает — сообщаем и чистим маркер."""
        if self._chat_id is None or not _INFLIGHT.exists():
            return
        _INFLIGHT.unlink(missing_ok=True)
        await self._send(http, t("restarted"))

    async def _get_updates(self, http: httpx.AsyncClient) -> list[dict]:
        r = await http.get(
            self._url("getUpdates"),
            params={
                "timeout": 50,
                "offset": self._offset,
                "allowed_updates": '["message"]',
            },
            timeout=60,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # ------------------------------------------------------------- сообщения
    async def _handle_update(self, http: httpx.AsyncClient, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        voice = msg.get("voice")
        if not isinstance(chat_id, int) or not (text or voice):
            return

        if self._chat_id is None:
            # хозяина ещё нет: первый /start — его (и только /start:
            # случайный текст незнакомца не должен захватить пульт)
            if text and text.split()[0].split("@")[0] == "/start":
                self._chat_id = chat_id
                config.persist("TGAGENT_CHAT_ID", str(chat_id))
                print(f"[tg-agent] хозяин зафиксирован: чат {chat_id}", flush=True)
                await self._send(http, t("welcome"))
            else:
                print(f"[tg-agent] сообщение до /start (чат {chat_id}) — игнорирую", flush=True)
            return

        if chat_id != self._chat_id:
            print(f"[tg-agent] чужой чат {chat_id} — игнорирую", flush=True)
            return

        if text.startswith("/"):
            await self._command(http, text)
            return

        # задачи — в фоне: длинная работа мозга (минуты) не должна
        # замораживать опрос, следующие сообщения принимаются сразу
        if voice:
            self._spawn(self._voice_task(http, voice))
        else:
            self._spawn(self._task(http, text))

    # --------------------------------------------------------------- команды
    async def _command(self, http: httpx.AsyncClient, text: str) -> None:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/start":
            await self._send(http, t("already_up"))
        elif cmd == "/model":
            if len(parts) == 1:
                await self._send(http, t("model_current", model=config.model()))
                return
            m = config.normalize_model(parts[1])
            if config.model_ok(m):
                config.persist("TGAGENT_MODEL", m)
                await self._send(http, t("model_set", model=m))
            else:
                await self._send(http, t("model_unknown", model=parts[1]))
        elif cmd == "/screen":
            self._spawn(self._screen_task(http))
        elif cmd == "/stop":
            # abort латчит поколение стопа: умрёт и живой CLI, и всё, что
            # стояло в очереди или ещё готовилось (голосовое в STT)
            if self._brain.abort() or self._active > 0:
                await self._send(http, t("stopping"))
            else:
                await self._send(http, t("nothing_to_stop"))
        elif cmd == "/reset":
            aborted = self._brain.abort() or self._active > 0
            self._brain.reset()
            await self._send(http, t("reset_and_stopped" if aborted else "reset_only"))
        else:
            await self._send(http, t("unknown_command"))

    # ----------------------------------------------------------------- задачи
    async def _task(self, http: httpx.AsyncClient, text: str,
                    stop_gen: int | None = None) -> None:
        if stop_gen is None:
            stop_gen = self._brain.stop_gen
        self._begin_work()
        try:
            progress_id = await self._send_one(http, t("working"))
            action: dict = {"txt": None}

            def on_tool(tool: str, detail: str) -> None:
                action["txt"] = f"⚙ {tool}: {detail[:200]}" if detail else f"⚙ {tool}"

            updater = asyncio.ensure_future(self._progress_loop(http, progress_id, action))
            t0 = time.monotonic()
            mark = "✅"
            try:
                reply = await self._brain.ask(text, config.model(), on_tool, stop_gen)
            except asyncio.CancelledError:
                # рестарт агента посреди задачи: shutdown_tasks гасит нас ДО
                # закрытия HTTP-клиента — успеваем честно пометить прогресс
                updater.cancel()
                if progress_id is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            self._edit(http, progress_id, t("interrupted_by_restart")), 3
                        )
                raise
            except brain.Aborted:
                mark = "⛔"
                reply = t("aborted_by_owner")
            except Exception as e:  # noqa: BLE001 — сбой мозга = честный отчёт в чат
                mark = "⚠"
                reply = t("failed", error=e)
            finally:
                updater.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await updater
            took = time.monotonic() - t0
            if progress_id is not None:
                await self._edit(http, progress_id, f"{mark} {took:.0f} с")
            clean, photos = _split_photos(reply or "")
            await self._send(http, clean or t("done" if mark == "✅" else "not_done"))
            for p in photos[:5]:
                await self._send_photo(http, p)
        finally:
            self._end_work()

    async def _progress_loop(self, http: httpx.AsyncClient, message_id: int | None, action: dict) -> None:
        if message_id is None:
            return
        last = None
        while True:
            await asyncio.sleep(_PROGRESS_EVERY)
            txt = action["txt"]
            if txt and txt != last:
                last = txt
                await self._edit(http, message_id, txt[:_MAX_MSG])

    async def _voice_task(self, http: httpx.AsyncClient, voice: dict) -> None:
        # снимок поколения стопа ДО скачивания и STT: /stop, нажатый пока
        # голосовое распознаётся, отменит и эту задачу
        stop_gen = self._brain.stop_gen
        self._begin_work()
        try:
            size = voice.get("file_size") or 0
            if size > _MAX_VOICE:
                await self._send(http, t("voice_too_big"))
                return
            try:
                data = await self._download(http, voice.get("file_id") or "")
                text = await stt.transcribe(data)
            except Exception as e:  # noqa: BLE001 — сбой распознавания не роняет цикл
                _log_err("голосовое не распозналось", e)
                await self._send(http, t("voice_failed"))
                return
            if not text:
                await self._send(http, t("voice_empty"))
                return
            await self._send(http, t("heard", text=text))
            await self._task(http, text, stop_gen)
        finally:
            self._end_work()

    async def _download(self, http: httpx.AsyncClient, file_id: str) -> bytes:
        r = await http.get(self._url("getFile"), params={"file_id": file_id}, timeout=30)
        r.raise_for_status()
        file_path = (r.json().get("result") or {}).get("file_path") or ""
        if not file_path:
            raise RuntimeError("getFile не вернул путь")
        f = await http.get(self._file_url(file_path), timeout=60)
        f.raise_for_status()
        return f.content

    async def _screen_task(self, http: httpx.AsyncClient) -> None:
        try:
            path = await brain.take_screenshot()
        except Exception as e:  # noqa: BLE001
            await self._send(http, t("screenshot_failed", error=e))
            return
        await self._send_photo(http, path)

    # -------------------------------------------------------------- отправка
    async def _send(self, http: httpx.AsyncClient, text: str) -> None:
        for i in range(0, len(text), _MAX_MSG):
            try:
                r = await http.post(
                    self._url("sendMessage"),
                    json={"chat_id": self._chat_id, "text": text[i : i + _MAX_MSG]},
                )
                if r.status_code != 200:
                    print(f"[tg-agent] sendMessage: HTTP {r.status_code}", flush=True)
            except Exception as e:  # noqa: BLE001 — недоставка не роняет цикл
                _log_err("sendMessage не удался", e)

    async def _send_one(self, http: httpx.AsyncClient, text: str) -> int | None:
        """Одно сообщение с возвратом message_id (для последующих правок)."""
        try:
            r = await http.post(
                self._url("sendMessage"),
                json={"chat_id": self._chat_id, "text": text[:_MAX_MSG]},
            )
            if r.status_code == 200:
                mid = (r.json().get("result") or {}).get("message_id")
                return mid if isinstance(mid, int) else None
            print(f"[tg-agent] sendMessage: HTTP {r.status_code}", flush=True)
        except Exception as e:  # noqa: BLE001
            _log_err("sendMessage не удался", e)
        return None

    async def _edit(self, http: httpx.AsyncClient, message_id: int, text: str) -> None:
        try:
            r = await http.post(
                self._url("editMessageText"),
                json={
                    "chat_id": self._chat_id,
                    "message_id": message_id,
                    "text": text[:_MAX_MSG],
                },
            )
            # 400 «message is not modified» — не ошибка, просто нечего менять
            if r.status_code not in (200, 400):
                print(f"[tg-agent] editMessageText: HTTP {r.status_code}", flush=True)
        except Exception as e:  # noqa: BLE001
            _log_err("editMessageText не удался", e)

    async def _send_photo(self, http: httpx.AsyncClient, path: str) -> None:
        try:
            data = Path(path).read_bytes()
        except OSError:
            await self._send(http, t("file_unreadable", path=path))
            return
        if len(data) > _MAX_PHOTO:
            await self._send(http, t("photo_too_big", path=path))
            return
        try:
            r = await http.post(
                self._url("sendPhoto"),
                data={"chat_id": str(self._chat_id)},
                files={"photo": (Path(path).name, data, "image/png")},
            )
            if r.status_code != 200:
                print(f"[tg-agent] sendPhoto: HTTP {r.status_code}", flush=True)
        except Exception as e:  # noqa: BLE001
            _log_err("sendPhoto не удался", e)
