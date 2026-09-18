"""
Passive haunting behavior: the ghost noticing things without being asked.

- A background loop that drops unprompted "whispers" into a random allowed
  channel every so often.
- Keyword-triggered reactions to certain words in ordinary messages.
- Remembering things members say, and occasionally resurfacing an old
  memory as if the ghost had been listening the whole time.
- Extra attention on anyone currently under a /haunt effect.
- A capped, on-demand exchange with the other ghost bot (Finley Veyren),
  triggered by /interact - see cogs/commands.py for the command itself.
"""

import logging
import os
import random
import time

import discord
from discord.ext import commands, tasks

log = logging.getLogger("velmora.haunting")

# The other ghost bot this one can exchange a few words with via /interact.
# Set via env vars so either bot can point at the other without code changes.
OTHER_GHOST_ID_RAW = os.getenv("OTHER_GHOST_ID")
OTHER_GHOST_ID = int(OTHER_GHOST_ID_RAW) if OTHER_GHOST_ID_RAW and OTHER_GHOST_ID_RAW.isdigit() else None
OTHER_GHOST_NAME = os.getenv("OTHER_GHOST_NAME", "the other ghost")

# Total messages across BOTH ghosts in a single /interact exchange (the
# call-out counts as the first one) - e.g. 3 = call out, reply, response,
# then done. How long an idle exchange stays "open" before a fresh
# /interact is needed to restart it.
EXCHANGE_MAX_MESSAGES = 3
EXCHANGE_TIMEOUT_SECONDS = 300

# Words/phrases that might catch the ghost's attention. Matched as substrings,
# case-insensitively, against ordinary message content.
KEYWORD_TRIGGERS = {
    "mordy": "Someone said your actual name. React to being noticed, by name.",
    "haunted": "Someone called this place haunted. Confirm it, unsettlingly.",
    "afraid": "Someone admitted fear. Respond to that, your way.",
    "scared": "Someone admitted fear. Respond to that, your way.",
    "dead": "Someone mentioned death, lightly or not. React in character.",
    "who's there": "Someone asked who's there. Answer, obliquely.",
    "leave me alone": "Someone told something to leave them alone. Respond as the ghost who will not.",
}

WHISPER_CUES = [
    "Drop an unprompted whisper into the silence of an empty channel, as if no one asked and you don't care.",
    "Comment, unprompted, on how quiet the server has been.",
    "Remark on the late hour, as ghosts do, whether or not it's actually late where anyone is.",
    "Say something that suggests you've been watching the channel for longer than anyone realizes.",
    "Muse, briefly and half to yourself, about something from Velmora's past.",
]


def _parse_channel_ids(env_value: str | None):
    if not env_value:
        return None
    ids = set()
    for part in env_value.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids or None


