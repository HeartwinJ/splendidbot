# OnSinch Shift Notifier

A Dockerised Telegram bot that monitors the Splendid OnSinch staffing platform for new available shifts and sends instant notifications to your Telegram account. The bot checks every 15 minutes and only notifies you about shifts you haven't seen before — no spam, no repeats.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/) (v2+)
- A Telegram bot token (see below)
- Your OnSinch credentials for `splendid.onsinch.com`

---

## Setup

### 1. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts (choose a name and username)
3. Copy the **bot token** — it looks like `123456789:ABCdef...`

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | The token from BotFather |
| `POSTGRES_PASSWORD` | A strong password for the database |
| `POSTGRES_DB` | Database name (default: `onsinch_bot`) |
| `POSTGRES_USER` | Database user (default: `botuser`) |
| `SCRAPE_INTERVAL_MINUTES` | How often to check for shifts (default: `15`) |

### 3. Start the bot

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f bot
```

---

## Usage

1. Open Telegram and start a chat with your bot
2. Send `/start`
3. Send your OnSinch credentials in the format:
   ```
   yourname@example.com:yourpassword
   ```
   The bot will delete your message immediately and validate your credentials.
4. Once confirmed, the bot will notify you automatically whenever a new shift appears.

---

## Commands

| Command | Description |
|---|---|
| `/start` | Register or re-authenticate with your OnSinch credentials |
| `/stop` | Pause notifications |
| `/check` | Trigger an immediate check and show current available shifts |
| `/status` | Show your registration status and tracking stats |

---

## Notifications

Each new shift is sent as a separate message. You'll see:

- **Shift name and event**
- **Profession and specific role** (e.g. Waiting Staff — Food)
- **Date and time** in UK local time
- **Location**
- **Spots available**

Special indicators:

- ⭐ Featured shifts are highlighted
- ⚠️ Shifts that conflict with your existing bookings are flagged with a warning
- 🎖️ Team Leader positions are labelled

---

## Troubleshooting

**Bot says "Login failed"**
- Double-check your `email:password` — make sure there are no extra spaces
- If your OnSinch password contains a colon (`:`), everything after the first `:` is treated as the password, so this should work fine
- The login page uses Google reCAPTCHA. The bot uses browser automation to handle this transparently; if Splendid has added extra security measures, login may fail intermittently

**I stopped receiving notifications after a few weeks**
- Your OnSinch session cookie expires approximately every 24 hours — the bot re-logs in automatically
- If your OnSinch password changed, the bot will send a warning message. Use `/start` and re-enter your new credentials

**reCAPTCHA blocking login**
- The bot uses Playwright with stealth mode to avoid triggering reCAPTCHA challenges
- If a challenge appears despite this, the bot will log a warning but cannot solve visual challenges automatically. Try again after a few minutes; low scrape frequency (15 min) minimises the risk

**Database connection errors on first start**
- Docker Compose waits for Postgres to be healthy before starting the bot. If you see connection errors, check `docker compose logs db`

---

## Security notes

- Credentials are stored in the Postgres database. Keep your `.env` file and `data/sessions/` directory secure and out of version control (both are in `.gitignore`)
- The bot deletes your credential message from Telegram chat history immediately after reading it
- Session state files in `data/sessions/` contain authentication tokens — do not share them
