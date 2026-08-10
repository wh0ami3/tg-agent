# tg-agent

**Control your computer from Telegram. Type or send a voice note — the agent
looks at your screen, moves the mouse, clicks, types, and reports back.**

No SSH. No exact commands. You describe the task in plain language; the agent
takes a screenshot, finds the element it needs, and does it.

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

127 assertions across 4 suites, all passing:

```
tests/test_brain.py       43
tests/test_telegram.py    52
tests/test_config.py      20
tests/test_stt.py         12
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
git clone https://github.com/YOURNAME/tg-agent
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

---

## Status

Running daily as a systemd service. Built by [Jesse](https://github.com/YOURNAME).

Available for freelance work on voice AI agents, Telegram automation and
LLM integrations.

## License

MIT
