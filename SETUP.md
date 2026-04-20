# Bot Setup Guide

Step-by-step instructions for creating the Discord application, inviting the bot, and running the server.

---

## 1. Create a Discord Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and sign in.
2. Click **New Application**, give it a name (e.g. *Sigmionary*), and confirm.
3. On the left sidebar, click **Bot**.
4. Click **Add Bot** → **Yes, do it!**
5. Under **Token**, click **Reset Token** and copy the value — you will need this in step 3.

---

## 2. Enable Required Intents

Still on the **Bot** page, scroll down to **Privileged Gateway Intents** and enable:

- **Message Content Intent** — required so the bot can read players' answers in chat.

Click **Save Changes**.

---

## 3. Configure the Bot

In the project folder, copy the example environment file:

```bash
cp .env.example .env
```

Open `.env` and paste your token:

```
DISCORD_TOKEN=your_actual_token_here
```

Optional settings:

```
# Only set this during local development for instant slash-command registration.
# Remove it (or leave blank) before deploying to production.
DISCORD_GUILD_ID=your_test_server_id_here

# HTTP status page port (default: 8080)
PORT=8080
```

To find your server ID: in Discord, go to **Settings → Advanced** and enable **Developer Mode**. Then right-click your server icon and choose **Copy Server ID**.

---

## 4. Invite the Bot to Your Server

1. In the Developer Portal, go to **OAuth2 → URL Generator**.
2. Under **Scopes**, check:
   - `bot`
   - `applications.commands`
3. Under **Bot Permissions**, check:
   - `Send Messages`
   - `Attach Files`
   - `Embed Links`
   - `Read Message History`
4. Copy the generated URL, open it in a browser, and select your server.

> **Important:** the bot needs both `bot` and `applications.commands` scopes, or slash commands will not appear.

---

## 5. Run the Bot

### Mac / Linux

```bash
./start.sh
```

The script will:
- Detect your Python installation (3.10+ required)
- Create and activate a virtual environment
- Install all dependencies from `requirements.txt`
- Start the bot

### Windows

Double-click `start.bat` or run it from a terminal:

```
start.bat
```

The script does the same steps as above.

---

## 6. Verify It's Working

Once running, open the status page at:

```
http://localhost:8080/
```

It shows:
- Token status (configured / not set)
- Discord connection status and latency
- Server count and uptime

For a health-check JSON endpoint:

```
http://localhost:8080/health
```

In Discord, type `/sigmionary` — the command should appear in the autocomplete menu. If it doesn't show up immediately after the first start, wait up to **1 hour** for Discord's global command cache to update (this is Discord's own delay, not the bot's).

---

## 7. Local Development Tips

Set `DISCORD_GUILD_ID` in `.env` to your test server ID. Commands registered to a specific guild appear **instantly** (no 1-hour wait). Remove the variable before deploying to production.

```env
DISCORD_GUILD_ID=976816967161892976
```

---

## Troubleshooting

**Commands don't appear after first launch**
- Global slash commands can take up to 1 hour to propagate. Try opening Discord in a browser (bypasses the desktop client cache).
- Check the terminal logs for `Commands synced` with a non-empty list.

**`403 Forbidden` error on startup**
- The bot was invited without the `applications.commands` scope. Re-invite using the URL generated in step 4.

**`ModuleNotFoundError: No module named 'audioop'`**
- This is a Python 3.12+ compatibility issue with some Discord libraries. Sigmionary does not use voice features, so this can be safely ignored if it appears only as a warning. If it causes a crash, ensure you are on py-cord 2.6+.

**`RuntimeError: There is no current event loop`**
- Caused by Python 3.10+ behaviour. The bot already sets the event loop explicitly at startup — this error should not occur. If it does, ensure you are running `bot.py` directly and not through a wrapper that creates its own loop.

**Bot is online but answers are not being detected**
- Make sure **Message Content Intent** is enabled in the Developer Portal (step 2). Without it the bot cannot read chat messages.

---

## Deploying to Railway (or any cloud host)

1. Push the repo to GitHub.
2. Create a new Railway project → **Deploy from GitHub repo**.
3. Add environment variables in the Railway dashboard:
   - `DISCORD_TOKEN` — your bot token
   - `PORT` — Railway sets this automatically; leave it unset and the bot reads it from the environment.
   - Do **not** set `DISCORD_GUILD_ID` in production.
4. Railway will run `python bot.py` automatically (or set the start command to that).
5. The `/health` endpoint is useful for Railway's health-check configuration.
