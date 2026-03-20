# OnSinch Shift Notifier Bot

Get instant Telegram notifications whenever a new shift appears on Splendid's OnSinch platform — no more checking manually.

The bot checks every 15 minutes and only messages you about shifts you haven't seen before.

---

## Getting started

### 1. Open a chat with the bot

Find the bot on Telegram and tap **Start**, or send `/start`.

### 2. Send your OnSinch credentials

When prompted, send your login details in this format:

```
yourname@example.com:yourpassword
```

Your message is deleted immediately after the bot reads it. The bot will log in and confirm your credentials are working before saving anything.

### 3. That's it

Once confirmed, you'll receive a message automatically every time a new shift appears. You don't need to do anything else.

---

## Commands

| Command | What it does |
|---|---|
| `/start` | Register, or update your credentials if your password has changed |
| `/stop` | Pause notifications (you can resume any time with `/start`) |
| `/check` | See any new shifts available right now that you haven't been notified about yet |
| `/status` | Check whether the bot is active and how many listings it's tracking for you |
| `/help` | Show all available commands |

---

## What the notifications look like

Each new shift arrives as its own message. It includes:

- Shift name and event
- Your role and profession (e.g. Waiting Staff — Food)
- Date and time, shown in UK local time
- Location
- Number of spots available
- A link to view and apply on OnSinch

Special labels:

- ⭐ **Featured** — highlighted shifts from the organiser
- ⚠️ **Conflict** — this shift overlaps with something already in your schedule
- 🎖️ **Team Leader** — positions with a leadership role
- ⚠️ **Requirements** — you may not meet the eligibility criteria for this shift

---

## Troubleshooting

**"Login failed" when I send my credentials**
- Check for typos — make sure there are no spaces before or after your email or password
- If your password contains a colon (`:`), don't worry — everything after the first `:` is treated as the password
- Occasionally the login page shows a reCAPTCHA challenge that blocks the bot. Wait a few minutes and try again

**I stopped getting notifications**
- Send `/status` to check if the bot is still active
- If you see an auth failure warning in Telegram, your OnSinch password may have changed — use `/start` to re-enter your credentials
- The bot handles session expiry automatically, so you normally don't need to do anything

**`/check` says "No new shifts" but I can see shifts on OnSinch**
- `/check` only shows shifts you haven't been notified about yet. If the bot already sent you a notification for a shift, it won't show it again here

---

## Privacy

- Your email and password are stored in an encrypted database on the server running the bot
- Your credential message is deleted from Telegram immediately after the bot reads it
- Session tokens are stored locally on the server and are never shared
