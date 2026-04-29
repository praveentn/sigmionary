"""
admin_cog.py — Developer-only puzzle management commands.

Uses prefix commands (!) so they never appear in Discord's slash command menu.
Access is restricted to Discord user IDs listed in DEVELOPER_IDS (env var).

Usage:
    !addpuzzle   — interactive wizard to add a new puzzle with images
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from utils import database as db
from utils.questions import IMAGES_PATH

log = logging.getLogger("sigmionary")

_IMG_EXTS     = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_STEP_TIMEOUT = 120   # seconds for view interactions (select / buttons)
_IMG_TIMEOUT  = 300   # seconds of inactivity before image upload session expires

# Comma-separated Discord user IDs: DEVELOPER_IDS=123456789,987654321
_raw_ids = os.getenv("DEVELOPER_IDS", "")
DEVELOPER_IDS: frozenset[int] = frozenset(
    int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()
)


# ── Reusable Views ─────────────────────────────────────────────────────────────

class _ChoiceView(discord.ui.View):
    """
    Shows a Select with existing choices + '+ New …' option, and a Cancel button.
    After stop(): check .chosen (str), .new_requested (bool), .cancelled (bool).
    """

    def __init__(self, existing: list[str], noun: str) -> None:
        super().__init__(timeout=_STEP_TIMEOUT)
        self.chosen:        str | None = None
        self.new_requested: bool       = False
        self.cancelled:     bool       = False

        if existing:
            options = [discord.SelectOption(label=o, value=o) for o in existing[:24]]
            options.append(discord.SelectOption(label=f"+ New {noun}", value="__new__", emoji="➕"))
            self._sel = discord.ui.Select(
                placeholder=f"Pick a {noun.lower()} or create new…",
                options=options,
            )
            self._sel.callback = self._on_select
            self.add_item(self._sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        val = self._sel.values[0]
        if val == "__new__":
            self.new_requested = True
        else:
            self.chosen = val
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.cancelled = True
        self.stop()


class _ConfirmView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=_STEP_TIMEOUT)
        self.confirmed: bool = False

    @discord.ui.button(label="Save Puzzle", style=discord.ButtonStyle.success)
    async def confirm_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.confirmed = True
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.stop()


# ── Cog ────────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot              = bot
        self._active: set[int] = set()   # user IDs with an open session

    # ── Guard ─────────────────────────────────────────────────────────────────

    def _msg_check(self, ctx: commands.Context):
        """wait_for check: same author + same channel, not a bot."""
        def check(m: discord.Message) -> bool:
            return (
                m.author.id  == ctx.author.id
                and m.channel.id == ctx.channel.id
                and not m.author.bot
            )
        return check

    # ── !addpuzzle ────────────────────────────────────────────────────────────

    @commands.command(name="addpuzzle")
    async def add_puzzle(self, ctx: commands.Context) -> None:
        # Silently ignore everyone who isn't a developer
        if ctx.author.id not in DEVELOPER_IDS:
            return

        if ctx.guild is None:
            await ctx.send("Run this command inside a server.")
            return

        if ctx.author.id in self._active:
            await ctx.send("You already have an active session. Finish it first.")
            return

        self._active.add(ctx.author.id)
        try:
            await self._wizard(ctx)
        except Exception as exc:
            log.error("!addpuzzle crashed: %s", exc, exc_info=True)
            await ctx.send(f"Unexpected error: `{exc}`")
        finally:
            self._active.discard(ctx.author.id)

    # ── Wizard ────────────────────────────────────────────────────────────────

    async def _wizard(self, ctx: commands.Context) -> None:
        guild_id = ctx.guild.id
        check    = self._msg_check(ctx)

        # ── Step 1: Category ──────────────────────────────────────────────────
        categories = await db.get_categories(guild_id)

        embed = discord.Embed(title="Add Puzzle — Step 1 of 4: Category", color=discord.Color.blurple())
        if categories:
            embed.description = "**Existing categories:**\n" + "\n".join(f"• {c}" for c in categories)
        else:
            embed.description = "No categories yet — you'll create the first one."

        view = _ChoiceView(categories, "Category")

        if not categories:
            # No existing choices — skip the view, go straight to text prompt
            view.new_requested = True

        msg = await ctx.send(embed=embed, view=view if categories else None)

        if categories:
            await view.wait()
            if view.cancelled or (not view.chosen and not view.new_requested):
                await msg.edit(content="Cancelled.", embed=None, view=None)
                return

        if view.new_requested:
            await msg.edit(
                embed=discord.Embed(description="Type the **new category name**:", color=discord.Color.blurple()),
                view=None,
            )
            try:
                reply = await self.bot.wait_for("message", timeout=60.0, check=check)
                category = reply.content.strip()
            except asyncio.TimeoutError:
                await msg.edit(content="Timed out — cancelled.", embed=None)
                return
            if not category:
                await msg.edit(content="Empty name — cancelled.", embed=None)
                return
        else:
            category = view.chosen

        # ── Step 2: Sub-category ──────────────────────────────────────────────
        subcategories = await db.get_subcategories(guild_id, category)

        embed2 = discord.Embed(
            title="Add Puzzle — Step 2 of 4: Sub-category",
            description=f"Category: **{category}**",
            color=discord.Color.blurple(),
        )
        if subcategories:
            embed2.add_field(
                name="Existing sub-categories",
                value="\n".join(f"• {s}" for s in subcategories),
                inline=False,
            )

        view2 = _ChoiceView(subcategories, "Sub-category")

        if not subcategories:
            view2.new_requested = True

        await msg.edit(embed=embed2, view=view2 if subcategories else None)

        if subcategories:
            await view2.wait()
            if view2.cancelled or (not view2.chosen and not view2.new_requested):
                await msg.edit(content="Cancelled.", embed=None, view=None)
                return

        if view2.new_requested:
            await msg.edit(
                embed=discord.Embed(
                    description=f"Category: **{category}**\nType the **new sub-category name**:",
                    color=discord.Color.blurple(),
                ),
                view=None,
            )
            try:
                reply = await self.bot.wait_for("message", timeout=60.0, check=check)
                subcategory = reply.content.strip()
            except asyncio.TimeoutError:
                await msg.edit(content="Timed out — cancelled.", embed=None)
                return
            if not subcategory:
                await msg.edit(content="Empty name — cancelled.", embed=None)
                return
        else:
            subcategory = view2.chosen

        # ── Step 3: Item name ─────────────────────────────────────────────────
        await msg.edit(
            embed=discord.Embed(
                title="Add Puzzle — Step 3 of 4: Item Name",
                description=(
                    f"Category: **{category}** › **{subcategory}**\n\n"
                    "Type the **item name** (the answer players will guess):"
                ),
                color=discord.Color.blurple(),
            ),
            view=None,
        )

        try:
            reply = await self.bot.wait_for("message", timeout=60.0, check=check)
            item_name = reply.content.strip()
        except asyncio.TimeoutError:
            await msg.edit(content="Timed out — cancelled.", embed=None)
            return
        if not item_name:
            await msg.edit(content="Empty name — cancelled.", embed=None)
            return

        # ── Step 4: Images ────────────────────────────────────────────────────
        await msg.edit(
            embed=discord.Embed(
                title="Add Puzzle — Step 4 of 4: Images",
                description=(
                    f"Adding: **{item_name}** ({category} › {subcategory})\n\n"
                    "Upload images **one at a time** as attachments, in reveal order.\n"
                    "Each image is numbered automatically by upload sequence.\n\n"
                    "• Type `done` when finished\n"
                    "• Type `cancel` to abort\n\n"
                    f"_Session expires after {_IMG_TIMEOUT // 60} min of inactivity._"
                ),
                color=discord.Color.blurple(),
            ),
            view=None,
        )

        # Collect (position, original_filename, bytes) in memory
        images_data: list[tuple[int, str, bytes]] = []

        while True:
            try:
                img_msg = await self.bot.wait_for("message", timeout=_IMG_TIMEOUT, check=check)
            except asyncio.TimeoutError:
                await ctx.send("Session timed out — cancelled. Nothing was saved.")
                return

            text = img_msg.content.strip().lower()

            if text == "cancel":
                await ctx.send("Cancelled — nothing was saved.")
                return

            if text == "done":
                break

            if not img_msg.attachments:
                await ctx.send("No attachment found. Send an image file, or type `done` / `cancel`.")
                continue

            att = img_msg.attachments[0]
            ext = Path(att.filename).suffix.lower()
            if ext not in _IMG_EXTS:
                await ctx.send(
                    f"Unsupported format `{ext}`. Accepted: {', '.join(sorted(_IMG_EXTS))}"
                )
                continue

            img_bytes = await att.read()
            pos = len(images_data) + 1
            images_data.append((pos, att.filename, img_bytes))
            await img_msg.add_reaction("✅")
            await ctx.send(f"Image {pos} saved. Send the next image or type `done`.")

        if not images_data:
            await ctx.send("No images provided — cancelled. Nothing was saved.")
            return

        # ── Confirm ───────────────────────────────────────────────────────────
        confirm_embed = discord.Embed(title="Confirm New Puzzle", color=discord.Color.gold())
        confirm_embed.add_field(name="Category",     value=category,                 inline=True)
        confirm_embed.add_field(name="Sub-category", value=subcategory,              inline=True)
        confirm_embed.add_field(name="Item",         value=item_name,                inline=True)
        confirm_embed.add_field(name="Images",       value=f"{len(images_data)}",    inline=True)
        confirm_embed.add_field(
            name="Visibility",
            value="This server only",
            inline=True,
        )
        confirm_embed.set_footer(text="Press Save Puzzle to confirm, or Cancel to discard.")

        confirm_view = _ConfirmView()
        confirm_msg  = await ctx.send(embed=confirm_embed, view=confirm_view)
        await confirm_view.wait()

        if not confirm_view.confirmed:
            await confirm_msg.edit(content="Cancelled — nothing was saved.", embed=None, view=None)
            return

        # ── Save to DB + Volume ───────────────────────────────────────────────
        try:
            qid = await db.insert_question(guild_id, category, subcategory, item_name, ctx.author.id)

            q_dir = IMAGES_PATH / str(qid)
            q_dir.mkdir(parents=True, exist_ok=True)

            for pos, filename, img_bytes in images_data:
                dest = q_dir / f"{pos}-{filename}"
                dest.write_bytes(img_bytes)
                await db.insert_question_image(qid, pos, str(dest))

            await confirm_msg.edit(
                embed=discord.Embed(
                    title="Puzzle Added!",
                    description=(
                        f"**{item_name}** ({category} › {subcategory})\n"
                        f"{len(images_data)} image(s) saved to `{q_dir}`\n"
                        f"Question ID: `{qid}`"
                    ),
                    color=discord.Color.green(),
                ),
                view=None,
            )
            log.info(
                "Puzzle added — id=%d item=%r guild=%d by=%d images=%d",
                qid, item_name, guild_id, ctx.author.id, len(images_data),
            )

        except Exception as exc:
            log.error("Failed to save puzzle: %s", exc, exc_info=True)
            await confirm_msg.edit(content=f"Save failed: `{exc}`", embed=None, view=None)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AdminCog(bot))
