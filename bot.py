import os
import sys
import logging
import certifi

# Must be set before any SSL/network import.
# Fixes "certificate verify failed" on macOS python.org builds.
os.environ.setdefault("SSL_CERT_FILE",      certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

import asyncio
import time

import discord
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sigmionary")

TOKEN      = os.getenv("DISCORD_TOKEN", "")
PORT       = int(os.getenv("PORT", "8080"))
START_TIME = time.time()

_raw_guild   = os.getenv("DISCORD_GUILD_ID", "").strip()
DEBUG_GUILDS = [int(_raw_guild)] if _raw_guild.isdigit() else None

# Python 3.10+ no longer auto-creates an event loop at module scope.
asyncio.set_event_loop(asyncio.new_event_loop())

intents                 = discord.Intents.default()
intents.message_content = True   # Privileged intent — enable in Discord Developer Portal

bot = discord.Bot(intents=intents, debug_guilds=DEBUG_GUILDS)

COGS = [
    "cogs.game_cog",
    "cogs.reminder_cog",
]

# ── Status dashboard ──────────────────────────────────────────────────────────

_STATUS_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>Sigmionary — Bot Status</title>
  <style>
    *    {{ box-sizing:border-box; margin:0; padding:0 }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            background:#1a1a2e; color:#e0e0e0; padding:32px 16px }}
    .wrap{{ max-width:680px; margin:0 auto }}
    h1   {{ color:#7289da; font-size:1.8rem; margin-bottom:24px }}
    h2   {{ color:#99aab5; font-size:1rem; text-transform:uppercase;
            letter-spacing:.08em; margin-bottom:10px }}
    .card{{ background:#16213e; border-radius:10px; padding:18px 22px;
            margin-bottom:16px }}
    ul   {{ list-style:none; line-height:2 }}
    .ok  {{ color:#43b581 }}
    .warn{{ color:#faa61a }}
    .err {{ color:#f04747 }}
    code {{ background:#0f3460; padding:2px 6px; border-radius:4px; font-size:.88em }}
    a    {{ color:#7289da; text-decoration:none }}
    a:hover {{ text-decoration:underline }}
    footer{{ margin-top:24px; color:#555; font-size:.8rem; text-align:center }}
  </style>
</head>
<body><div class="wrap">
  <h1>🎮 Sigmionary Discord Bot</h1>
  {content}
  <footer>Auto-refreshes every 10 s &nbsp;·&nbsp;
    <a href="/health">/health JSON</a></footer>
</div></body></html>"""


async def _status_page(request):
    up     = int(time.time() - START_TIME)
    uptime = f"{up//3600}h {(up%3600)//60}m {up%60}s"

    token_set  = bool(TOKEN) and TOKEN != "your_bot_token_here"
    discord_ok = bool(bot.user)

    rows = [
        f'<li>{"✅" if token_set  else "❌"} Token  : '
        + (
            "<span class=ok>configured</span>"
            if token_set
            else "<span class=err>not set — edit <code>.env</code></span>"
        )
        + "</li>",
        f'<li>{"✅" if discord_ok else "⏳"} Discord: '
        + (
            f"<span class=ok>connected as <b>{bot.user}</b></span>"
            if discord_ok
            else "<span class=warn>not connected yet</span>"
        )
        + "</li>",
        f'<li>✅ HTTP server: <span class=ok>port {PORT}</span></li>',
        f'<li>⏱️ Uptime: {uptime}</li>',
    ]

    content = f'<div class=card><h2>Status</h2><ul>{"".join(rows)}</ul></div>'

    if discord_ok:
        content += (
            f'<div class=card><h2>Bot Info</h2><ul>'
            f'<li>🤖 {bot.user}</li>'
            f'<li>🏠 Guilds: {len(bot.guilds)}</li>'
            f'<li>📡 Latency: {round(bot.latency*1000)} ms</li>'
            f'</ul></div>'
        )
    elif not token_set:
        content += """<div class=card><h2>Setup Checklist</h2>
<ol style="padding-left:18px;line-height:2.2">
  <li>Go to <a href="https://discord.com/developers/applications">discord.com/developers</a> → New Application</li>
  <li>Bot tab → Add Bot → copy token</li>
  <li>Enable <b>Message Content Intent</b> under Privileged Gateway Intents</li>
  <li>Paste token into <code>.env</code> as <code>DISCORD_TOKEN=&lt;token&gt;</code></li>
  <li>Invite the bot with <b>bot + applications.commands</b> scopes</li>
  <li>Restart — this page refreshes automatically</li>
</ol></div>"""

    return web.Response(
        text=_STATUS_TMPL.format(content=content),
        content_type="text/html",
    )


async def _health_json(request):
    return web.json_response({
        "status":     "online",
        "discord":    str(bot.user) if bot.user else None,
        "guilds":     len(bot.guilds),
        "latency_ms": round(bot.latency * 1000, 2) if bot.user else None,
        "uptime_s":   int(time.time() - START_TIME),
        "port":       PORT,
    })


async def _run_web_server():
    app = web.Application()
    app.router.add_get("/",       _status_page)
    app.router.add_get("/health", _health_json)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Status page  →  http://localhost:%d/", PORT)
    log.info("Health JSON  →  http://localhost:%d/health", PORT)


# ── Bot events ────────────────────────────────────────────────────────────────

_ready_fired = False


@bot.event
async def on_ready():
    global _ready_fired

    log.info(
        "Discord connected: %s (ID: %s) | guilds: %d",
        bot.user, bot.user.id, len(bot.guilds),
    )

    if _ready_fired:
        log.info("Reconnected — re-syncing commands.")
        try:
            await bot.sync_commands()
        except Exception as exc:
            log.error("sync_commands() failed on reconnect: %s", exc)
        return

    _ready_fired = True

    from utils.database import init_db
    await init_db()

    try:
        await bot.sync_commands()
        synced = [c.name for c in bot.pending_application_commands]
        log.info(
            "Commands synced (%s): %s",
            f"guild {DEBUG_GUILDS[0]}" if DEBUG_GUILDS else "global",
            synced or "⚠  EMPTY — cog load may have failed silently",
        )
    except Exception as exc:
        log.error("sync_commands() FAILED: %s", exc, exc_info=True)

    # Clean up stale guild-scoped commands when running globally
    if not DEBUG_GUILDS:
        cleaned = 0
        for guild in bot.guilds:
            try:
                existing = await bot.http.get_guild_commands(bot.user.id, guild.id)
                if existing:
                    await bot.http.bulk_upsert_guild_commands(bot.user.id, guild.id, [])
                    log.info(
                        "Cleared %d stale guild command(s) from %s (%d).",
                        len(existing), guild.name, guild.id,
                    )
                    cleaned += 1
            except Exception as exc:
                log.warning("Could not clear guild commands for %s: %s", guild.name, exc)
        if cleaned:
            log.info("Stale guild-command cleanup done — %d guild(s) cleared.", cleaned)
        else:
            log.info("No stale guild commands found.")


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error):
    msg = f"An error occurred: {error}"
    try:
        if ctx.response.is_done():
            await ctx.followup.send(msg, ephemeral=True)
        else:
            await ctx.respond(msg, ephemeral=True)
    except Exception:
        pass
    raise error


# ── Load cogs ─────────────────────────────────────────────────────────────────

for cog in COGS:
    try:
        bot.load_extension(cog)
        log.info("Loaded cog: %s", cog)
    except Exception as exc:
        log.critical(
            "FAILED to load cog %s: %s — aborting startup.", cog, exc,
            exc_info=True,
        )
        sys.exit(1)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await _run_web_server()

    token_set = bool(TOKEN) and TOKEN != "your_bot_token_here"

    if not token_set:
        log.warning(
            "DISCORD_TOKEN not set — running in local-only mode. "
            "Open http://localhost:%d/ for setup instructions.",
            PORT,
        )
        await asyncio.Event().wait()
    else:
        await bot.start(TOKEN)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Shutting down — goodbye!")
    finally:
        loop.close()
