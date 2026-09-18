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


def _parse_guild_ids(env_value: str | None):
    """Comma-separated list of server IDs this ghost is allowed to be in.
    If unset, no restriction is applied (not recommended for a bot with a
    live token floating around)."""
    if not env_value:
        return None
    ids = set()
    for part in env_value.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids or None


ALLOWED_GUILD_IDS = _parse_guild_ids(os.getenv("ALLOWED_GUILD_IDS"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!veyren-unused-", intents=intents, help_command=None)


INITIAL_COGS = (
    "cogs.personality",
    "cogs.haunting",
    "cogs.commands",
)


async def _leave_if_unauthorized(guild: discord.Guild) -> bool:
    """If this guild isn't on the allowed list, leave immediately and say
    so in the logs. Returns True if the ghost left."""
    if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
        log.warning(
            "Not authorized for guild %r (id=%s) - leaving immediately.", guild.name, guild.id
        )
        await guild.leave()
        return True
    return False


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Someone tried to add this ghost to a server it doesn't belong in.
    Leave right away - it should only ever live in Velmora."""
    await _leave_if_unauthorized(guild)


@bot.event
async def on_ready():
    log.info("Finley has arrived. Logged in as %s (id=%s)", bot.user, bot.user.id)

    # Catch any unauthorized guild it's already sitting in too - covers a
    # stale invite link used before ALLOWED_GUILD_IDS was set, or Public
    # Bot getting flipped back on by accident.
    for guild in list(bot.guilds):
        await _leave_if_unauthorized(guild)

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
