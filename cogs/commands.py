"""
Slash commands for interacting with the ghost directly:

- /seance <question>  - ask it something, get a cryptic in-character answer
- /haunt <user>        - it starts randomly slipping into that member's
                          conversations for a while
- /lore                - request the next unrevealed fragment of Velmora's
                          backstory
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

log = logging.getLogger("velmora.commands")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LORE_PATH = DATA_DIR / "lore.json"

HAUNT_DURATION_SECONDS = 60 * 60 * 6  # 6 hours

OTHER_GHOST_NAME = os.getenv("OTHER_GHOST_NAME", "the other ghost")


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

    @app_commands.command(name="seance", description="Ask the ghost of Velmora a question.")
    @app_commands.describe(question="What do you want to ask it?")
    async def seance(self, interaction: discord.Interaction, question: str):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "The circle will not form tonight.", ephemeral=True
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
            "Answer as the ghost - cryptic, but responsive to what was actually asked."
        )
        line = await personality.speak(cue, memory_hint=memory_hint, max_tokens=250)

        embed = discord.Embed(
            description=line,
            color=discord.Color.dark_purple(),
        )
        embed.set_author(name=f"{asker} calls out into the dark...")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="haunt", description="Set the ghost loose on a specific member for a while.")
    @app_commands.describe(user="Who should the ghost fixate on?")
    async def haunt(self, interaction: discord.Interaction, user: discord.Member):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "It does not answer to that name.", ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message(
                "It has no interest in the hollow ones.", ephemeral=True
            )
            return

        personality.set_haunt_target(user.id, HAUNT_DURATION_SECONDS)

        cue = (
            f"You have just been set loose to haunt {user.display_name} specifically, for a while. "
            "Announce, in character, that you've noticed them - a warning or a promise, not an explanation."
        )
        line = await personality.speak(cue, max_tokens=150)

        embed = discord.Embed(
            description=line,
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text=f"The ghost's attention now turns to {user.display_name}.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="lore", description="Ask the ghost to reveal a fragment of Velmora's past.")
    async def lore(self, interaction: discord.Interaction):
        personality = self._personality()
        if not personality:
            await interaction.response.send_message(
                "The past stays buried tonight.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        fragment = personality.next_lore_fragment(self.lore)
        if fragment is None:
            line = await personality.speak(
                "Someone has asked you to reveal more of your past, but you have already told "
                "them everything you're willing to. Deflect, in character - refuse without "
                "explaining that you've run out of material.",
                max_tokens=120,
            )
            embed = discord.Embed(description=line, color=discord.Color.dark_grey())
            await interaction.followup.send(embed=embed)
            return

        cue = (
            f'Reveal this fragment of your past to whoever is listening, in your own voice, '
            f'not verbatim but true to it: "{fragment}"'
        )
        line = await personality.speak(cue, max_tokens=200)

        embed = discord.Embed(
            title="A fragment surfaces...",
            description=line,
            color=discord.Color.dark_teal(),
        )
        remaining = len(self.lore) - personality.state.get("lore_index", 0)
        embed.set_footer(text=f"{remaining} fragment(s) of Velmora's past remain untold.")
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
                "You lack the standing to demand that of it.", ephemeral=True
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
        await interaction.followup.send(line)


async def setup(bot: commands.Bot):
    await bot.add_cog(GhostCommands(bot))
