# tg-agent

**Control your computer from Telegram. Type or send a voice note — the agent
looks at your screen, moves the mouse, clicks, types, and reports back.**

No SSH. No exact commands. You describe the task in plain language; the agent
takes a screenshot, finds the element it needs, and does it.

> ### ⚠️ Read this before you run it
>
> This agent gives a language model **unrestricted control of your machine**.
> It runs the reasoning CLI with `--dangerously-skip-permissions`, which means
> the model executes shell commands and GUI actions without asking you first.
> That is a deliberate design choice — a headless bot has nobody to ask — but
> it means:
>
> - **Do not run this on a machine you cannot afford to lose.** Use a spare
>   box, a VM, or a dedicated user account.
> - **Anything on your screen is untrusted input.** The agent reads the screen
>   to decide what to do, so a web page, document, or email can contain text
>   aimed at the model rather than at you. This is inherent to every
>   computer-use agent, not specific to this one — but you should know it
>   before pointing it at a browser.
> - **The bot is single-owner by design.** The first `/start` claims it and
>   every other chat is ignored — but guard the token like a password anyway.
>
> I run it on my own desktop and accept that trade-off. Decide for yourself.

---

## What it does

Send `open the browser and search for the weather` — or say it as a voice
note — and the agent:

1. takes a screenshot of your desktop (cursor included),
2. reasons about what's on screen,
3. moves the mouse with human-like motion, clicks, types, scrolls,
4. edits a live progress message as it works,
5. replies in chat when it's done — and can attach a screenshot.

## Commands

| Command | What it does |
|---|---|
| `/start` | Binds the bot to you. First chat to send it becomes the owner |
| `/screen` | Sends a screenshot of the current desktop |
| `/stop` | Kills the running task *and* everything queued behind it |
| `/reset` | Stop + start a fresh conversation |
| `/model` | Show or switch the reasoning model |

Voice notes are transcribed automatically and treated as tasks.

---

## Engineering notes

The interesting parts of this project are not the happy path.

**Cancellation that actually cancels.** A naive stop flag leaves queued work
running. This uses a *stop-latch generation* counter: every abort bumps a
generation, and each task compares the snapshot it captured at start — once
after the queue, and again immediately before spawning the subprocess. That
closes the dead window where a task could slip through between "stop" and
"launch". It kills the live process group and drains the queue.

**Survives restarts mid-task.** A marker file plus a `CancelledError` branch
rewrites the stale progress message instead of leaving a task spinning
forever. Conversation continuity is preserved across the restart.

**Session environment that doesn't go stale.** The systemd unit starts before
the desktop exports its display variables, and the display can change across
re-login. So the environment is re-read from `systemctl --user
show-environment` on every invocation (7–10 ms) instead of being captured once
at boot.

**Secrets.** Token lives in a `0600` file. Logs record the exception class and
HTTP status — never the raw exception, which is how tokens usually leak. There
is a test asserting exactly this.

**Ownership.** The first `/start` binds a chat id. Every other chat is
silently ignored — no error message, no acknowledgement.

## Tests

154 assertions across 5 suites, all passing:

```
tests/test_telegram.py    52
tests/test_brain.py       43
tests/test_strings.py     26
tests/test_config.py      20
tests/test_stt.py         13
```

Run them:

```bash
uv run python tests/test_brain.py
```

---

## Stack

