"""
Finley Veyren — a Discord bot that plays a gentler, House-Veyren-flavored
spirit watching over the Velmora server. Entry point: wires up the client,
loads cogs, and starts the background whisper loop.
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("veyren")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # optional, for instant slash-command sync while testing

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!veyren-unused-", intents=intents, help_command=None)


INITIAL_COGS = (
    "cogs.personality",
    "cogs.haunting",
    "cogs.commands",
)


@bot.event
async def on_ready():
    log.info("Finley has arrived. Logged in as %s (id=%s)", bot.user, bot.user.id)

    try:
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), DEV_GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Synced %d global commands", len(synced))
    except Exception:
        log.exception("Slash command sync failed")

    ghost_name = os.getenv("GHOST_NAME", "Finley Veyren")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=f"over Velmora as {ghost_name}")
    )

    haunting_cog = bot.get_cog("Haunting")
    if haunting_cog:
        haunting_cog.start_whisper_loop()


async def main():
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    async with bot:
        for cog in INITIAL_COGS:
            await bot.load_extension(cog)
            log.info("Loaded %s", cog)
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
