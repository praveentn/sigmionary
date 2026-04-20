"""
game_cog.py — Sigmionary game engine + all slash commands.

All commands are grouped under /sigmionary.
Server (guild) isolation is enforced throughout: every DB query and in-memory
state lookup is keyed by guild_id.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import SlashCommandGroup
from discord.ext import commands

from utils import database as db
from utils.fuzzy_match import guess_score, is_correct_answer, THRESHOLD_HOT, THRESHOLD_WARM
from utils.questions import load_questions

log = logging.getLogger("sigmionary")

# ── Tuning constants ──────────────────────────────────────────────────────────
HINT_INTERVAL   = 20      # seconds to display each hint before revealing the next
BETWEEN_Q_DELAY = 4       # seconds between answer reveal and next question prompt
SPEED_BONUS_MAX = 30      # extra points awarded for answering instantly
BASE_POINTS     = {1: 100, 2: 70, 3: 40}   # points per hint level

# streak → multiplier mapping (threshold, multiplier), checked in order
_STREAK_TIERS = [(5, 2.0), (3, 1.5), (2, 1.2)]


def _streak_multiplier(streak: int) -> float:
    for threshold, mult in _STREAK_TIERS:
        if streak >= threshold:
            return mult
    return 1.0


def _calc_points(hint_level: int, elapsed: float, streak: int) -> int:
    base         = BASE_POINTS.get(hint_level, 30)
    speed_bonus  = max(0, int(SPEED_BONUS_MAX * (1.0 - min(elapsed / HINT_INTERVAL, 1.0))))
    multiplier   = _streak_multiplier(streak)
    return max(1, int((base + speed_bonus) * multiplier))


# ── Per-guild game state ───────────────────────────────────────────────────────

@dataclass
class _GameState:
    active:           bool            = False
    channel_id:       int | None      = None
    session_id:       int | None      = None
    questions:        list            = field(default_factory=list)
    current_idx:      int             = -1
    current_q:        dict | None     = None
    hint_level:       int             = 0      # 0 = none shown yet
    hint_task:        asyncio.Task | None = None
    hint_start_time:  float           = 0.0
    q_answered:       bool            = False  # True after first correct answer
    session_scores:   dict            = field(default_factory=dict)  # uid → pts
    streaks:          dict            = field(default_factory=dict)  # uid → count
    q_token:          int             = 0      # incremented each question; prevents stale callbacks


# ── Cog ───────────────────────────────────────────────────────────────────────

class GameCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot       = bot
        self._questions: list[dict] = load_questions()
        self._states:   dict[int, _GameState]    = {}
        self._locks:    dict[int, asyncio.Lock]  = {}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _state(self, guild_id: int) -> _GameState:
        if guild_id not in self._states:
            self._states[guild_id] = _GameState()
        return self._states[guild_id]

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    # ── Slash command group ───────────────────────────────────────────────────

    sigmionary = SlashCommandGroup("sigmionary", "Sigmionary pictionary game commands")

    # /sigmionary start ───────────────────────────────────────────────────────

    @sigmionary.command(name="start", description="Start a Sigmionary game in this channel")
    @discord.option("rounds", description="Number of rounds to play (0 = all questions)", input_type=int, required=False, default=0)
    async def cmd_start(self, ctx: discord.ApplicationContext, rounds: int = 0) -> None:
        guild_id = ctx.guild_id
        state    = self._state(guild_id)

        if state.active:
            await ctx.respond(
                "A game is already running here! Use `/sigmionary stop` to end it.",
                ephemeral=True,
            )
            return

        if not self._questions:
            await ctx.respond("No questions found. Check the `questions/` folder.", ephemeral=True)
            return

        pool = list(self._questions)
        random.shuffle(pool)
        if rounds and 0 < rounds < len(pool):
            pool = pool[:rounds]

        # Initialise state
        state.active         = True
        state.channel_id     = ctx.channel_id
        state.questions      = pool
        state.current_idx    = -1
        state.current_q      = None
        state.session_scores = {}
        state.streaks        = {}
        state.q_answered     = False
        state.q_token        = 0
        state.session_id     = await db.create_session(guild_id, ctx.author.id)

        embed = discord.Embed(
            title="Sigmionary — Game Starting!",
            description=(
                f"**{len(pool)} round(s)** | Just type your answers in chat — no commands needed!\n\n"
                "**How scoring works**\n"
                "Hint 1 correct → **100 pts** + speed bonus (up to +30)\n"
                "Hint 2 correct → **70 pts** + speed bonus\n"
                "Hint 3 correct → **40 pts**\n\n"
                "**Streak multipliers** — consecutive correct answers:\n"
                "2 streak → **×1.2** | 3 streak → **×1.5** | 5 streak → **×2.0**\n\n"
                "Get ready — first question dropping in **3 seconds!**"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Game started by {ctx.author.display_name}")
        await ctx.respond(embed=embed)

        await asyncio.sleep(3)
        await self._next_question(ctx.channel, guild_id)

    # /sigmionary stop ────────────────────────────────────────────────────────

    @sigmionary.command(name="stop", description="Stop the current game (Manage Server required)")
    async def cmd_stop(self, ctx: discord.ApplicationContext) -> None:
        guild_id = ctx.guild_id
        state    = self._state(guild_id)

        if not state.active:
            await ctx.respond("No game is currently running.", ephemeral=True)
            return

        if not ctx.author.guild_permissions.manage_guild:
            await ctx.respond(
                "You need the **Manage Server** permission to stop a game.", ephemeral=True
            )
            return

        await ctx.respond("Game stopped by a moderator.")
        await self._end_game(ctx.channel, guild_id, forced=True)

    # /sigmionary skip ────────────────────────────────────────────────────────

    @sigmionary.command(name="skip", description="Skip the current question (Manage Server required)")
    async def cmd_skip(self, ctx: discord.ApplicationContext) -> None:
        guild_id = ctx.guild_id
        state    = self._state(guild_id)

        if not state.active:
            await ctx.respond("No game is currently running.", ephemeral=True)
            return

        if not ctx.author.guild_permissions.manage_guild:
            await ctx.respond(
                "You need the **Manage Server** permission to skip questions.", ephemeral=True
            )
            return

        item = state.current_q["item"] if state.current_q else "?"
        await ctx.respond(f"Skipping — the answer was **{item}**.")
        await self._advance(ctx.channel, guild_id, state.q_token)

    # /sigmionary score ───────────────────────────────────────────────────────

    @sigmionary.command(name="score", description="Show scores for the current game session")
    async def cmd_score(self, ctx: discord.ApplicationContext) -> None:
        guild_id = ctx.guild_id
        state    = self._state(guild_id)

        if not state.active:
            await ctx.respond("No game is currently running.", ephemeral=True)
            return

        if not state.session_scores:
            await ctx.respond("No one has scored yet — keep guessing!", ephemeral=True)
            return

        lines  = _format_scoreboard(ctx.guild, state.session_scores, state.streaks)
        q_done = max(state.current_idx, 0)
        embed  = discord.Embed(
            title="Current Session Scores",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"After {q_done}/{len(state.questions)} question(s)")
        await ctx.respond(embed=embed)

    # /sigmionary leaderboard ─────────────────────────────────────────────────

    @sigmionary.command(name="leaderboard", description="All-time leaderboard for this server")
    async def cmd_leaderboard(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        rows = await db.get_leaderboard(ctx.guild_id, limit=10)

        if not rows:
            await ctx.respond("No scores recorded yet — start a game with `/sigmionary start`!")
            return

        medals  = ["🥇", "🥈", "🥉"]
        lines   = []
        for i, row in enumerate(rows):
            medal  = medals[i] if i < 3 else f"**{i + 1}.**"
            member = ctx.guild.get_member(row["user_id"])
            name   = member.display_name if member else f"<@{row['user_id']}>"
            acc    = (
                f"{row['total_correct']/max(row['total_correct'], 1)*100:.0f}%"
                if row["total_correct"]
                else "—"
            )
            lines.append(
                f"{medal} **{name}** — {row['total_points']:,} pts "
                f"| {row['total_correct']} correct | best streak: {row['best_streak']} | games: {row['games_played']}"
            )

        embed = discord.Embed(
            title=f"Sigmionary Leaderboard — {ctx.guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.respond(embed=embed)

    # /sigmionary stats ───────────────────────────────────────────────────────

    @sigmionary.command(name="stats", description="View your stats (or another player's)")
    @discord.option("user", description="Player to look up (default: yourself)", input_type=discord.Member, required=False)
    async def cmd_stats(self, ctx: discord.ApplicationContext, user: discord.Member = None) -> None:
        await ctx.defer()
        target   = user or ctx.author
        guild_id = ctx.guild_id

        stats = await db.get_user_stats(guild_id, target.id)
        if not stats:
            msg = (
                "You haven't played yet — start a game with `/sigmionary start`!"
                if target == ctx.author
                else f"**{target.display_name}** hasn't played here yet."
            )
            await ctx.respond(msg, ephemeral=True)
            return

        rank = await db.get_user_rank(guild_id, target.id)
        acc  = (
            f"{stats['total_correct'] / stats['games_played']:.1f} correct/game"
            if stats["games_played"]
            else "—"
        )

        embed = discord.Embed(
            title=f"Stats — {target.display_name}",
            color=target.color if target.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Server Rank",     value=f"#{rank}",                       inline=True)
        embed.add_field(name="Total Points",    value=f"{stats['total_points']:,}",      inline=True)
        embed.add_field(name="Games Played",    value=str(stats["games_played"]),        inline=True)
        embed.add_field(name="Correct Answers", value=str(stats["total_correct"]),       inline=True)
        embed.add_field(name="Best Streak",     value=str(stats["best_streak"]),         inline=True)
        embed.add_field(name="Avg per Game",    value=acc,                               inline=True)
        await ctx.respond(embed=embed)

    # /sigmionary help ────────────────────────────────────────────────────────

    @sigmionary.command(name="help", description="How to play Sigmionary")
    async def cmd_help(self, ctx: discord.ApplicationContext) -> None:
        embed = discord.Embed(
            title="How to Play Sigmionary",
            color=discord.Color.purple(),
        )
        embed.add_field(
            name="What is it?",
            value=(
                "A picture-guessing game! A series of images are shown as clues. "
                "Guess the place, word, or item they represent."
            ),
            inline=False,
        )
        embed.add_field(
            name="How to answer",
            value=(
                "**Just type your answer in chat** — no slash command needed. "
                "Spelling variations and typos are handled automatically."
            ),
            inline=False,
        )
        embed.add_field(
            name="Commands",
            value=(
                "`/sigmionary start [rounds]` — Start a game\n"
                "`/sigmionary stop` — Stop the game *(Manage Server)*\n"
                "`/sigmionary skip` — Skip question *(Manage Server)*\n"
                "`/sigmionary score` — Session scores\n"
                "`/sigmionary leaderboard` — All-time leaderboard\n"
                "`/sigmionary stats [user]` — Player stats\n"
                "`/sigmionary help` — This message"
            ),
            inline=False,
        )
        embed.add_field(
            name="Scoring",
            value=(
                "**Hint 1:** 100 pts + speed bonus (up to +30)\n"
                "**Hint 2:** 70 pts + speed bonus\n"
                "**Hint 3:** 40 pts\n"
                "**Streak multipliers:** 2x→×1.2 | 3x→×1.5 | 5x→×2.0\n"
                "*Streaks reset if you miss a question!*"
            ),
            inline=False,
        )
        await ctx.respond(embed=embed, ephemeral=True)

    # ── Game-flow internals ───────────────────────────────────────────────────

    async def _next_question(self, channel: discord.TextChannel, guild_id: int) -> None:
        state = self._state(guild_id)
        if not state.active:
            return

        state.current_idx += 1
        if state.current_idx >= len(state.questions):
            await self._end_game(channel, guild_id, forced=False)
            return

        q               = state.questions[state.current_idx]
        state.current_q = q
        state.hint_level    = 0
        state.q_answered    = False
        state.hint_start_time = 0.0
        state.q_token      += 1
        token = state.q_token

        n_total = len(state.questions)
        n_hints = len(q["images"])

        embed = discord.Embed(
            title=f"Question {state.current_idx + 1} / {n_total}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Category",     value=q["category"],    inline=True)
        embed.add_field(name="Sub-category", value=q["subcategory"], inline=True)
        embed.add_field(name="Hints",        value=f"{n_hints} image(s) — revealed one at a time", inline=False)
        embed.set_footer(text="Type your answer in chat!")
        await channel.send(embed=embed)

        await asyncio.sleep(2)

        # Kick off hint-reveal loop as a background task
        if state.hint_task and not state.hint_task.done():
            state.hint_task.cancel()
        state.hint_task = asyncio.create_task(
            self._hint_loop(channel, guild_id, token)
        )

    async def _hint_loop(
        self, channel: discord.TextChannel, guild_id: int, token: int
    ) -> None:
        """Reveal images one by one (side-by-side), then trigger timeout if unanswered."""
        state    = self._state(guild_id)
        q        = state.current_q
        revealed: list[Path] = []

        for idx, img_path in enumerate(q["images"]):
            if not state.active or state.q_token != token:
                return

            revealed.append(img_path)
            state.hint_level      = idx + 1
            state.hint_start_time = time.time()

            await self._send_hints(channel, q, revealed, state.hint_level, len(q["images"]))

            try:
                await asyncio.sleep(HINT_INTERVAL)
            except asyncio.CancelledError:
                return

        # All hints exhausted — no one answered
        if state.active and state.q_token == token and not state.q_answered:
            await self._on_timeout(channel, guild_id, token)

    async def _send_hints(
        self,
        channel: discord.TextChannel,
        q: dict,
        revealed: list[Path],
        hint_num: int,
        total_hints: int,
    ) -> None:
        """Send all revealed images stitched side-by-side in a single embed."""
        embed = discord.Embed(
            description=f"**Category:** {q['category']}  |  **Sub-category:** {q['subcategory']}",
            color=discord.Color.og_blurple(),
        )
        embed.set_author(name=f"Hint {hint_num} / {total_hints}")
        embed.set_footer(text=f"You have {HINT_INTERVAL}s — type your answer in chat!")

        buf = _stitch_images(revealed)
        if buf:
            embed.set_image(url="attachment://hints.png")
            await channel.send(file=discord.File(buf, filename="hints.png"), embed=embed)
            return

        # Fallback: send each image as a separate attachment in one message
        try:
            handles = [open(p, "rb") for p in revealed]
            files   = [discord.File(h, filename=p.name) for h, p in zip(handles, revealed)]
            await channel.send(files=files, embed=embed)
            for h in handles:
                h.close()
        except Exception as exc:
            log.error("Failed to send hint images: %s", exc)
            await channel.send(embed=discord.Embed(
                description="*(Images unavailable)*", color=discord.Color.greyple()
            ))

    async def _on_timeout(
        self, channel: discord.TextChannel, guild_id: int, token: int
    ) -> None:
        state = self._state(guild_id)
        if not state.active or state.q_token != token:
            return

        q = state.current_q
        # Reset streaks for all participants who didn't answer this question
        for uid in list(state.session_scores):
            state.streaks[uid] = 0

        embed = discord.Embed(
            title=f"Time's up! The answer was **{q['item']}**",
            description=f"{q['category']} → {q['subcategory']}",
            color=discord.Color.red(),
        )
        await channel.send(embed=embed)

        await asyncio.sleep(BETWEEN_Q_DELAY)
        await self._advance(channel, guild_id, token)

    async def _advance(
        self, channel: discord.TextChannel, guild_id: int, token: int
    ) -> None:
        """Cancel hint task and move to next question, guarded by token."""
        state = self._state(guild_id)
        if not state.active or state.q_token != token:
            return
        if state.hint_task and not state.hint_task.done():
            state.hint_task.cancel()
        await self._next_question(channel, guild_id)

    async def _end_game(
        self, channel: discord.TextChannel, guild_id: int, forced: bool
    ) -> None:
        state = self._state(guild_id)

        if state.hint_task and not state.hint_task.done():
            state.hint_task.cancel()

        q_done = state.current_idx + (0 if forced else 0)
        if state.session_id:
            await db.end_session(state.session_id, q_done)

        participants = list(state.session_scores)
        if participants:
            await db.increment_games_played(guild_id, participants)

        if state.session_scores:
            lines = _format_scoreboard(channel.guild, state.session_scores, state.streaks)
            desc  = "\n".join(lines)
        else:
            desc = "No one scored this game — better luck next time!"

        embed = discord.Embed(
            title="Game Over — Final Scores",
            description=desc,
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{q_done} question(s) played")
        await channel.send(embed=embed)

        # Reset state (keeps dict entry so next game can start cleanly)
        state.active         = False
        state.channel_id     = None
        state.session_id     = None
        state.current_q      = None
        state.questions      = []
        state.session_scores = {}
        state.streaks        = {}

    # ── on_message answer detection ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots, DMs, slash-command text
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        state    = self._state(guild_id)

        # Fast-path guards (no lock needed)
        if not state.active:
            return
        if message.channel.id != state.channel_id:
            return
        if state.current_q is None or state.hint_level == 0:
            return
        if state.q_answered:
            return

        answer = state.current_q["item"]
        score  = guess_score(message.content, answer)

        # ── Near-miss feedback — react to tell the player how close they are ──
        if score < THRESHOLD_HOT:
            if score >= THRESHOLD_WARM:
                # In the right ballpark but not close enough
                try:
                    await message.add_reaction("🌡️")
                except discord.HTTPException:
                    pass
            return  # not correct, nothing more to do

        if score < 82:  # HOT but not correct yet
            try:
                await message.add_reaction("🔥")
            except discord.HTTPException:
                pass
            return

        # ── Correct! acquire lock to prevent simultaneous accepts ─────────────
        async with self._lock(guild_id):
            if state.q_answered:      # another message beat us
                return
            if not state.active or state.current_q is None:
                return

            state.q_answered = True
            token   = state.q_token
            uid     = message.author.id
            elapsed = time.time() - state.hint_start_time

            streak              = state.streaks.get(uid, 0) + 1
            state.streaks[uid]  = streak
            points              = _calc_points(state.hint_level, elapsed, streak)
            state.session_scores[uid] = state.session_scores.get(uid, 0) + points

            # Reset everyone else's streak
            for other_uid in list(state.session_scores):
                if other_uid != uid:
                    state.streaks[other_uid] = 0

            # Persist asynchronously so we don't block the lock
            asyncio.create_task(
                db.record_answer(
                    state.session_id, guild_id, uid, answer,
                    points, state.hint_level, elapsed, streak,
                )
            )

            mult       = _streak_multiplier(streak)
            mult_str   = f" ×{mult:.1f}" if mult > 1.0 else ""
            streak_str = f"  |  🔥 {streak}x streak!" if streak >= 2 else ""
            total      = state.session_scores[uid]

            embed = discord.Embed(
                title=f"{message.author.display_name} got it!",
                description=(
                    f"**{answer}**\n"
                    f"+**{points} pts**{mult_str}  |  Session total: **{total:,} pts**{streak_str}"
                ),
                color=discord.Color.green(),
            )
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            await message.channel.send(embed=embed)

            # Cancel hint loop
            if state.hint_task and not state.hint_task.done():
                state.hint_task.cancel()

        # Advance outside the lock (we don't want to hold it over I/O)
        await asyncio.sleep(BETWEEN_Q_DELAY)
        await self._advance(message.channel, guild_id, token)


# ── Image stitching ───────────────────────────────────────────────────────────

_TARGET_HEIGHT = 300   # px — all images normalised to this height
_GAP           = 12    # px — gap between images
_BG_COLOR      = (47, 49, 54)  # Discord dark-mode background


def _stitch_images(paths: list[Path]) -> io.BytesIO | None:
    """
    Resize each image to _TARGET_HEIGHT (preserving aspect ratio) then place
    them side-by-side on a single canvas.  Returns a PNG BytesIO, or None if
    Pillow is unavailable or any error occurs (caller falls back to individual files).
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow not installed — falling back to individual image files.")
        return None

    try:
        frames: list[Image.Image] = []
        for p in paths:
            with Image.open(p) as raw:
                img = raw.convert("RGBA")
                ratio   = _TARGET_HEIGHT / img.height
                new_w   = max(1, int(img.width * ratio))
                frames.append(img.resize((new_w, _TARGET_HEIGHT), Image.LANCZOS))

        total_w = sum(f.width for f in frames) + _GAP * (len(frames) - 1)
        canvas  = Image.new("RGBA", (total_w, _TARGET_HEIGHT), (*_BG_COLOR, 255))

        x = 0
        for frame in frames:
            canvas.paste(frame, (x, 0), frame)   # alpha-aware paste
            x += frame.width + _GAP

        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, "PNG", optimize=True)
        buf.seek(0)
        return buf

    except Exception as exc:
        log.warning("_stitch_images failed: %s", exc)
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

_MEDALS = ["🥇", "🥈", "🥉"]


def _format_scoreboard(
    guild: discord.Guild,
    scores: dict[int, int],
    streaks: dict[int, int],
) -> list[str]:
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    lines  = []
    for i, (uid, pts) in enumerate(ranked[:10]):
        medal  = _MEDALS[i] if i < 3 else f"**{i + 1}.**"
        member = guild.get_member(uid)
        name   = member.display_name if member else f"<@{uid}>"
        streak = streaks.get(uid, 0)
        s_str  = f"  🔥{streak}" if streak >= 2 else ""
        lines.append(f"{medal} **{name}** — {pts:,} pts{s_str}")
    return lines


def setup(bot: discord.Bot) -> None:
    bot.add_cog(GameCog(bot))