Python · [uv](https://github.com/astral-sh/uv) · a single runtime dependency
(`httpx`) · Telegram Bot API long-polling · systemd user service ·
ffmpeg for audio · an LLM for reasoning and a vision model for locating
on-screen elements.

## Install

```bash
git clone https://github.com/wh0ami3/tg-agent
cd tg-agent
uv sync
```

Put your bot token in `~/.jarvis/tg-agent.env`:

```
TGAGENT_TELEGRAM_TOKEN=...
```

```bash
chmod 600 ~/.jarvis/tg-agent.env
```

Then run it, or install the systemd user unit so it starts on login and
restarts on failure.

### Configuration

| Variable | Default | What it does |
|---|---|---|
| `TGAGENT_TELEGRAM_TOKEN` | — | Bot token. Required. Lives in the `0600` env file, not the environment |
| `TGAGENT_HANDS_BIN` | `~/Projects/jarvis-app/bridge/.venv/bin` | Directory holding the CLI that drives mouse and keyboard. **Set this** — the default points at my own layout. If the directory doesn't exist, the command is looked up on `PATH` |
| `TGAGENT_HANDS_CMD` | `jarvis-computer` | Name of that CLI. Change it and the system prompt follows |
| `TGAGENT_LANG` | `en` | Language of the bot's own messages. See [Languages](#languages) |
| `TGAGENT_LOCALE_DIR` | `~/.jarvis/tg-agent-locales` | Where to look for translation files |
| `TGAGENT_REPLY_LANG` | — | Language the *agent* reports in. Free text: `English`, `Deutsch`, `日本語`. Empty means it replies in whatever language you wrote the task in |
| `TGAGENT_TIMEOUT` | `900` | Seconds before a task is killed, process group and all |
| `TGAGENT_STT_LANG` | — | Hint for voice-note language (`de-DE`, `ru-RU`). Empty means auto-detect |
| `TGAGENT_STT_MODEL` | `gemini-flash-latest` | Model used to transcribe voice notes |

---

## Languages

Everything you see is language-independent, in three separate places.

**1. What the agent says back to you.** Set `TGAGENT_REPLY_LANG` to any
language, written any way you like — `Spanish`, `Deutsch`, `日本語`. The model
speaks them natively, so nothing needs translating. Leave it empty and it
replies in whichever language you wrote the task in.

**2. Voice notes.** Auto-detected by default. Set `TGAGENT_STT_LANG` only if
transcription confuses similar languages.

**3. The bot's own 24 messages** — "Working…", "Nothing to stop", and so on.
English and Russian ship built in. For anything else, drop a JSON file into
the locale directory:

```bash
mkdir -p ~/.jarvis/tg-agent-locales
cat > ~/.jarvis/tg-agent-locales/de.json <<'EOF'
{
  "working": "⏳ Arbeite…",
  "done": "Fertig.",
  "nothing_to_stop": "Nichts zu stoppen — es läuft keine Aufgabe."
}
EOF
```

```bash
TGAGENT_LANG=de
```

You don't have to translate all of them. Missing keys fall back to English, so
a half-finished translation mixes languages instead of breaking. A malformed
file is ignored rather than crashing the bot — there are tests for both.

Keys live in [`src/tg_agent/strings.py`](src/tg_agent/strings.py). A locale
file also overrides the built-in languages, so you can reword the Russian or
English without touching the code.

---

## Security

What this project does carefully:

- **The token never reaches the logs.** The Bot API URL embeds the token, and
  `str(e)` on an `httpx` error includes the URL — so errors are logged as the
  exception class plus HTTP status only, never the raw exception. There is a
  test asserting this.
- **The speech-to-text key travels in a header, not a query parameter**, for
  the same reason: query strings end up inside error text.
- **Config writes are atomic and `0600`**, and refuse newlines in keys or
  values so a crafted value can't append a line to the env file.
- **Ownership is claimed only by `/start`.** Stray text from an unknown chat
  cannot take the remote, and unknown chats get no reply at all — not even an
  error, which would confirm the bot exists.
- **Stopping kills the whole process group.** An orphaned reasoning CLI would
  otherwise keep acting on the machine with nobody holding the remote.

What it deliberately does *not* do — see the warning at the top:

- It does not sandbox the model's actions.
- It does not sanitise what it reads off the screen.

Found something? Open an issue.

---

## Status

Running daily as a systemd service. Built by [Jesse](https://github.com/wh0ami3).

Available for freelance work on voice AI agents, Telegram automation and
LLM integrations.

## License

MIT
