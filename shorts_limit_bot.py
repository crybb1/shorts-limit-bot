# file: shorts_limit_bot.py
import os
import re
import time
import asyncio
import aiosqlite
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

# --- Config ---
ROLLING_WINDOW_DAYS = 7
DB_PATH = "shorts_limits.sqlite"
# If True, the bot will try to delete offending messages. Requires Manage Messages.
DELETE_OFFENDING_MESSAGES = True
# If True, also DM the user when their message is blocked.
DM_USER_ON_BLOCK = True

# Strictly match Shorts-style URLs (desktop/mobile)
SHORTS_REGEX = re.compile(
    r"""(?ix)
    https?://
    (?:www\.)?(?:m\.)?youtube\.com/shorts/
    [A-Za-z0-9_-]{5,}
    (?:[/?#&].*)?
    """
)

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = False  # not needed

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shorts-limit-bot")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS shorts_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                posted_at_utc INTEGER NOT NULL
            )
            """
        )
        await db.commit()


async def record_shorts_post(guild_id: int, user_id: int):
    now_epoch = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shorts_posts (guild_id, user_id, posted_at_utc) VALUES (?, ?, ?)",
            (guild_id, user_id, now_epoch),
        )
        await db.commit()


async def count_within_window(guild_id: int, user_id: int, days: int) -> int:
    window_start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) 
            FROM shorts_posts
            WHERE guild_id = ? AND user_id = ? AND posted_at_utc >= ?
            """,
            (guild_id, user_id, window_start),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0



def extract_shorts_links(text: str) -> list[str]:
    return SHORTS_REGEX.findall(text or "")


@bot.event
async def on_ready():
    await init_db()
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    logger.info("Ready.")


@bot.event
async def on_message(message: discord.Message):
    # Ignore ourselves and webhooks
    if message.author.bot or message.webhook_id:
        return

    shorts_links = extract_shorts_links(message.content)
    if not shorts_links:
        await bot.process_commands(message)
        return

    # Only enforce in guild channels
    if not message.guild:
        await bot.process_commands(message)
        return

    # Check count in rolling window
    try:
        current_count = await count_within_window(message.guild.id, message.author.id, ROLLING_WINDOW_DAYS)
        if current_count >= 1:
            # Over the limit → block
            blocked_notice = (
                f"Hey {message.author.mention}, you’ve already posted a YouTube Shorts link "
                f"in the last {ROLLING_WINDOW_DAYS} days. This one was blocked."
            )

            # Try delete
            if DELETE_OFFENDING_MESSAGES:
                try:
                    await message.delete()
                except discord.Forbidden:
                    blocked_notice += " (I don’t have permission to delete messages.)"
                except discord.HTTPException:
                    pass

            # Notify in channel (and delete the notice after a few seconds to keep chat clean)
            try:
                warn_msg = await message.channel.send(blocked_notice)
                await asyncio.sleep(8)
                await warn_msg.delete()
            except discord.HTTPException:
                pass

            # DM the user (optional)
            if DM_USER_ON_BLOCK:
                try:
                    await message.author.send(
                        "Your Shorts link was blocked because you’ve hit the 1-per-week limit in that server."
                    )
                except discord.Forbidden:
                    pass  # DMs disabled

            return  # Don’t record blocked post
        else:
            # Allowed: record exactly once per message that contains ≥1 shorts links
            await record_shorts_post(message.guild.id, message.author.id)

    finally:
        # Continue processing commands if present
        await bot.process_commands(message)


# --- Slash commands ---


@bot.tree.command(name="shorts_stats", description="See how many Shorts links a user has posted in the last 7 days.")
async def shorts_stats(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    count = await count_within_window(interaction.guild_id, member.id, ROLLING_WINDOW_DAYS)
    await interaction.response.send_message(
        f"{member.display_name} has posted **{count}** Shorts link(s) in the last {ROLLING_WINDOW_DAYS} days.",
        ephemeral=True,
    )


@bot.tree.command(name="shorts_reset_me", description="(Personal) Reset your rolling window by forgetting your last Shorts post.")
async def shorts_reset_me(interaction: discord.Interaction):
    # Optional helper for users to clear their last record (admin could disable this by removing the command)
    window_start = int((datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)).timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM shorts_posts
            WHERE guild_id = ? AND user_id = ? AND posted_at_utc >= ?
            """,
            (interaction.guild_id, interaction.user.id, window_start),
        )
        await db.commit()
    await interaction.response.send_message("Your Shorts count for the current 7-day window was reset.", ephemeral=True)


@bot.event
async def setup_hook():
    # Sync app commands on startup
    try:
        await bot.tree.sync()
    except Exception as e:
        logger.error(f"Command sync failed: {e}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(token)
