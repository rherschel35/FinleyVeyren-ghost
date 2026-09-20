"""
Slash commands for interacting with the ghost directly:

- /seance <question>  - ask it something, get a warm, in-character answer
- /watch <user>        - it starts quietly checking in on that member for a
                          while (House Veyren's version of /haunt)
- /lore                - request the next unrevealed fragment of House
                          Veyren's history
- /mood                - (admin-only) peek at the ghost's current mood
- /interact            - call out to the other ghost bot for a brief,
                          capped public exchange
"""

import json
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("veyren.commands")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LORE_PATH = DATA_DIR / "lore.json"

WATCH_DURATION_SECONDS = 60 * 60 * 6  # 6 hours

OTHER_GHOST_NAME = os.getenv("OTHER_GHOST_NAME", "the other ghost")

# Must match the constant of the same name in cogs/haunting.py - marks this
# message as genuinely part of an /interact exchange (see there for why).
INTERACT_MARKER = "​"


def _load_lore():
    try:
        with open(LORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load lore.json")
        return []


class GhostCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lore = _load_lore()

    def _personality(self):
        return self.bot.get_cog("Personality")

    @app_commands.command(name="seance", description="Ask the Veyren ghost of Velmora a question.")
    @app_commands.describe(question="What do you want to ask it?")
    async def seance(self, interaction: discord.Interaction, question: str):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "No one answers tonight.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        asker = str(interaction.user.display_name)
        memory_hint = None
        prior = personality.memories_about(asker, limit=1)
        if prior:
            memory_hint = prior[0]

        cue = (
            f'{asker} has called a seance and asks you directly: "{question}". '
            "Answer as the ghost - warm and direct, but responsive to what was actually asked."
        )
        line = await personality.speak(cue, memory_hint=memory_hint, max_tokens=220)

        embed = discord.Embed(
            description=line,
            color=discord.Color.gold(),
        )
        embed.set_author(name=f"{asker} calls out into the quiet...")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="watch", description="Ask the ghost to quietly watch over a specific member for a while.")
    @app_commands.describe(user="Who should it look out for?")
    async def watch(self, interaction: discord.Interaction, user: discord.Member):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "It doesn't answer to that request right now.", ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message(
                "It has no need to watch over the hollow ones.", ephemeral=True
            )
            return

        personality.set_haunt_target(user.id, WATCH_DURATION_SECONDS)

        cue = (
            f"You have just been asked to watch over {user.display_name} specifically, for a while. "
            "Announce, in character, that you've noticed them and you're keeping an eye out - warm, "
            "quietly protective, not ominous."
        )
        line = await personality.speak(cue, max_tokens=150)

        embed = discord.Embed(
            description=line,
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"{user.display_name} is being watched over.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="lore", description="Ask the ghost to share a piece of House Veyren's history.")
    async def lore(self, interaction: discord.Interaction):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "That story stays untold tonight.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        fragment = personality.next_lore_fragment(self.lore)
        if fragment is None:
            line = await personality.speak(
                "Someone has asked you to share more of House Veyren's history, but you've already "
                "shared everything you're ready to. Deflect warmly, in character - not a refusal, more "
                "like 'not tonight' - without explaining that you've run out of material.",
                max_tokens=120,
            )
            embed = discord.Embed(description=line, color=discord.Color.dark_grey())
            await interaction.followup.send(embed=embed)
            return

        cue = (
            f'Share this piece of House Veyren\'s history with whoever is listening, in your own voice, '
            f'not verbatim but true to it: "{fragment}"'
        )
        line = await personality.speak(cue, max_tokens=200)

        embed = discord.Embed(
            title="A memory surfaces...",
            description=line,
            color=discord.Color.dark_gold(),
        )
        remaining = len(self.lore) - personality.state.get("lore_index", 0)
        embed.set_footer(text=f"{remaining} piece(s) of House Veyren's history remain untold.")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="mood", description="(admin) Peek at the ghost's current mood.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def mood(self, interaction: discord.Interaction):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message("No mood to report.", ephemeral=True)
            return

        from cogs.personality import GHOST_NAME

        await interaction.response.send_message(
            f"{GHOST_NAME}'s current mood: `{personality.current_mood()}`", ephemeral=True
        )

    @mood.error
    async def mood_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You don't need to ask it to trust you. But you do need permission for this.",
                ephemeral=True,
            )
        else:
            log.exception("Unhandled error in /mood", exc_info=error)

    @app_commands.command(name="interact", description="Call out to the other ghost for a brief exchange.")
    async def interact_command(self, interaction: discord.Interaction):
        personality = self._personality()
        haunting = self.bot.get_cog("Haunting")
        if not personality or not haunting:
            await interaction.response.send_message("No answer comes.", ephemeral=True)
            return

        channel_id = interaction.channel_id
        # (Re)start the exchange for this channel: this call-out is message 1.
        haunting.exchange_turns[channel_id] = {"total": 1, "last_at": time.time()}

        await interaction.response.defer(thinking=True)

        cue = (
            f"Call out, in character, to {OTHER_GHOST_NAME}, a distinct spirit who shares this place "
            "with you - address them directly, in front of everyone, inviting a response, as if "
            "starting a conversation between the two of you."
        )
        line = await personality.speak(cue, max_tokens=150)
        # Send as a normal channel message rather than the interaction followup -
        # the other ghost's bot reads this over the gateway to reply, and an
        # interaction-followup message doesn't reliably carry its content to
        # other bots the way a plain message does. Clean up the "thinking..."
        # placeholder so it doesn't linger next to the real message. The
        # trailing marker tells the other ghost's bot this is a genuine
        # call-out, not just something to eavesdrop on.
        await interaction.delete_original_response()
        await interaction.channel.send(line + INTERACT_MARKER)


async def setup(bot: commands.Bot):
    await bot.add_cog(GhostCommands(bot))
