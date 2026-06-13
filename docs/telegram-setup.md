# Telegram — alerts & remote control (runbook)

The infrastructure lives in [`app/notify.py`](../app/notify.py): it sends alerts
and runs a command poller that only obeys one allowed chat. Follow the four steps
below to get the commands and the dashboard **Test** button working.

Once configured, the bot supports status queries (`/summary`, `/status`,
`/pending`, `/positions`, `/kss`), remote control (`/fullauto`, `/pause`,
`/resume`, `/freeze`, `/reset`), automatic push on fills and risk events, and an
optional periodic digest.

## Step 1 — Create the bot, get the TOKEN

1. Open Telegram and chat with **@BotFather**.
2. Send `/newbot` → set a name and a username ending in `bot` (e.g. `findmy_fm_bot`).
3. BotFather returns a **token** like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx`. Keep it secret.

## Step 2 — Get the CHAT_ID (the chat that receives alerts & sends commands)

Option A (quick): chat with **@userinfobot**; it replies with your `Id` — that is your `chat_id`.

Option B (precise, also for groups/channels):

1. Send any message to the bot you just created (e.g. `hello`).
2. Open in a browser: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id": <number>}` — that number is the `chat_id` (groups are negative).

> Only **this exact chat_id** receives alerts and is allowed to send commands —
> every other chat is ignored (the auth boundary lives in
> [`app/notify.py`](../app/notify.py)).

## Step 3 — Declare it in `.env`

Add/edit in your `.env` file (do **not** commit it):

```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:AAE...     # token from BotFather
TELEGRAM_CHAT_ID=123456789              # chat_id from Step 2
TELEGRAM_POLL_INTERVAL=5                # seconds between command polls (default 5)
TELEGRAM_NOTIFY_TRADES=true             # push an alert on each fill
TELEGRAM_NOTIFY_RISK=true               # push on risk events (SL/trailing/breaker/veto)
TELEGRAM_DIGEST_HOURS=0                 # hours between periodic digest pushes (0 = off)
```

## Step 4 — Restart & verify

1. Restart the app (uvicorn). The boot log prints `notify poller started`.
2. **On the dashboard**: status bar → **TELEGRAM** → click **Test** → you should
   receive "FINDMY-FM test alert". (You can also toggle Telegram on/off here.)
3. **In Telegram**: send `/status` to the bot → you receive the automation +
   circuit-breaker status.

## Commands

| Command | Effect |
|---------|--------|
| `/summary` | Equity, cash, market value, realized/unrealized P&L. |
| `/status` | Automation status + breaker metrics (drawdown / daily-loss / consecutive-loss). |
| `/pending` | Orders waiting for approval. |
| `/positions` | Open positions. |
| `/kss` | KSS sessions. |
| `/fullauto on\|off` | Turn Full-Auto on/off (explicit alias for resume/pause). |
| `/resume` | Enable Full-Auto + scheduler. |
| `/pause` | Disable Full-Auto + scheduler. |
| `/freeze` | Freeze the breaker (blocks auto-approve; manual approval still works). |
| `/reset` | Unfreeze the breaker. |
| `/help` | List commands. |

## Automatic push

- **Trades** — an alert on each fill, gated by `TELEGRAM_NOTIFY_TRADES`.
- **Risk events** — alerts on stop-loss / trailing exits, breaker freeze, and
  Guardian vetoes, gated by `TELEGRAM_NOTIFY_RISK`.
- **Digest** — a periodic summary (equity + today's P&L + open counts) every
  `TELEGRAM_DIGEST_HOURS` hours (`0` = off).

Each push has its own kill switch so you can silence the noisy ones.

## Security

- The token is a `SecretStr` and is never logged. Never commit `.env`.
- Only the declared `TELEGRAM_CHAT_ID` can control the bot; all other chats are ignored.
