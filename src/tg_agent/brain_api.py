"""Мозг на голом HTTP: Anthropic Messages API и OpenAI-совместимые движки
(Ollama, LM Studio, llama.cpp server, vLLM, OpenRouter, Groq).

ПОЧЕМУ подкласс Brain, а не своя иерархия: живучесть — латч /stop, очередь на
_lock, счётчик _busy, маркер продолжения, пул потоков, гашение групп процессов,
переброс on_tool в event loop — уже написана в Brain.ask и покрыта юнитами.
Сменный тут ровно один шов, _run_sync: «сделать ход задачи синхронно, сообщая
о действиях через emit». У claude CLI это подпроцесс, здесь — цикл
«запрос к модели → выполнить её команду → отдать вывод».

ПОЧЕМУ без SDK: httpx уже в зависимостях; SDK по умолчанию ретраит обрыв
соединения и после /stop молча перезапустил бы прогон, а сырое тело ответа —
единственное, что помогает разбираться с локальными движками, которые
«OpenAI-совместимы» каждый по-своему.

ПОЧЕМУ инструменты названы Bash и Read: системный промпт уже написан под
«делай всё сам через Bash и CLI рук». Имена совпадают → промпт не форкается,
и строка прогресса в чате («⚙ Bash: …») побайтно та же, что у CLI.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import brain, config
from .brain import Aborted

ABORT_MSG = "остановлено командой хозяина"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else default


# Потолки. TIMEOUT их не заменяет: каждый шаг быстрый, а «пробую то же самое»
# крутится до самого таймаута — это четверть часа реальных кликов по живому
# экрану.
MAX_STEPS = _int("TGAGENT_MAX_STEPS", 16)          # ходов модели на задачу
MAX_CALLS = _int("TGAGENT_MAX_CALLS", 48)          # исполненных вызовов на задачу
MAX_PER_TURN = _int("TGAGENT_MAX_PER_TURN", 8)     # вызовов в одном ответе
REPEAT_LIMIT = _int("TGAGENT_REPEAT_LIMIT", 3)     # одинаковых подряд
TOOL_TIMEOUT = _int("TGAGENT_TOOL_TIMEOUT", 120)
HTTP_READ = _int("TGAGENT_HTTP_READ", 120)         # заметно меньше TIMEOUT
MAX_TOKENS = _int("TGAGENT_MAX_TOKENS", 8192)
MAX_OUT = _int("TGAGENT_MAX_OUT", 6000)            # вывод команды для модели
MAX_IMAGE = _int("TGAGENT_MAX_IMAGE", 3 * 1024 * 1024)
HISTORY_BYTES = _int("TGAGENT_HISTORY_BYTES", 400_000)
EFFORT = os.environ.get("TGAGENT_EFFORT", "").strip()

_RETRY = {408, 409, 429, 500, 502, 503, 504, 529}
_IMG_SUFFIX = (".png", ".jpg", ".jpeg", ".webp")

# Голая модель не знает, что у неё нет Write/Grep/WebFetch, и тратит ходы на их
# вызовы. Дописываем к общему промпту; сам system_style() не трогаем.
TOOLS_NOTE = (
    " TOOLS: you have exactly two — Bash and Read. There is no Write, Grep, "
    "Glob, Edit or WebFetch: use Bash (cat, tee, grep, curl) instead. Bash "
    "keeps no state between calls — every command starts in the same working "
    "directory, so chain with && instead of relying on an earlier cd. Read "
    "takes file_path of a PNG/JPEG and shows you that image; for text files "
    "use Bash."
)

_BASH_DOC = ("Run a shell command on the owner's computer and get its output. "
             "This is how you act: the hands CLI, launching programs, files.")
_READ_DOC = "Look at an image file (a screenshot path printed by the hands CLI)."

_SCHEMA = {
    "Bash": {"type": "object", "properties": {"command": {"type": "string"}},
             "required": ["command"]},
    "Read": {"type": "object", "properties": {"file_path": {"type": "string"}},
             "required": ["file_path"]},
}
# Ключ аргумента для ИСПОЛНЕНИЯ берём отсюда, а НЕ из brain._tool_detail:
# _tool_detail — витрина для чата с приоритетом command→file_path→…, и если
# исполнять её результат, то Read с file_path="touch /tmp/x" уедет в shell.
_ARG_KEY = {"Bash": "command", "Read": "file_path"}

_A_TOOLS = [{"name": n, "description": d, "input_schema": _SCHEMA[n]}
            for n, d in (("Bash", _BASH_DOC), ("Read", _READ_DOC))]
_O_TOOLS = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"],
    "parameters": t["input_schema"]}} for t in _A_TOOLS]


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    args: dict
    arg: str                 # то, что реально исполняется (взято по схеме)
    detail: str              # то, что уходит в чат через on_tool
    synthetic: bool = False  # вызов вытащен из ТЕКСТА — id модель не выдавала


def _mkcall(cid: str, name: str, args, seq: str, synthetic: bool = False) -> Call:
    if not isinstance(args, dict):
        args = {}
    key = _ARG_KEY.get(name, "")
    arg = str(args.get(key) or "").strip() if key else ""
    detail = brain._tool_detail(args) or (
        json.dumps(args, ensure_ascii=False)[:200] if args else "")
    # id может не прийти вовсе (частая сборка Ollama) — тогда два вызова
    # получили бы одинаковый пустой id и сопоставить результат стало бы нечем
    return Call(cid or f"call_{seq}", name, args, arg, detail, synthetic)


_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FENCE_RE = re.compile(r"```(?:json|tool_code)?\s*(\{.*?\})\s*```", re.S)


def _calls_from_text(text: str, seq) -> list[Call]:
    """Вызов инструмента, пришедший ТЕКСТОМ.

    llama.cpp без --jinja, старые сборки Ollama и модели без своего парсера
    возвращают <tool_call>{...}</tool_call> в content при пустом tool_calls.
    Без этого разбора агент отдаёт хозяину сырой XML под галочкой «готово» и
    молча не делает ничего — худший из возможных отказов.
    """
    chunks = _TOOLCALL_RE.findall(text) + _FENCE_RE.findall(text)
    if not chunks:
        s = text.strip()
        if s.startswith("{") and '"name"' in s:
            chunks = [s]
    out: list[Call] = []
    for i, chunk in enumerate(chunks):
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = str(obj.get("name") or "")
        args = obj.get("arguments") or obj.get("parameters") or obj.get("input") or {}
        if isinstance(args, str):
            with contextlib.suppress(ValueError):
                args = json.loads(args or "{}")
        if name in _SCHEMA and isinstance(args, dict):
            out.append(_mkcall("", name, args, f"txt_{seq}_{i}", synthetic=True))
    return out


# ───────────────────────────── история диалога ───────────────────────────────
_STUB = {"type": "text", "text": "[снимок экрана, показан ранее]"}


def _scrub(node):
    """Выкинуть картинки. Один png — больше мегабайта base64: в файл истории
    он не попадает вовсе, иначе диалог за вечер весит сотни мегабайт."""
    if isinstance(node, list):
        return [_scrub(x) for x in node]
    if isinstance(node, dict):
        if node.get("type") in ("image", "image_url"):
            return dict(_STUB)
        return {k: _scrub(v) for k, v in node.items()}
    return node


def _has_image(msg: dict) -> bool:
    return '"image' in json.dumps(msg, ensure_ascii=False)


def _keep_last_image(msgs: list[dict]) -> None:
    """В живом контексте держим только ПОСЛЕДНИЙ снимок: иначе каждый
    следующий шаг тащит в API все предыдущие мегабайты."""
    seen = False
    for i in range(len(msgs) - 1, -1, -1):
        if not _has_image(msgs[i]):
            continue
        if seen:
            msgs[i] = _scrub(msgs[i])
        seen = True


def _safe_start(msg: dict) -> bool:
    """С чего диалог имеет право начинаться. Правило одно на оба API: обычное
    user-сообщение со СТРОКОВЫМ текстом. Всё прочее (tool_result у Anthropic,
    role=tool у OpenAI, снимок отдельным ходом) без своего предшественника —
    гарантированные 400."""
    return msg.get("role") == "user" and isinstance(msg.get("content"), str)


def _trim(msgs: list[dict]) -> list[dict]:
    """Обрезка головы — ТОЛЬКО по безопасной границе.

    Срез по счётчику (msgs[-80:]) почти всегда оставляет tool_result без своего
    tool_use, а это 400 НАВСЕГДА: каждая следующая задача падает одинаково,
    пока хозяин не догадается сделать /reset.
    """
    def cut():
        while msgs and not _safe_start(msgs[0]):
            msgs.pop(0)

    cut()
    while msgs and len(json.dumps(msgs, ensure_ascii=False)) > HISTORY_BYTES:
        msgs.pop(0)
        cut()
    return msgs


class _History:
    """Диалог на диске вместо --continue.

    JSONL и дозапись ПОСЛЕ КАЖДОГО хода, а не одним куском в конце: прогон
    убивают посреди петли (/stop, SIGKILL, таймаут), и «сохраню, когда
    закончу» теряет и саму задачу, и УЖЕ СДЕЛАННЫЕ действия — после рестарта
    модель начала бы кликать заново, а действия не идемпотентны. claude CLI
    пишет свою сессию так же, по строке на сообщение.
    """

    def __init__(self, path: Path, api: str) -> None:
        self.path, self.api = path, api

    def clear(self) -> list[dict]:
        self._write([])
        return []

    def append(self, msg: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_scrub(msg), ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as e:  # молча терять нельзя
            print(f"[brain] история не пишется: {type(e).__name__}", flush=True)

    def load(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return self.clear()
        head_ok, msgs = False, []
        for i, line in enumerate(raw):
            try:
                obj = json.loads(line)
            except ValueError:
                continue          # оборванная последняя строка: SIGKILL при записи
            if i == 0:
                head_ok = isinstance(obj, dict) and obj.get("api") == self.api
            elif isinstance(obj, dict) and obj.get("role"):
                msgs.append(obj)
        if not head_ok:           # история от другого API — начинаем заново,
            return self.clear()   # но задача не падает
        return msgs

    def rewrite(self, msgs: list[dict]) -> None:
        self._write(msgs)

    def _write(self, msgs: list[dict]) -> None:
        """tmp + os.replace: SIGKILL посреди write_text оставил бы обрезанный
        файл, и после рестарта агент тихо потерял бы контекст."""
        lines = [json.dumps({"v": 1, "api": self.api})]
        lines += [json.dumps(_scrub(m), ensure_ascii=False) for m in msgs]
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            print(f"[brain] история не сохранена: {e}", flush=True)


# ─────────────────────────────── общий цикл ──────────────────────────────────
class ApiBrain(brain.Brain):
    NAME = API = "api"
    DEF_MODEL = ""
    ALIASES: dict[str, str] = {}
    BASE = ""

    def __init__(self) -> None:
        super().__init__()          # WORKDIR.mkdir, _lock, латч, маркер .continue
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()

    # ------------------------------------------------------------- состояние
    def _hist(self) -> _History:
        # WORKDIR читается В МОМЕНТ вызова — как у Brain._marker(): тесты
        # подменяют brain.WORKDIR уже после импорта
        return _History(brain.WORKDIR / f"api-history-{self.API}.jsonl", self.API)

    def reset(self) -> None:
        super().reset()             # двигает поколение и снимает маркер
        self._hist().clear()

    # ------------------------------------------------------------------ /stop
    def abort(self) -> bool:
        """Базовый abort латчит поколение и убивает группы процессов — руки
        лежат в том же brain._LIVE, поэтому они гаснут бесплатно. Здесь
        добавлено единственное, чем можно прервать висящий HTTP-запрос:
        закрытие клиента. Закрываем в ОТДЕЛЬНОМ потоке — abort() зовут из
        event loop, блокировать его на закрытии сокета нельзя."""
        stopped = super().abort()
        return self._drop_client() or stopped

    def _client(self) -> httpx.Client:
        with self._http_lock:
            if self._http is None:
                self._http = httpx.Client(timeout=httpx.Timeout(
                    connect=10.0, read=HTTP_READ, write=30.0, pool=10.0))
            return self._http

    def _drop_client(self) -> bool:
        with self._http_lock:
            http, self._http = self._http, None
        if http is None:
            return False

        def _shut(c: httpx.Client) -> None:
            with contextlib.suppress(Exception):
                c.close()

        threading.Thread(target=_shut, args=(http,), daemon=True).start()
        return True

    def _close_client(self) -> None:
        """Штатное закрытие в конце прогона — мы в рабочем потоке, можно
        синхронно. Если abort() уже забрал клиент, здесь None и двойного
        закрытия (оно рвёт пул навсегда) не происходит."""
        with self._http_lock:
            http, self._http = self._http, None
        if http is not None:
            with contextlib.suppress(Exception):
                http.close()

    # ---------------------------------------------------------------- помощь
    def _check(self, baseline: int) -> None:
        if self._stop_gen != baseline:
            raise Aborted(ABORT_MSG)

    @staticmethod
    def _left(deadline: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise RuntimeError(
                f"мозг превысил таймаут {brain.TIMEOUT} с — задачу пришлось прервать")
        return left

    def model_id(self, model: str) -> str:
        m = self.ALIASES.get(model, model) or self.DEF_MODEL
        if not m:
            raise RuntimeError("не выбрана модель — задай её командой /model")
        return m

    # ------------------------------------------------------------------- ШОВ
    def _run_sync(self, text, model, continue_session, emit, stop_baseline=None) -> str:
        """Тот же контракт, что у claude-CLI-версии: строка ответа, Aborted при
        сдвиге стоп-поколения, RuntimeError человеческим однострочником."""
        if stop_baseline is None:
            stop_baseline = self._stop_gen
        self._check(stop_baseline)          # /stop прилетел, пока стояли в очереди
        deadline = time.monotonic() + brain.TIMEOUT
        env = brain.child_env()             # свежая сессия Wayland + руки в PATH
        system = brain.system_style() + TOOLS_NOTE   # пересобирается на прогон
        hist = self._hist()
        if continue_session:
            msgs = self._repair_tail(_trim(hist.load()))
            hist.rewrite(msgs)              # починенное сохраняем сразу
        else:
            msgs = hist.clear()             # /reset или первый ход — чистый файл
        user = self._user(text)
        msgs.append(user)
        hist.append(user)
        last_text, calls_done, same_key, same = "", 0, "", 0
        try:
            for _step in range(MAX_STEPS):
                self._check(stop_baseline)
                # Дедлайн проверяем ЗДЕСЬ, а не только внутри _post/_bash:
                # иначе таймаут — свойство транспорта, и подкласс со своим
                # _post терял бы его молча.
                self._left(deadline)
                _keep_last_image(msgs)
                data = self._post(self._body(self.model_id(model), system, msgs),
                                  deadline, stop_baseline)
                reply, calls, more = self._take(data, msgs, _step)
                hist.append(msgs[-1])
                if reply:
                    last_text = reply
                self._check(stop_baseline)
                if not calls:
                    if more:                # pause_turn: дослать историю как есть
                        continue
                    final = (reply or last_text).strip()
                    if not final:
                        # локальный движок на исчерпанном контексте отдаёт
                        # пустой ход — это провал, а не «Готово»
                        raise RuntimeError("мозг вернул пустой ответ — задача не выполнена")
                    return final
                done: list[tuple[Call, object]] = []
                for i, call in enumerate(calls):
                    self._check(stop_baseline)
                    emit(call.name, call.detail)      # лента прогресса в чат
                    key = f"{call.name}\x00{call.arg}"
                    same = same + 1 if key == same_key else 0
                    same_key = key
                    if same >= REPEAT_LIMIT + 1:
                        raise RuntimeError("мозг зациклился на одном действии — прервал")
                    if same >= REPEAT_LIMIT - 1:
                        out = ("ты повторяешь одно и то же действие — не выполняю. "
                               "Смени подход или объясни в ответе, что мешает.")
                    elif i >= MAX_PER_TURN:
                        out = "слишком много вызовов за один ход — этот не выполнен"
                    elif calls_done >= MAX_CALLS:
                        raise RuntimeError(
                            f"мозг сделал {MAX_CALLS} действий и не закончил — прервал")
                    else:
                        out = self._call_tool(call, env, deadline, stop_baseline)
                        calls_done += 1
                    done.append((call, out))
                mark = len(msgs)
                for call, out in done:
                    self._answer(msgs, call, out)
                for m in msgs[mark:]:
                    hist.append(m)
        finally:
            self._close_client()
        raise RuntimeError(f"мозг сделал {MAX_STEPS} шагов и не закончил — прервал")

    # ----------------------------------------------------------- инструменты
    def _call_tool(self, call: Call, env, deadline, baseline):
        """Белый список. НИКАКОГО «иначе в shell»: галлюцинированный read_file
        с file_path="touch /tmp/PWNED" отправил бы аргумент ЧТЕНИЯ в bash — а
        модель читает экран, то есть это ещё и канал инъекции."""
        if "__bad__" in call.args:
            return "аргументы вызова пришли не JSON — повтори вызов"
        if call.name == "Bash":
            return self._bash(call.arg, env, deadline, baseline)
        if call.name == "Read":
            return self._read(call.arg)
        return f"нет инструмента {call.name}. Есть только Bash и Read."

    def _bash(self, cmd: str, env, deadline, baseline) -> str:
        if not cmd:
            return "Bash требует непустой строковый command — не выполнено."
        left = max(1, int(min(TOOL_TIMEOUT, self._left(deadline))))
        # bash -c, а НЕ -lc: логин-шелл сорсит профиль хозяина на КАЖДУЮ
        # команду — пересобирает PATH (папка рук из child_env уезжает из
        # головы) и подмешивает свой вывод в результат.
        # stdout — во ВРЕМЕННЫЙ ФАЙЛ, а не в пайп: communicate() ждёт EOF, а
        # дескриптор пайпа наследуют потомки. Фоновый («firefox &») или ушедший
        # из группы (setsid) процесс держал бы нас до TOOL_TIMEOUT даже после
        # /stop и killpg — то есть /stop врал бы хозяину.
        # wait() ждёт СМЕРТЬ процесса и от чужих дескрипторов не зависит.
        with tempfile.TemporaryFile() as buf:
            proc = subprocess.Popen(
                ["bash", "-c", cmd], cwd=str(brain.WORKDIR), env=env,
                stdin=subprocess.DEVNULL, stdout=buf, stderr=subprocess.STDOUT,
                start_new_session=True,       # своя группа: /stop гасит и внуков
            )
            with brain._LIVE_LOCK:
                brain._LIVE.add(proc.pid)
            try:
                # рукопожатие против гонки: abort() двигает _stop_gen ДО снимка
                # _LIVE — значит либо он уже видит наш pid, либо мы видим сдвиг
                # здесь. Окна, в котором команда переживает /stop, не остаётся.
                if self._stop_gen != baseline:
                    brain._kill_tree(proc.pid)
                    raise Aborted(ABORT_MSG)
                try:
                    proc.wait(timeout=left)
                except subprocess.TimeoutExpired:
                    brain._kill_tree(proc.pid)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(5)          # reap: иначе зомби и «exit=None»
                    return self._out(buf, proc.returncode,
                                     f"[прервано по таймауту {left} с]")
            finally:
                with brain._LIVE_LOCK:
                    brain._LIVE.discard(proc.pid)
            self._check(baseline)   # /stop убил команду — огрызок модели не отдаём
            return self._out(buf, proc.returncode, "")

    @staticmethod
    def _out(buf, rc, note: str) -> str:
        buf.seek(0)
        out = buf.read().decode("utf-8", "replace").strip()
        if len(out) > MAX_OUT:
            half = MAX_OUT // 2
            out = out[:half] + "\n…[вырезано]…\n" + out[-half:]
        head = note or ("" if rc == 0 else f"exit={rc}")
        body = "\n".join(x for x in (head, out) if x)
        # ПУСТОЙ результат — генератор петли: `clickon` при успехе не печатает
        # ничего, и модель, не получив подтверждения, кликает снова. Anthropic
        # пустой блок ещё и отвергает.
        return body or "выполнено, код 0, вывод пуст"

    @staticmethod
    def _read(path: str):
        if not path:
            return "Read требует file_path — не выполнено."
        p = Path(path)
        if not p.is_file():
            return f"нет файла {path}"
        if p.suffix.lower() not in _IMG_SUFFIX:
            return f"{path} не картинка — читай текст через Bash (cat)"
        try:
            data = p.read_bytes()
        except OSError as e:
            return f"файл не читается: {type(e).__name__}"
        if len(data) > MAX_IMAGE:
            return (f"снимок больше {MAX_IMAGE // (1024 * 1024)} МБ — смотреть "
                    "не буду, работай через clickon")
        ext = p.suffix.lower().lstrip(".")
        kind = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        return {"media_type": kind, "b64": base64.b64encode(data).decode()}

    # ------------------------------------------------------------------ HTTP
    def _post(self, body: dict, deadline: float, baseline: int) -> dict:
        attempt = 0
        while True:
            self._check(baseline)
            left = self._left(deadline)
            try:
                r = self._client().post(
                    self._url(), headers=self._headers(), json=body,
                    timeout=httpx.Timeout(connect=10.0, read=min(HTTP_READ, left),
                                          write=30.0, pool=10.0))
            except httpx.HTTPError as e:
                # сюда же прилетает наш собственный close() по /stop
                self._check(baseline)
                if attempt >= 2:
                    # НИКОГДА не str(e): текст httpx содержит URL, а он уходит
                    # хозяину в чат
                    raise RuntimeError(
                        f"{self.NAME}: сеть недоступна ({type(e).__name__})") from None
                self._sleep(2 ** attempt, deadline, baseline)
                attempt += 1
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    raise RuntimeError(f"{self.NAME}: ответ не JSON") from None
                if isinstance(data, dict) and data.get("error") and not (
                        data.get("choices") or data.get("content")):
                    raise RuntimeError(self._fold(self.NAME, data["error"]))
                return data
            if r.status_code in _RETRY and attempt < 3:
                wait = float(2 ** attempt)
                with contextlib.suppress(ValueError, TypeError):
                    wait = max(wait, float(r.headers.get("retry-after") or 0))
                self._sleep(wait, deadline, baseline)
                attempt += 1
                continue
            raise RuntimeError(self._http_error(r, body.get("model", "")))

    def _sleep(self, sec: float, deadline: float, baseline: int) -> None:
        """Кусочками: иначе /stop во время бэкоффа не сработает до конца паузы."""
        end = time.monotonic() + min(sec, 30.0)
        while time.monotonic() < end:
            self._check(baseline)
            self._left(deadline)
            time.sleep(0.5)

    @staticmethod
    def _fold(who: str, err) -> str:
        msg = err.get("message") if isinstance(err, dict) else err
        return f"{who}: {str(msg or '')[:200]}"

    def _http_error(self, r: httpx.Response, model: str) -> str:
        """Текст исключения виден хозяину в чате — человеческий однострочник,
        без ключа, без URL, без дампа JSON."""
        detail = ""
        with contextlib.suppress(Exception):
            detail = str((r.json().get("error") or {}).get("message") or "")[:200]
        table = {401: "ключ не принят", 402: "на счёте кончились деньги",
                 403: "у ключа нет доступа к этой модели",
                 404: f"нет такой модели или адреса: {model}",
                 413: "запрос слишком большой — сделай /reset",
                 429: "лимит запросов исчерпан — попробуй позже",
                 529: "сервис перегружен — попробуй позже"}
        head = table.get(r.status_code) or f"ошибка {r.status_code}"
        return f"{self.NAME}: {head}" + (f" — {detail}" if detail else "")

    # ------------------------------------------- провод: у наследников
    def _url(self) -> str: ...
    def _headers(self) -> dict: ...
    def _body(self, model, system, msgs) -> dict: ...
    def _user(self, text: str) -> dict: ...
    def _take(self, data, msgs, step) -> tuple[str, list[Call], bool]: ...
    def _answer(self, msgs, call: Call, out) -> None: ...
    def _repair_tail(self, msgs: list[dict]) -> list[dict]: ...


def _as_text(call: Call, out) -> str:
    if isinstance(out, dict):
        return "снимок в этом режиме показать не могу — опиши, что нужно увидеть"
    return f"Результат {call.name}:\n{out}"


# ───────────────────────────── Anthropic Messages ────────────────────────────
class AnthropicBrain(ApiBrain):
    NAME = API = "anthropic"
    DEF_MODEL = "claude-sonnet-5"
    ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5",
               "haiku": "claude-haiku-4-5", "fable": "claude-fable-5"}
    BASE = "https://api.anthropic.com"

    @staticmethod
    def available() -> bool:
        return bool(config.api_key("anthropic"))

    def _url(self) -> str:
        return f"{(config.api_base() or self.BASE).rstrip('/')}/v1/messages"

    def _headers(self) -> dict:
        return {"x-api-key": config.api_key("anthropic"),
                "anthropic-version": "2023-06-01", "content-type": "application/json"}

    @staticmethod
    def _user(text: str) -> dict:
        return {"role": "user", "content": text}

    def _body(self, model, system, msgs) -> dict:
        body = {"model": model, "max_tokens": MAX_TOKENS, "system": system,
                "messages": msgs, "tools": _A_TOOLS}
        # thinking НЕ шлём: на новых моделях адаптивное включено по умолчанию,
        # а с выключенным мышлением модель иногда пишет вызов инструмента
        # простым текстом — команда молча не выполняется. output_config
        # отправляем только если хозяин задал effort: на прокси неизвестное
        # поле — 400.
        if EFFORT:
            body["output_config"] = {"effort": EFFORT}
        return body

    def _take(self, data, msgs, step):
        stop = data.get("stop_reason")
        if stop == "refusal":      # HTTP 200, а content пуст — content[0] упал бы
            raise RuntimeError("модель отклонила запрос")
        blocks = [b for b in (data.get("content") or []) if isinstance(b, dict)]
        # блоки кладём ОБРАТНО КАК ЕСТЬ, включая thinking с подписью:
        # изменённый или отфильтрованный блок — 400 на следующем ходе
        msgs.append({"role": "assistant", "content": blocks})
        text = "\n".join(b.get("text") or "" for b in blocks
                         if b.get("type") == "text").strip()
        calls = [_mkcall(str(b.get("id") or ""), str(b.get("name") or ""),
                         b.get("input"), f"{step}_{i}")
                 for i, b in enumerate(blocks) if b.get("type") == "tool_use"]
        if not calls and text:
            calls = _calls_from_text(text, step)
        return text, calls, stop == "pause_turn"

    def _answer(self, msgs, call, out) -> None:
        if call.synthetic:
            # tool_use_id, которого модель не выдавала, — это 400
            msgs.append({"role": "user", "content": _as_text(call, out)})
            return
        if isinstance(out, dict):
            content = [{"type": "image", "source": {
                "type": "base64", "media_type": out["media_type"], "data": out["b64"]}}]
        else:
            content = str(out)
        block = {"type": "tool_result", "tool_use_id": call.id, "content": content}
        # ВСЕ результаты — ОДНИМ user-ходом и ПЕРВЫМИ блоками: разбивка по
        # сообщениям отучает модель звать инструменты параллельно, а текст
        # перед tool_result — 400
        prev = msgs[-1] if msgs else None
        if (prev and prev.get("role") == "user" and isinstance(prev.get("content"), list)
                and prev["content"] and prev["content"][0].get("type") == "tool_result"):
            prev["content"].append(block)
        else:
            msgs.append({"role": "user", "content": [block]})

    def _repair_tail(self, msgs):
        """Убитый посреди петли прогон оставляет tool_use без tool_result — и
        это 400 НАВСЕГДА. Достраиваем заглушки: заодно модель узнаёт, почему
        обрыв, и не переделывает уже сделанное."""
        if not msgs or msgs[-1].get("role") != "assistant":
            return msgs
        ids = [b.get("id") for b in msgs[-1].get("content") or []
               if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
        if ids:
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": i,
                 "content": "прервано перезапуском агента"} for i in ids]})
        return msgs


# ───────────────── OpenAI-совместимый (он же локальные движки) ───────────────
class OpenAIBrain(ApiBrain):
    NAME = API = "openai"
    BASE = "https://api.openai.com/v1"

    @staticmethod
    def available() -> bool:
        return bool(config.api_key("openai") or config.api_base())

    def _url(self) -> str:
        return f"{(config.api_base() or self.BASE).rstrip('/')}/chat/completions"

    def _headers(self) -> dict:
        h = {"content-type": "application/json"}
        if key := config.api_key("openai"):
            h["Authorization"] = f"Bearer {key}"
        return h

    @staticmethod
    def _user(text: str) -> dict:
        return {"role": "user", "content": text}

    def _body(self, model, system, msgs) -> dict:
        # system пересобирается на каждый ход и в историю НЕ кладётся: смена
        # TGAGENT_REPLY_LANG подхватывается без перезапуска.
        # tool_choice не шлём (Ollama его не поддерживает); parallel_tool_calls
        # тоже — вызовы мы и так исполняем строго по одному, а лишнее поле
        # часть локальных серверов отбивает 400-м.
        return {"model": model,
                "messages": [{"role": "system", "content": system}, *msgs],
                "tools": _O_TOOLS, "max_tokens": MAX_TOKENS, "stream": False}

    def _take(self, data, msgs, step):
        m = ((data.get("choices") or [{}])[0].get("message")) or {}
        raw = [c for c in (m.get("tool_calls") or []) if isinstance(c, dict)]
        calls: list[Call] = []
        for i, c in enumerate(raw):
            fn = c.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):          # OpenAI-канон
                try:
                    args = json.loads(args or "{}")
                except ValueError:
                    args = {"__bad__": args[:200]}
            elif not isinstance(args, dict):   # Ollama /api-стиль: уже объект
                args = {}
            calls.append(_mkcall(str(c.get("id") or ""), str(fn.get("name") or ""),
                                 args, f"{step}_{i}"))
        text = str(m.get("content") or "").strip()
        # content НИКОГДА не null: Mistral и часть локальных серверов на null
        # отвечают 400. tool_calls пересобираем из calls — так в эхо попадают
        # ТЕ ЖЕ id, что мы синтезировали вместо отсутствующих.
        echo = {"role": "assistant", "content": text}
        if calls:
            echo["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name,
                              "arguments": json.dumps(c.args, ensure_ascii=False)}}
                for c in calls]
        msgs.append(echo)
        if not calls and text:
            calls = _calls_from_text(text, step)
        # признак «есть вызовы» — непустой список, а НЕ finish_reason:
        # Ollama и часть прокси шлют "stop" даже когда вызовы были
        return text, calls, False

    def _answer(self, msgs, call, out) -> None:
        if call.synthetic:
            msgs.append({"role": "user", "content": _as_text(call, out)})
            return
        if isinstance(out, dict):
            # сообщение role=tool картинку нести не может
            msgs.append({"role": "tool", "tool_call_id": call.id,
                         "content": "снимок ниже"})
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": "Вот запрошенный снимок экрана."},
                {"type": "image_url", "image_url": {
                    "url": f"data:{out['media_type']};base64,{out['b64']}"}}]})
        else:
            # поле name НЕ кладём: часть провайдеров отвергает messages[].name
            msgs.append({"role": "tool", "tool_call_id": call.id, "content": str(out)})

    def _repair_tail(self, msgs):
        if not msgs or msgs[-1].get("role") != "assistant":
            return msgs
        for c in msgs[-1].get("tool_calls") or []:
            if isinstance(c, dict) and c.get("id"):
                msgs.append({"role": "tool", "tool_call_id": c["id"],
                             "content": "прервано перезапуском агента"})
        return msgs


CLASSES = {"anthropic": AnthropicBrain, "openai": OpenAIBrain}


def probe(name: str) -> str:
    if name == "anthropic":
        return "" if AnthropicBrain.available() else "нет ANTHROPIC_API_KEY"
    if name == "openai":
        return "" if OpenAIBrain.available() else "нет OPENAI_API_KEY и TGAGENT_API_BASE"
    return f"неизвестный мозг {name}"
