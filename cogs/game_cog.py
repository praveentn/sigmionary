"""
game_cog.py — Sigmionary game engine + all slash commands.

All commands are grouped under /sigmionary.
Server (guild) isolation is enforced throughout: every DB query and in-memory
state lookup is keyed by guild_id.

Button UX:
  Every hint message has interactive buttons (Next Hint / Skip / Stop / Score).
  Correct-answer and timeout embeds have a "Next Question" button.
  Game-over screen has Play Again / My Stats / Leaderboard.

Bug fixes vs original:
  - _hint_loop is wrapped in try/except; any exception forces a safe advance
    instead of silently leaving the game stuck.
  - Early hint reveal via asyncio.Event eliminates the 20-second wait on request.
  - Stale hint-message buttons are disabled as soon as the question advances.
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
from utils.database import load_questions
from utils.external_leaderboard import post_points as _post_external_points
from utils.fuzzy_match import guess_score, THRESHOLD_HOT, THRESHOLD_WARM

log = logging.getLogger("sigmionary")

# ── Tuning constants ───────────────────────────────────────────────────────────
HINT_INTERVAL   = 20      # seconds between automatic hint reveals
BETWEEN_Q_DELAY = 4       # seconds between answer/timeout reveal and next question
SPEED_BONUS_MAX = 30
BASE_POINTS     = {1: 100, 2: 70, 3: 40}
_STREAK_TIERS   = [(5, 2.0), (3, 1.5), (2, 1.2)]


def _streak_multiplier(streak: int) -> float:
    for threshold, mult in _STREAK_TIERS:
        if streak >= threshold:
            return mult
    return 1.0


def _calc_points(hint_level: int, elapsed: float, streak: int) -> int:
    base        = BASE_POINTS.get(hint_level, 30)
    speed_bonus = max(0, int(SPEED_BONUS_MAX * (1.0 - min(elapsed / HINT_INTERVAL, 1.0))))
    return max(1, int((base + speed_bonus) * _streak_multiplier(streak)))


def _has_manage_guild(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )


# ── Views ──────────────────────────────────────────────────────────────────────

class HintView(discord.ui.View):
    """
    Buttons attached to every hint message during a question.
    All interactions are token-gated — a stale message's buttons silently
    no-op if the question has already moved on.
    """

    def __init__(self, cog: "GameCog", guild_id: int, q_token: int, is_last_hint: bool) -> None:
        super().__init__(timeout=HINT_INTERVAL * 4 + 60)
        self.cog          = cog
        self.guild_id     = guild_id
        self.q_token      = q_token
        self.message: discord.Message | None = None

        # Hide "Next Hint" on the final hint — there is no next hint to reveal.
        if is_last_hint:
            self.remove_item(self.next_hint_btn)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _live(self) -> bool:
        """True when this view's question is still the active one."""
        s = self.cog._state(self.guild_id)
        return s.active and s.q_token == self.q_token

    async def disable_all(self) -> None:
        for item in self.children:
            item.disabled = True
        self.stop()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def on_timeout(self) -> None:
        await self.disable_all()

    # ── buttons ──────────────────────────────────────────────────────────────

    @discord.ui.button(label="💡 Next Hint", style=discord.ButtonStyle.primary, row=0)
    async def next_hint_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not self._live():
            await interaction.response.send_message("This question has already moved on.", ephemeral=True)
            return
        state = self.cog._state(self.guild_id)
        if state.hint_level >= len(state.current_q["images"]):
            await interaction.response.send_message("All hints have already been revealed!", ephemeral=True)
            return
        button.disabled = True
        button.label    = "💡 Loading…"
        await interaction.response.edit_message(view=self)
        state.hint_early_event.set()

    @discord.ui.button(label="📊 Score", style=discord.ButtonStyle.secondary, row=0)
    async def score_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        state = self.cog._state(self.guild_id)
        if not state.session_scores:
            await interaction.response.send_message("No one has scored yet — keep guessing!", ephemeral=True)
            return
        lines  = _format_scoreboard(interaction.guild, state.session_scores, state.streaks)
        q_done = max(state.current_idx, 0)
        embed  = discord.Embed(
            title="Current Session Scores",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"After {q_done}/{len(state.questions)} question(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not _has_manage_guild(interaction):
            await interaction.response.send_message("You need **Manage Server** to skip.", ephemeral=True)
            return
        if not self._live():
            await interaction.response.send_message("This question has already moved on.", ephemeral=True)
            return
        state = self.cog._state(self.guild_id)
        item  = state.current_q["item"] if state.current_q else "?"
        await interaction.response.send_message(f"⏭️ Skipping — the answer was **{item}**.")
        await self.disable_all()
        await self.cog._advance(interaction.channel, self.guild_id, self.q_token)

    @discord.ui.button(label="🛑 Stop", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not _has_manage_guild(interaction):
            await interaction.response.send_message("You need **Manage Server** to stop.", ephemeral=True)
            return
        state = self.cog._state(self.guild_id)
        if not state.active:
            await interaction.response.send_message("No game is currently running.", ephemeral=True)
            return
        await interaction.response.send_message("🛑 Game stopped.")
        await self.disable_all()
        await self.cog._end_game(interaction.channel, self.guild_id, forced=True)


class NextQuestionView(discord.ui.View):
    """
    'Next Question' button shown on correct-answer and timeout embeds.
    Clicking skips the automatic BETWEEN_Q_DELAY wait.
    """

    def __init__(self, cog: "GameCog", guild_id: int, q_token: int) -> None:
        super().__init__(timeout=BETWEEN_Q_DELAY + 10)
        self.cog      = cog
        self.guild_id = guild_id
        self.q_token  = q_token
        self.message: discord.Message | None = None

    async def _disable(self) -> None:
        for item in self.children:
            item.disabled = True
        self.stop()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def on_timeout(self) -> None:
        await self._disable()

    @discord.ui.button(label="▶️ Next Question", style=discord.ButtonStyle.success)
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        button.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.cog._advance(interaction.channel, self.guild_id, self.q_token)


class PostGameView(discord.ui.View):
    """Buttons shown after a game ends."""

    def __init__(self, cog: "GameCog") -> None:
        super().__init__(timeout=600)
        self.cog = cog

    @discord.ui.button(label="🎮 Play Again", style=discord.ButtonStyle.success)
    async def play_again(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        state = self.cog._state(interaction.guild_id)
        if state.active:
            await interaction.response.send_message("A game is already running here!", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._start_game_in_channel(
            interaction.channel, interaction.guild_id,
            rounds=0, started_by=interaction.user.display_name, author_id=interaction.user.id,
        )

    @discord.ui.button(label="📊 My Stats", style=discord.ButtonStyle.secondary)
    async def my_stats(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user  = interaction.user
        stats = await db.get_user_stats(interaction.guild_id, user.id)
        if not stats:
            await interaction.followup.send(
                "You haven't played yet — start a game to get on the board!", ephemeral=True
            )
            return
        rank = await db.get_user_rank(interaction.guild_id, user.id)
        acc  = (
            f"{stats['total_correct'] / stats['games_played']:.1f} correct/game"
            if stats["games_played"] else "—"
        )
        embed = discord.Embed(
            title=f"Stats — {user.display_name}",
            color=user.color if user.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Server Rank",     value=f"#{rank}",                    inline=True)
        embed.add_field(name="Total Points",    value=f"{stats['total_points']:,}",  inline=True)
        embed.add_field(name="Games Played",    value=str(stats["games_played"]),    inline=True)
        embed.add_field(name="Correct Answers", value=str(stats["total_correct"]),   inline=True)
        embed.add_field(name="Best Streak",     value=str(stats["best_streak"]),     inline=True)
        embed.add_field(name="Avg per Game",    value=acc,                           inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🏆 Leaderboard", style=discord.ButtonStyle.secondary)
    async def leaderboard(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await db.get_leaderboard(interaction.guild_id, limit=10)
        if not rows:
            await interaction.followup.send(
                "No scores yet — play a game to get on the board!", ephemeral=True
            )
            return
        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, row in enumerate(rows):
            medal  = medals[i] if i < 3 else f"**{i + 1}.**"
            member = interaction.guild.get_member(row["user_id"])
            name   = member.display_name if member else f"<@{row['user_id']}>"
            lines.append(
                f"{medal} **{name}** — {row['total_points']:,} pts "
                f"| {row['total_correct']} correct | best streak: {row['best_streak']} | games: {row['games_played']}"
            )
        embed = discord.Embed(
            title=f"Sigmionary Leaderboard — {interaction.guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class PlayNowView(discord.ui.View):
    """Single 'Play Now' button for daily reminder messages."""

    def __init__(self, cog: "GameCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🎮 Play Now", style=discord.ButtonStyle.success)
    async def play_now(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        state = self.cog._state(interaction.guild_id)
        if state.active:
            await interaction.response.send_message("A game is already running here!", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._start_game_in_channel(
            interaction.channel, interaction.guild_id,
            rounds=0, started_by=interaction.user.display_name, author_id=interaction.user.id,
        )


# ── Per-guild game state ───────────────────────────────────────────────────────

@dataclass
class _GameState:
    active:            bool               = False
    channel_id:        int | None         = None
    session_id:        int | None         = None
    questions:         list               = field(default_factory=list)
    current_idx:       int                = -1
    current_q:         dict | None        = None
    hint_level:        int                = 0
    hint_task:         asyncio.Task | None = None
    hint_start_time:   float              = 0.0
    q_answered:        bool               = False
    session_scores:    dict               = field(default_factory=dict)
    streaks:           dict               = field(default_factory=dict)
    q_token:           int                = 0
    # ── new fields ────────────────────────────────────────────────────────────
    hint_early_event:  asyncio.Event      = field(default_factory=asyncio.Event)
    active_hint_view:  HintView | None    = None   # last hint's view; disabled on advance


# ── Cog ───────────────────────────────────────────────────────────────────────

class GameCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot    = bot
        self._states: dict[int, _GameState]   = {}
        self._locks:  dict[int, asyncio.Lock] = {}

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

    @sigmionary.command(name="start", description="Start a Sigmionary game in this channel")
    @discord.option("rounds", description="Number of rounds to play (0 = all)", input_type=int, required=False, default=0)
    async def cmd_start(self, ctx: discord.ApplicationContext, rounds: int = 0) -> None:
        state = self._state(ctx.guild_id)
        if state.active:
            await ctx.respond(
                "A game is already running! Use the 🛑 Stop button or `/sigmionary stop`.",
                ephemeral=True,
            )
            return
        await ctx.defer()
        await self._start_game_in_channel(
            ctx.channel, ctx.guild_id, rounds,
            started_by=ctx.author.display_name, author_id=ctx.author.id,
        )

    @sigmionary.command(name="stop", description="Stop the current game (Manage Server)")
    async def cmd_stop(self, ctx: discord.ApplicationContext) -> None:
        state = self._state(ctx.guild_id)
        if not state.active:
            await ctx.respond("No game is currently running.", ephemeral=True)
            return
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.respond("You need **Manage Server** to stop a game.", ephemeral=True)
            return
        await ctx.respond("🛑 Game stopped by a moderator.")
        await self._end_game(ctx.channel, ctx.guild_id, forced=True)

    @sigmionary.command(name="skip", description="Skip the current question (Manage Server)")
    async def cmd_skip(self, ctx: discord.ApplicationContext) -> None:
        state = self._state(ctx.guild_id)
        if not state.active:
            await ctx.respond("No game is currently running.", ephemeral=True)
            return
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.respond("You need **Manage Server** to skip questions.", ephemeral=True)
            return
        item = state.current_q["item"] if state.current_q else "?"
        await ctx.respond(f"⏭️ Skipping — the answer was **{item}**.")
        await self._advance(ctx.channel, ctx.guild_id, state.q_token)

    @sigmionary.command(name="score", description="Show scores for the current game session")
    async def cmd_score(self, ctx: discord.ApplicationContext) -> None:
        state = self._state(ctx.guild_id)
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

    @sigmionary.command(name="leaderboard", description="All-time leaderboard for this server")
    async def cmd_leaderboard(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        rows = await db.get_leaderboard(ctx.guild_id, limit=10)
        if not rows:
            await ctx.respond("No scores recorded yet — be the first to play!")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, row in enumerate(rows):
            medal  = medals[i] if i < 3 else f"**{i + 1}.**"
            member = ctx.guild.get_member(row["user_id"])
            name   = member.display_name if member else f"<@{row['user_id']}>"
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

    @sigmionary.command(name="stats", description="View your stats (or another player's)")
    @discord.option("user", description="Player to look up (default: yourself)", input_type=discord.Member, required=False)
    async def cmd_stats(self, ctx: discord.ApplicationContext, user: discord.Member = None) -> None:
        await ctx.defer()
        target   = user or ctx.author
        guild_id = ctx.guild_id
        stats    = await db.get_user_stats(guild_id, target.id)
        if not stats:
            msg = (
                "You haven't played yet!"
                if target == ctx.author
                else f"**{target.display_name}** hasn't played here yet."
            )
            await ctx.respond(msg, ephemeral=True)
            return
        rank = await db.get_user_rank(guild_id, target.id)
        acc  = (
            f"{stats['total_correct'] / stats['games_played']:.1f} correct/game"
            if stats["games_played"] else "—"
        )
        embed = discord.Embed(
            title=f"Stats — {target.display_name}",
            color=target.color if target.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Server Rank",     value=f"#{rank}",                      inline=True)
        embed.add_field(name="Total Points",    value=f"{stats['total_points']:,}",     inline=True)
        embed.add_field(name="Games Played",    value=str(stats["games_played"]),       inline=True)
        embed.add_field(name="Correct Answers", value=str(stats["total_correct"]),      inline=True)
        embed.add_field(name="Best Streak",     value=str(stats["best_streak"]),        inline=True)
        embed.add_field(name="Avg per Game",    value=acc,                              inline=True)
        await ctx.respond(embed=embed)

    @sigmionary.command(name="help", description="How to play Sigmionary")
    async def cmd_help(self, ctx: discord.ApplicationContext) -> None:
        embed = discord.Embed(title="How to Play Sigmionary", color=discord.Color.purple())
        embed.add_field(
            name="What is it?",
            value=(
                "A picture-guessing game — a series of images reveal a place, word, or item.\n"
                "Guess it before all hints run out!"
            ),
            inline=False,
        )
        embed.add_field(
            name="How to answer",
            value=(
                "**Just type your answer in chat** — no slash command needed.\n"
                "Typos and spelling variations are handled automatically."
            ),
            inline=False,
        )
        embed.add_field(
            name="Buttons during a game",
            value=(
                "**💡 Next Hint** — reveal the next image immediately (no 20-second wait)\n"
                "**📊 Score** — see current session scores\n"
                "**⏭️ Skip** — skip this question *(Manage Server)*\n"
                "**🛑 Stop** — end the game *(Manage Server)*\n"
                "**▶️ Next Question** — advance instantly after an answer"
            ),
            inline=False,
        )
        embed.add_field(
            name="Scoring",
            value=(
                "**Hint 1:** 100 pts + speed bonus (up to +30)\n"
                "**Hint 2:** 70 pts + speed bonus\n"
                "**Hint 3:** 40 pts\n"
                "**Streak multipliers:** 2×→×1.2 · 3×→×1.5 · 5×→×2.0"
            ),
            inline=False,
        )
        view = discord.ui.View()
        btn  = discord.ui.Button(label="🎮 Start a Game", style=discord.ButtonStyle.success)

        async def start_cb(interaction: discord.Interaction) -> None:
            if self._state(interaction.guild_id).active:
                await interaction.response.send_message("A game is already running here!", ephemeral=True)
                return
            await interaction.response.defer()
            await self._start_game_in_channel(
                interaction.channel, interaction.guild_id,
                rounds=0, started_by=interaction.user.display_name, author_id=interaction.user.id,
            )

        btn.callback = start_cb
        view.add_item(btn)
        await ctx.respond(embed=embed, view=view, ephemeral=True)

    # ── Shared game-start logic ───────────────────────────────────────────────

    async def _start_game_in_channel(
        self,
        channel: discord.TextChannel,
        guild_id: int,
        rounds: int,
        started_by: str,
        author_id: int,
    ) -> None:
        questions = await load_questions(guild_id)
        if not questions:
            await channel.send("No questions found. Add some first.")
            return

        seen_ids = await db.get_seen_question_ids(guild_id)
        unseen   = [q for q in questions if q["id"] not in seen_ids]

        fresh_start = False
        if not unseen:
            await db.reset_seen_questions(guild_id)
            unseen      = list(questions)
            fresh_start = True

        random.shuffle(unseen)
        pool = unseen[:rounds] if rounds and 0 < rounds < len(unseen) else unseen

        state                    = self._state(guild_id)
        state.active             = True
        state.channel_id         = channel.id
        state.questions          = pool
        state.current_idx        = -1
        state.current_q          = None
        state.session_scores     = {}
        state.streaks            = {}
        state.q_answered         = False
        state.q_token            = 0
        state.active_hint_view   = None
        state.hint_early_event.clear()
        state.session_id         = await db.create_session(guild_id, author_id)

        fresh_note = "\n\n🔄 All questions completed — starting a **fresh rotation**!" if fresh_start else ""
        embed = discord.Embed(
            title="🎮 Sigmionary — Game Starting!",
            description=(
                f"**{len(pool)} round(s)** | Type answers in chat — no commands needed!\n\n"
                "**Scoring**\n"
                "Hint 1 → **100 pts** + speed bonus (+30)\n"
                "Hint 2 → **70 pts** + speed bonus\n"
                "Hint 3 → **40 pts**\n\n"
                "**Streak multipliers:** 2×→×1.2 · 3×→×1.5 · 5×→×2.0\n\n"
                "**Buttons on every hint:** 💡 Next Hint · ⏭️ Skip · 🛑 Stop · 📊 Score\n\n"
                f"First question in **3 seconds!**{fresh_note}"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Started by {started_by}")
        await channel.send(embed=embed)

        await asyncio.sleep(3)
        await self._next_question(channel, guild_id)

    # ── Game-flow internals ───────────────────────────────────────────────────

    async def _next_question(self, channel: discord.TextChannel, guild_id: int) -> None:
        state = self._state(guild_id)
        if not state.active:
            return

        # Disable buttons on the previous hint message
        if state.active_hint_view:
            asyncio.create_task(state.active_hint_view.disable_all())
            state.active_hint_view = None

        state.current_idx += 1
        if state.current_idx >= len(state.questions):
            await self._end_game(channel, guild_id, forced=False)
            return

        q                     = state.questions[state.current_idx]
        state.current_q       = q
        state.hint_level      = 0
        state.q_answered      = False
        state.hint_start_time = 0.0
        state.q_token        += 1
        state.hint_early_event.clear()
        token = state.q_token

        asyncio.create_task(db.mark_question_seen(guild_id, q["id"]))

        n_total = len(state.questions)
        n_hints = len(q["images"])

        embed = discord.Embed(
            title=f"Question {state.current_idx + 1} / {n_total}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Category",     value=q["category"],    inline=True)
        embed.add_field(name="Sub-category", value=q["subcategory"], inline=True)
        embed.add_field(name="Hints",        value=f"{n_hints} image(s) — revealed one at a time", inline=False)
        embed.set_footer(text="💡 Use the Next Hint button or just wait — type your answer in chat!")
        await channel.send(embed=embed)

        await asyncio.sleep(2)

        if state.hint_task and not state.hint_task.done():
            state.hint_task.cancel()
        state.hint_task = asyncio.create_task(
            self._hint_loop(channel, guild_id, token)
        )

    async def _hint_loop(
        self, channel: discord.TextChannel, guild_id: int, token: int
    ) -> None:
        """
        Reveal images one-by-one, then call _on_timeout if unanswered.
        Wrapped in a safety net so any exception forces a recovery advance
        instead of silently leaving the game stuck forever.
        """
        state = self._state(guild_id)
        q     = state.current_q

        if q is None:
            log.error("_hint_loop: current_q is None for guild %d — aborting", guild_id)
            return

        revealed: list[Path] = []

        try:
            for idx, img_path in enumerate(q["images"]):
                if not state.active or state.q_token != token:
                    return

                revealed.append(img_path)
                state.hint_level      = idx + 1
                state.hint_start_time = time.time()
                is_last               = (idx == len(q["images"]) - 1)

                # Send hint message; store view reference for later disabling
                try:
                    view = HintView(self, guild_id, token, is_last_hint=is_last)
                    msg  = await self._send_hints(
                        channel, q, revealed, state.hint_level, len(q["images"]), view=view
                    )
                    if msg is not None:
                        view.message = msg
                        # Disable the previous hint's view now that a new one is live
                        if state.active_hint_view and state.active_hint_view is not view:
                            asyncio.create_task(state.active_hint_view.disable_all())
                        state.active_hint_view = view
                except Exception as exc:
                    log.error(
                        "_hint_loop: send failed guild=%d q=%s: %s", guild_id, q["item"], exc
                    )

                # Wait for HINT_INTERVAL seconds, or wake early if button clicked
                state.hint_early_event.clear()
                try:
                    await asyncio.wait_for(
                        state.hint_early_event.wait(), timeout=HINT_INTERVAL
                    )
                    # Button woke us early — proceed to next hint
                except asyncio.TimeoutError:
                    pass  # Normal timeout
                except asyncio.CancelledError:
                    return  # Task cancelled (answer given or game stopped)

            # All hints exhausted without a correct answer
            if state.active and state.q_token == token and not state.q_answered:
                await self._on_timeout(channel, guild_id, token)

        except asyncio.CancelledError:
            return

        except Exception as exc:
            # Safety net: log and force-advance so the game is never permanently stuck.
            log.error(
                "_hint_loop unhandled exception guild=%d q=%s: %s",
                guild_id, q.get("item", "?"), exc, exc_info=True,
            )
            state = self._state(guild_id)
            if state.active and state.q_token == token and not state.q_answered:
                try:
                    await channel.send(
                        "⚠️ Something went wrong revealing that hint — moving to the next question.",
                        delete_after=5,
                    )
                except Exception:
                    pass
                await asyncio.sleep(2)
                await self._next_question(channel, guild_id)

    async def _send_hints(
        self,
        channel: discord.TextChannel,
        q: dict,
        revealed: list[Path],
        hint_num: int,
        total_hints: int,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        """Stitch revealed images side-by-side and send a single embed. Returns the sent message."""
        embed = discord.Embed(
            description=f"**Category:** {q['category']}  |  **Sub-category:** {q['subcategory']}",
            color=discord.Color.og_blurple(),
        )
        embed.set_author(name=f"Hint {hint_num} / {total_hints}")
        embed.set_footer(text=f"⏱️ {HINT_INTERVAL}s · type your answer in chat · use 💡 to speed up")

        buf = _stitch_images(revealed)
        if buf:
            embed.set_image(url="attachment://hints.png")
            return await channel.send(
                file=discord.File(buf, filename="hints.png"), embed=embed, view=view
            )

        # Fallback: send images as separate files
        try:
            handles = [open(p, "rb") for p in revealed]
            files   = [discord.File(h, filename=p.name) for h, p in zip(handles, revealed)]
            msg = await channel.send(files=files, embed=embed, view=view)
            for h in handles:
                h.close()
            return msg
        except Exception as exc:
            log.error("_send_hints fallback failed: %s", exc)
            return await channel.send(
                embed=discord.Embed(
                    description="*(Images unavailable for this hint)*",
                    color=discord.Color.greyple(),
                ),
                view=view,
            )

    async def _on_timeout(
        self, channel: discord.TextChannel, guild_id: int, token: int
    ) -> None:
        state = self._state(guild_id)
        if not state.active or state.q_token != token:
            return

        # Disable hint buttons
        if state.active_hint_view:
            asyncio.create_task(state.active_hint_view.disable_all())
            state.active_hint_view = None

        q = state.current_q
        for uid in list(state.session_scores):
            state.streaks[uid] = 0

        view  = NextQuestionView(self, guild_id, token)
        embed = discord.Embed(
            title=f"⏰ Time's up!  The answer was **{q['item']}**",
            description=f"{q['category']} → {q['subcategory']}",
            color=discord.Color.red(),
        )
        embed.set_footer(text="Next question in a moment…")
        msg = await channel.send(embed=embed, view=view)
        view.message = msg

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

        if state.active_hint_view:
            asyncio.create_task(state.active_hint_view.disable_all())
            state.active_hint_view = None

        q_done = state.current_idx
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
            title="🏁 Game Over — Final Scores",
            description=desc,
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{q_done} question(s) played")
        await channel.send(embed=embed, view=PostGameView(self))

        # Reset mutable state (keeps the entry for a clean next-game start)
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

        if score < THRESHOLD_HOT:
            if score >= THRESHOLD_WARM:
                try:
                    await message.add_reaction("🌡️")
                except discord.HTTPException:
                    pass
            return

        if score < 82:
            try:
                await message.add_reaction("🔥")
            except discord.HTTPException:
                pass
            return

        # ── Correct! acquire lock to prevent simultaneous accepts ─────────────
        async with self._lock(guild_id):
            if state.q_answered:
                return
            if not state.active or state.current_q is None:
                return

            state.q_answered = True
            token   = state.q_token
            uid     = message.author.id
            elapsed = time.time() - state.hint_start_time

            streak                    = state.streaks.get(uid, 0) + 1
            state.streaks[uid]        = streak
            points                    = _calc_points(state.hint_level, elapsed, streak)
            state.session_scores[uid] = state.session_scores.get(uid, 0) + points

            for other_uid in list(state.session_scores):
                if other_uid != uid:
                    state.streaks[other_uid] = 0

            asyncio.create_task(
                db.record_answer(
                    state.session_id, guild_id, uid, answer,
                    points, state.hint_level, elapsed, streak,
                )
            )
            asyncio.create_task(
                _post_external_points(
                    user_id=uid,
                    guild_id=guild_id,
                    username=message.author.display_name,
                    points=points,
                    match_id=str(state.session_id),
                )
            )

            # Disable the active hint view immediately
            if state.active_hint_view:
                asyncio.create_task(state.active_hint_view.disable_all())
                state.active_hint_view = None

            mult       = _streak_multiplier(streak)
            mult_str   = f" ×{mult:.1f}" if mult > 1.0 else ""
            streak_str = f"  |  🔥 {streak}× streak!" if streak >= 2 else ""
            total      = state.session_scores[uid]

            view  = NextQuestionView(self, guild_id, token)
            embed = discord.Embed(
                title=f"✅  {message.author.display_name} got it!",
                description=(
                    f"**{answer}**\n"
                    f"+**{points} pts**{mult_str}  |  Session total: **{total:,} pts**{streak_str}"
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text="Next question coming up — or press the button to skip the wait!")
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            msg = await message.channel.send(embed=embed, view=view)
            view.message = msg

            if state.hint_task and not state.hint_task.done():
                state.hint_task.cancel()

        # Advance outside the lock to avoid holding it over I/O
        await asyncio.sleep(BETWEEN_Q_DELAY)
        await self._advance(message.channel, guild_id, token)


# ── Image stitching ────────────────────────────────────────────────────────────

_TARGET_HEIGHT = 300
_GAP           = 12
_BG_COLOR      = (47, 49, 54)


def _stitch_images(paths: list[Path]) -> io.BytesIO | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        frames: list[Image.Image] = []
        for p in paths:
            with Image.open(p) as raw:
                img   = raw.convert("RGBA")
                ratio = _TARGET_HEIGHT / img.height
                new_w = max(1, int(img.width * ratio))
                frames.append(img.resize((new_w, _TARGET_HEIGHT), Image.LANCZOS))

        total_w = sum(f.width for f in frames) + _GAP * (len(frames) - 1)
        canvas  = Image.new("RGBA", (total_w, _TARGET_HEIGHT), (*_BG_COLOR, 255))
        x = 0
        for frame in frames:
            canvas.paste(frame, (x, 0), frame)
            x += frame.width + _GAP

        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, "PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception as exc:
        log.warning("_stitch_images failed: %s", exc)
        return None


# ── Helpers ────────────────────────────────────────────────────────────────────

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
