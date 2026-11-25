# Muse–Telegram Bridge

Connect your **Meta Muse** assistant to Telegram through your own bot, so you can chat with Muse from Telegram — with your full memory, files, goals, and tools — not just a bare LLM.

This is the strongest Muse–Telegram bridge available: it goes beyond text with photos, files, voice messages, inline buttons, live (streaming-style) replies, and a self-healing poller that costs almost no tokens while you are silent.

> 🇮🇷 نسخه‌ی فارسی: [README-fa.md](README-fa.md)

## Features

| Feature | What it does |
|---|---|
| **Full Muse, on Telegram** | Your Muse (memory, files, goals, tools) answers in Telegram, not a generic chatbot. |
| **Text, photos, files** | Send photos (analyzed), documents (read: PDF, code, text), and plain messages. |
| **Voice messages** | Send a voice note; Whisper transcribes it locally (private, free) and Muse replies. |
| **Inline buttons & commands** | `/start`, `/help`, `/status`, `/schedule` plus a button menu. |
| **Streaming-style replies** | Long answers grow in place via message edits, so you see them appear live. |
| **Typing keeper** | The typing indicator stays alive until your reply lands. |
| **Stuck watchdog** | If a reply stalls (e.g. an approval card waits in the Muse app), the bot tells you. |
| **👀 read receipts** | Your message gets a 👀 reaction the moment it is claimed. |
| **Self-healing poller** | Lightweight long-polling process (zero agent turns while idle), with a heartbeat, single-instance lock, and automatic restart. |
| **Private by design** | Only your chat id is answered; strangers are ignored. The token stays in Muse's Secure Vault. |

## One-prompt setup (give this to Muse)

Don't want to follow the manual steps? Paste this to your Muse and it will set up the whole bridge for you:

```
Set up the Muse–Telegram bridge from this repo (https://github.com/SPARKY-CR/muse-telegram-bridge) so I can chat with you from Telegram.

1. Walk me through creating a Telegram bot with @BotFather (/newbot). Then give me the secure link to enter the bot token — register it as the `custom.telegram` connection (host api.telegram.org, placement `url_path_segment:/bot{}`). Never ask me to paste the token in chat, and never print or log it.
2. Ask me to message the bot and press Start, then read my numeric chat id from the bot's updates.
3. Set up the bridge: copy the files into place, write state.json with my chat id, and start the poller in the background.
4. Create a Muse hook from poller/hook-script.sh (poll every 30 seconds) using the worker instruction in docs/worker-prompt.md.
5. Send me a test message on Telegram with the main menu keyboard and confirm the bridge is live.
6. If I want voice messages, install the local Whisper model as described in the README.

Rules: only my chat id may be answered (ignore strangers); keep the typing keeper and stuck watchdog running; if any step fails, explain plainly what happened and what you need from me.
```

## How it works

```
Telegram ──getUpdates──► poller.py ──append──► inbox.jsonl
                                                   │
                                          hook (every 30s)
                                                   │ new message from you
                                                   ▼
                                        Muse worker agent ──telegram.py──► Telegram reply
```

- **poller.py** long-polls Telegram forever (no agent turns, no tokens). It claims each update, writes it to the inbox, and starts a typing keeper + stuck watchdog for your messages.
- **hook-script.sh** runs every 30 seconds, wakes a Muse agent only when a new message from you exists, and restarts the poller if it died or hung.
- **telegram.py** is the Bot API helper (send, send_photo, download, edit, react, inline keyboards, callbacks).
- The **worker agent** (prompt in `docs/worker-prompt.md`) reads the message, does the work, and replies on Telegram.

No webhook, no public server, no tunnel needed — the poller only makes outbound HTTPS calls to `api.telegram.org`.

## Setup

### 1. Create the bot

1. In Telegram, message **@BotFather** and send `/newbot`. Pick a name and username.
2. Copy the bot token BotFather gives you.
3. Message your new bot yourself and press **Start** (bots cannot message first).

### 2. Store the token (Muse Secure Vault)

In Muse, register the token as the `custom.telegram` connection (API access flow, host `api.telegram.org`, placement `url_path_segment:/bot{}`). The token is never written to a file or printed; the helper exchanges it for a short-lived surrogate at call time.

Standalone use (outside Muse): set `TELEGRAM_BOT_TOKEN` in the environment instead.

### 3. Find your chat id

Send any message to the bot, then run:

```bash
python3 bin/telegram.py getupdates
```

Your numeric chat id is in `message.chat.id`.

### 4. Configure

```bash
cp config.example.json runtime/state.json
# edit runtime/state.json: set "chat_id" to your id
```

### 5. Start the poller

```bash
bash setup.sh
```

This creates the runtime directory and starts the poller in the background. Check it with:

```bash
python3 bin/telegram.py getme
cat runtime/poller-state.json
```

### 6. Install the hook (Muse)

Create a Muse hook from `poller/hook-script.sh` (poll every 30 s) with the worker instruction in `docs/worker-prompt.md`. The hook wakes Muse only when you message the bot.

### 7. Optional: voice messages

Voice transcription uses a local Whisper model (private, no third party):

```bash
python3 -m venv whisper-venv
whisper-venv/bin/pip install faster-whisper huggingface_hub
whisper-venv/bin/hf download Systran/faster-whisper-small \
  --local-dir models/whisper-small
```

The worker prompt already points at these paths. Voice *replies* are not supported: Muse's speech tool does not speak Persian (or several other languages), so the bot always replies in text.

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome + main menu (inline buttons). |
| `/help` | List of commands. |
| `/status` | Bridge health: poller alive, uptime, polls, errors, offset. |
| `/schedule` | Today's classes / study plan (reads your schedule). |

Tap a menu button and the bot answers the matching command.

## Comparison

| | muse-telegram-bridge | twiceyuan/muse-telegram-connector | MuseBot |
|---|---|---|---|
| Real Muse (memory, files, tools) | ✅ | ✅ | ❌ (generic LLM) |
| Photos / files | ✅ | ❌ | ❌ |
| Voice messages (local Whisper) | ✅ | ❌ | ❌ |
| Inline buttons & commands | ✅ | ❌ | ✅ |
| Streaming-style replies | ✅ | ❌ | ❌ |
| Typing keeper / stuck watchdog | ✅ | ✅ | ❌ |
| Self-healing poller | ✅ | ❌ | ❌ |
| Zero tokens while idle | ✅ | ❌ | ❌ |

## Files

```
bin/telegram.py            Bot API helper (send, photo, download, edit, react, ...)
poller/poller.py           Lightweight long-polling process
poller/typing_keeper.py    Keeps "typing..." alive until a reply lands
poller/stuck_watchdog.py   Hints in chat if a reply stalls
poller/hook-script.sh      Muse hook: wakes the agent, supervises the poller
docs/worker-prompt.md      Instruction for the Muse worker agent
config.example.json        Example state.json (chat_id + offset)
setup.sh                   Creates runtime dir and starts the poller
```

## Security

- The bot token lives in Muse's Secure Vault (or an env var); it never appears in code, logs, or the repo.
- Only the configured chat id is answered. Messages from anyone else are skipped silently.
- The poller makes outbound calls only; no ports are opened and no webhook is registered.
- Voice audio is transcribed locally by Whisper; nothing is sent to a transcription service.

## License

MIT — see [LICENSE](LICENSE).
