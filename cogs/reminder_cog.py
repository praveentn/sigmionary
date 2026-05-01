"""
reminder_cog.py — Daily 7 AM reminder system for Sigmionary.

Admin commands (all require Manage Server):
  /remind channel #channel   — set the target channel
  /remind timezone <IANA>    — set timezone; bot confirms current local time
  /remind status             — view current config
  /remind test               — fire a test reminder right now
  /remind off                — disable reminders

Scheduling:
  A background task fires every 60 s and sends the reminder during the
  07:00–07:59 local hour if it hasn't fired today.  The one-hour grace window
  means the reminder is delivered even after brief bot restarts.

Security:
  All data is scoped to guild_id. No config from one server is readable by
  another. Only members with Manage Server can change reminder settings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import SlashCommandGroup
from discord.ext import commands, tasks

from utils import database as db
from utils.categoryhistory import get_history_fact
from cogs.game_cog import PlayNowView

log = logging.getLogger("sigmionary")

# Discord hard limit is 2 000 chars; keep well under to leave room for mentions.
_MAX_MENTION_CHARS = 1_800
_PERM_MSG = "You need the **Manage Server** permission to configure reminders."


class ReminderCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._daily_reminder.start()

    def cog_unload(self) -> None:
        self._daily_reminder.cancel()

    # ── Slash command group ───────────────────────────────────────────────────

    remind = SlashCommandGroup(
        "remind",
        "Configure daily Sigmionary reminders (requires Manage Server)",
    )

    # /remind channel ─────────────────────────────────────────────────────────

    @remind.command(name="channel", description="Set the channel for daily 7 AM reminders")
    @discord.option("channel", description="Channel to send reminders in", input_type=discord.TextChannel)
    async def cmd_channel(
        self, ctx: discord.ApplicationContext, channel: discord.TextChannel
    ) -> None:
        if not _has_manage_guild(ctx):
            await ctx.respond(_PERM_MSG, ephemeral=True)
            return

        # Validate the bot can actually post there
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            await ctx.respond(
                f"I don't have **Send Messages** + **Embed Links** permissions in {channel.mention}. "
                "Please grant those first.",
                ephemeral=True,
            )
            return

        await db.set_reminder_channel(ctx.guild_id, channel.id)
        await ctx.respond(
            f"✅ Daily reminders will go to {channel.mention}.\n"
            "Make sure to also set `/remind timezone` so the 7 AM time is correct for your server.\n"
            "Run `/remind test` any time to verify the setup.",
            ephemeral=True,
        )

    # /remind timezone ────────────────────────────────────────────────────────

    @remind.command(
        name="timezone",
        description="Set the server timezone for 7 AM daily reminders (IANA name, e.g. Asia/Kolkata)",
    )
    @discord.option("zone", description="IANA timezone name (e.g. Asia/Kolkata, America/New_York)", input_type=str)
    async def cmd_timezone(
        self, ctx: discord.ApplicationContext, zone: str
    ) -> None:
        if not _has_manage_guild(ctx):
            await ctx.respond(_PERM_MSG, ephemeral=True)
            return

        zone = zone.strip()
        try:
            tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, KeyError):
            await ctx.respond(
                f"❌ `{zone}` is not a valid IANA timezone name.\n"
                "Examples: `Asia/Kolkata` · `America/New_York` · `Europe/London` · `Australia/Sydney`\n"
                "Full list: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>",
                ephemeral=True,
            )
            return

        now_local = datetime.now(tz)
        await db.set_reminder_timezone(ctx.guild_id, zone)
        await db.enable_reminder(ctx.guild_id)
        await ctx.respond(
            f"✅ Timezone set to **{zone}**.\n"
            f"Current local time: **{now_local.strftime('%I:%M %p')} ({zone})**\n"
            f"Reminders will fire at **7:00 AM** every day.",
            ephemeral=True,
        )

    # /remind status ──────────────────────────────────────────────────────────

    @remind.command(name="status", description="Check the current reminder configuration")
    async def cmd_status(self, ctx: discord.ApplicationContext) -> None:
        config = await db.get_reminder_config(ctx.guild_id)
        if not config:
            await ctx.respond(
                "No reminder is configured for this server.\n"
                "Run `/remind channel` to set it up.",
                ephemeral=True,
            )
            return

        ch = ctx.guild.get_channel(config["channel_id"])
        ch_str = ch.mention if ch else f"<#{config['channel_id']}> *(channel not found — reconfigure)*"
        tz_str  = config["timezone"]
        enabled = config["enabled"] and config["channel_id"] > 0

        last_date = config["last_reminded_on"]
        last_str  = last_date.isoformat() if last_date else "Never"

        try:
            now_local = datetime.now(ZoneInfo(tz_str))
            local_str = now_local.strftime("%I:%M %p %Z")
        except Exception:
            local_str = "unknown"

        embed = discord.Embed(
            title="📅 Reminder Configuration",
            color=discord.Color.blurple() if enabled else discord.Color.greyple(),
        )
        embed.add_field(name="Status",            value="✅ Active" if enabled else "❌ Disabled", inline=True)
        embed.add_field(name="Channel",           value=ch_str,                                    inline=True)
        embed.add_field(name="Timezone",          value=tz_str,                                    inline=True)
        embed.add_field(name="Fires at",          value="7:00 AM local time",                      inline=True)
        embed.add_field(name="Current local time", value=local_str,                                inline=True)
        embed.add_field(name="Last sent",         value=last_str,                                  inline=True)
        embed.set_footer(text="Use /remind off to disable · /remind test to preview")
        await ctx.respond(embed=embed, ephemeral=True)

    # /remind test ────────────────────────────────────────────────────────────

    @remind.command(name="test", description="Send a test reminder right now (Manage Server)")
    async def cmd_test(self, ctx: discord.ApplicationContext) -> None:
        if not _has_manage_guild(ctx):
            await ctx.respond(_PERM_MSG, ephemeral=True)
            return

        config = await db.get_reminder_config(ctx.guild_id)
        if not config or not config["channel_id"]:
            await ctx.respond(
                "No channel configured. Use `/remind channel` first.",
                ephemeral=True,
            )
            return

        await ctx.respond("Sending test reminder…", ephemeral=True)
        try:
            await self._fire_reminder(
                ctx.guild_id, config["channel_id"], config["timezone"], test=True
            )
        except Exception as exc:
            log.error("Test reminder failed for guild %d: %s", ctx.guild_id, exc, exc_info=True)
            await ctx.followup.send(
                f"⚠️ Test reminder failed: `{exc}`\n"
                "Check that I have **Send Messages** and **Embed Links** permissions in the target channel.",
                ephemeral=True,
            )

    # /remind off ─────────────────────────────────────────────────────────────

    @remind.command(name="off", description="Disable daily reminders for this server")
    async def cmd_off(self, ctx: discord.ApplicationContext) -> None:
        if not _has_manage_guild(ctx):
            await ctx.respond(_PERM_MSG, ephemeral=True)
            return

        await db.disable_reminder(ctx.guild_id)
        await ctx.respond("✅ Daily reminders disabled.", ephemeral=True)

    # ── Background scheduler ──────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def _daily_reminder(self) -> None:
        try:
            configs = await db.get_all_reminder_configs()
        except Exception as exc:
            log.error("Reminder loop: failed to fetch configs: %s", exc)
            return

        now_utc = datetime.now(timezone.utc)

        for cfg in configs:
            guild_id   = cfg["guild_id"]
            channel_id = cfg["channel_id"]
            tz_name    = cfg["timezone"]

            try:
                tz        = ZoneInfo(tz_name)
                now_local = now_utc.astimezone(tz)

                # Grace window: send any time between 07:00 and 07:59 if not sent today.
                if now_local.hour != 7:
                    continue

                today     = now_local.date()
                last_sent = cfg.get("last_reminded_on")  # datetime.date or None

                if last_sent is not None and last_sent >= today:
                    continue  # already delivered today

                await self._fire_reminder(guild_id, channel_id, tz_name)
                await db.mark_reminder_sent(guild_id, today)

            except Exception as exc:
                log.error(
                    "Reminder loop: error for guild %s: %s", guild_id, exc, exc_info=True
                )

    @_daily_reminder.before_loop
    async def _before_daily_reminder(self) -> None:
        await self.bot.wait_until_ready()

    # ── Core reminder sender ──────────────────────────────────────────────────

    async def _fire_reminder(
        self,
        guild_id: int,
        channel_id: int,
        tz_name: str,
        test: bool = False,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            log.warning("Reminder: guild %d not cached — skipping", guild_id)
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.warning(
                "Reminder: channel %d not found or not a text channel in guild %d",
                channel_id, guild_id,
            )
            return

        # Resolve timezone
        try:
            tz        = ZoneInfo(tz_name)
            now_local = datetime.now(tz)
        except Exception:
            now_local = datetime.now(timezone.utc)

        # Today's historical fact
        fact = get_history_fact(now_local.month, now_local.day)

        # Top player for competitive FOMO
        top_rows = await db.get_leaderboard(guild_id, limit=1)
        top_info: dict | None = None
        if top_rows:
            top_member = guild.get_member(top_rows[0]["user_id"])
            if top_member:
                top_info = {
                    "name":   top_member.display_name,
                    "points": top_rows[0]["total_points"],
                }

        # Collect all players who've played (server-scoped)
        player_ids = await db.get_player_ids(guild_id)
        mentions: list[str] = []
        for uid in player_ids:
            member = guild.get_member(uid)
            if member and not member.bot:
                mentions.append(member.mention)

        # Build the embed
        embed = _build_embed(fact, top_info, now_local, test)

        # Build the Play Now button view (requires the GameCog to be loaded)
        game_cog = self.bot.cogs.get("GameCog")
        view = PlayNowView(game_cog) if game_cog else None

        # Paginate and send
        await _send_paginated(channel, embed, mentions, view=view)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_manage_guild(ctx: discord.ApplicationContext) -> bool:
    return bool(ctx.author.guild_permissions.manage_guild)


def _build_embed(
    fact: str,
    top_info: dict | None,
    now_local: datetime,
    test: bool,
) -> discord.Embed:
    date_str = now_local.strftime("%A, %B %-d")  # e.g. "Saturday, April 26"

    title = f"🎮 Sigmionary Daily Challenge — {date_str}"
    if test:
        title += "  [TEST]"

    lines = [
        "**Sharpen your eyes — a new round of picture puzzles awaits!** 🧠",
        "",
        "📅 **On This Day in History:**",
        f"> *{fact}*",
        "",
    ]

    if top_info:
        lines += [
            f"🏆 **Current Champion:** **{top_info['name']}** · **{top_info['points']:,} pts**",
            "Can *you* knock them off the top? The leaderboard never sleeps. 🔥",
            "",
        ]
    else:
        lines += [
            "🏆 **No champion yet — be the first to claim the throne!**",
            "",
        ]

    lines += [
        "👇 **Hit the button below to play now — it only takes 5 minutes!**",
        "*(Earn speed bonuses, build streaks, climb the leaderboard)*",
    ]

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=0xFFD700,
    )
    embed.set_footer(text="📸 Guess the images  ·  🔥 Build streaks  ·  🏆 Top the leaderboard")
    return embed


async def _send_paginated(
    channel: discord.TextChannel,
    embed: discord.Embed,
    mentions: list[str],
    view: discord.ui.View | None = None,
) -> None:
    """
    Send the embed with all player mentions.  Splits into multiple messages
    when the mention string would exceed Discord's 2 000-character content limit.
    The embed and view appear once in the first message; subsequent pages are plain text.
    """
    if not mentions:
        await channel.send(embed=embed, view=view)
        return

    # Chunk mentions so each message stays under _MAX_MENTION_CHARS
    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for mention in mentions:
        mlen = len(mention) + 1  # +1 for the separating space
        if current_len + mlen > _MAX_MENTION_CHARS and current:
            pages.append(current)
            current     = [mention]
            current_len = mlen
        else:
            current.append(mention)
            current_len += mlen

    if current:
        pages.append(current)

    # First page: mentions + embed + view
    await channel.send(content=" ".join(pages[0]), embed=embed, view=view)

    # Overflow pages: mentions only
    for page in pages[1:]:
        await channel.send(content=" ".join(page))


def setup(bot: discord.Bot) -> None:
    bot.add_cog(ReminderCog(bot))