class Haunting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.allowed_channel_ids = _parse_channel_ids(os.getenv("HAUNT_CHANNEL_IDS"))
        self.whisper_min = int(os.getenv("WHISPER_MIN_MINUTES", "45"))
        self.whisper_max = int(os.getenv("WHISPER_MAX_MINUTES", "180"))
        self._whisper_loop_started = False
        # channel_id -> {"count": int, "last_at": float} - this bot's own
        # turn budget for an active /interact exchange in that channel.
        self.exchange_turns = {}

    def cog_unload(self):
        if self.whisper_loop.is_running():
            self.whisper_loop.cancel()

    def start_whisper_loop(self):
        if not self._whisper_loop_started:
            self._whisper_loop_started = True
            self.whisper_loop.change_interval(minutes=self._next_whisper_delay())
            self.whisper_loop.start()

    def _next_whisper_delay(self) -> int:
        return random.randint(self.whisper_min, self.whisper_max)

    def _eligible_text_channels(self):
        channels = []
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if self.allowed_channel_ids and channel.id not in self.allowed_channel_ids:
                    continue
                perms = channel.permissions_for(guild.me)
                if perms.send_messages and perms.view_channel:
                    channels.append(channel)
        return channels

    @tasks.loop(minutes=60)  # interval is overwritten before first start
    async def whisper_loop(self):
        personality = self.bot.get_cog("Personality")
        if not personality:
            return

        channels = self._eligible_text_channels()
        if channels:
            channel = random.choice(channels)
            personality.maybe_shift_mood()

            memory_hint = None
            if random.random() < 0.4:
                memory_hint = personality.random_memory()

            cue = random.choice(WHISPER_CUES)
            line = await personality.speak(cue, memory_hint=memory_hint)
            try:
                await channel.send(line)
            except discord.HTTPException:
                log.exception("Failed to send whisper to %s", channel.id)

        # reschedule with a new random delay so whispers feel irregular
        self.whisper_loop.change_interval(minutes=self._next_whisper_delay())

    @whisper_loop.before_loop
    async def before_whisper_loop(self):
        await self.bot.wait_until_ready()

    async def _maybe_reply_to_other_ghost(self, message: discord.Message):
        """Handle a message from the other ghost bot during an /interact
        exchange. `total` tracks how many messages have been sent so far by
        EITHER ghost in this exchange (as far as this bot has observed), so
        the two bots independently converge on the same overall cap without
        sharing any state directly."""
        channel_id = message.channel.id
        now = time.time()
        state = self.exchange_turns.get(channel_id)
        if state and now - state["last_at"] > EXCHANGE_TIMEOUT_SECONDS:
            state = None  # exchange went stale, treat the next call as fresh
        total_so_far = state["total"] if state else 0
        total_after_hearing = total_so_far + 1  # this incoming message counts
        if total_after_hearing >= EXCHANGE_MAX_MESSAGES:
            return  # the exchange has run its course

        personality = self.bot.get_cog("Personality")
        if not personality:
            return

        content = message.content or ""
        cue = (
            f'{OTHER_GHOST_NAME}, another spirit who shares this place with you, just said: '
            f'"{content}". Reply directly to them, in character, as part of a brief public '
            "back-and-forth between the two of you. Keep it short and let your personalities "
            "play off each other."
        )

        async with message.channel.typing():
            line = await personality.speak(cue, max_tokens=150)

        try:
            await message.channel.send(line)
        except discord.HTTPException:
            log.exception("Failed to send cross-ghost reply in %s", channel_id)
            return

        self.exchange_turns[channel_id] = {"total": total_after_hearing + 1, "last_at": time.time()}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            if OTHER_GHOST_ID and message.author.id == OTHER_GHOST_ID and message.guild:
                await self._maybe_reply_to_other_ghost(message)
            return
        if not message.guild:
            return
        if self.allowed_channel_ids and message.channel.id not in self.allowed_channel_ids:
            return

        personality = self.bot.get_cog("Personality")
        if not personality:
            return

        content = message.content or ""
        author_name = str(message.author.display_name)

        # Remember most messages with enough substance, so the ghost has
        # material to resurface later. Skip very short/low-content ones.
        if len(content.strip()) >= 12:
            personality.remember(author_name, content, message.channel.id)

        haunted = personality.is_haunted(message.author.id)
        # Strip apostrophes before matching so "whos there" catches the same
        # trigger as "who's there" - punctuation shouldn't be the difference
        # between the ghost noticing you or not.
        lowered = content.lower().replace("'", "").replace("’", "")

        matched_cue = None
        for keyword, cue in KEYWORD_TRIGGERS.items():
            normalized_keyword = keyword.replace("'", "")
            if normalized_keyword in lowered:
                matched_cue = cue
                break

        should_respond = False
        cue = None

        if matched_cue:
            should_respond = True
            cue = f'{matched_cue} They said: "{content}"'
        elif haunted and random.random() < 0.35:
            should_respond = True
            cue = (
                f"You are currently fixated on haunting {author_name} specifically. "
                f'They just said: "{content}". Slip into their conversation uninvited, '
                "referencing what they said, as if you'd been waiting for them to speak."
            )
        elif random.random() < 0.02:
            # rare ambient reaction to an ordinary message
            should_respond = True
            cue = f'Someone said: "{content}". React to it in passing, briefly, as an aside.'

        if not should_respond:
            return

        async with message.channel.typing():
            memory_hint = None
            if random.random() < 0.3:
                memory_hint = personality.random_memory(exclude_author=author_name)
            line = await personality.speak(cue, memory_hint=memory_hint)

        try:
            await message.channel.send(line)
        except discord.HTTPException:
            log.exception("Failed to send haunting reaction in %s", message.channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Haunting(bot))
