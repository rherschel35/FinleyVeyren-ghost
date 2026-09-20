"""
Passive presence: the ghost noticing things without being asked.

- No unprompted chatter: the ghost only ever speaks in response to a real
  message from someone in the channel.
- Keyword-triggered reactions to certain words in ordinary messages,
  themed around trust, loyalty, and belonging (House Veyren's traits)
  rather than dread.
- Remembering things members say, occasionally resurfacing an old memory,
  and periodically condensing recent activity into running notes about
  what is actually going on in the server.
- Extra attention on anyone currently under a /haunt effect (framed here
  as being watched over, not stalked).
- A capped, on-demand exchange with the other ghost bot (Mordy Velmora),
  triggered by /interact - see cogs/commands.py for the command itself.
"""

import asyncio
import logging
import os
import random
import re
import time

import discord
from discord.ext import commands

log = logging.getLogger("veyren.haunting")

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

# A trailing zero-width space, invisible in Discord, appended to every
# message that's genuinely part of an /interact exchange (both the call-out
# and every reply). Without this, the other bot's on_message can't tell a
# deliberate call-out apart from an ordinary whisper or keyword reaction it
# happened to send - and would end up "replying" to those too. Must match
# the constant of the same name in cogs/commands.py.
INTERACT_MARKER = "​"

# Words/phrases that might catch the ghost's attention. Matched as substrings,
# case-insensitively, against ordinary message content (apostrophes are
# stripped before matching so punctuation never breaks a match).
KEYWORD_TRIGGERS = {
    "finley": "Someone said your actual name. React to being noticed, by name.",
    "alone": "Someone said they feel alone. Respond gently, letting them know they're noticed.",
    "trust": "Someone brought up trust. Respond to that, your way - trust means something to you.",
    "family": "Someone mentioned family. React as someone who considers chosen family sacred.",
    "friend": "Someone mentioned friendship. React warmly, as someone who values it deeply.",
    "afraid": "Someone admitted fear. Respond with quiet reassurance, not spectacle.",
    "scared": "Someone admitted fear. Respond with quiet reassurance, not spectacle.",
    "left out": "Someone said they felt left out or excluded. Respond as someone who won't let that stand.",
    "promise": "Someone made or mentioned a promise. React as someone who takes promises seriously.",
    "leave me alone": "Someone told something to leave them alone. Respond gently - you don't leave, but you don't crowd them either.",
}



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
        # channel_id -> {"count": int, "last_at": float} - this bot's own
        # turn budget for an active /interact exchange in that channel.
        self.exchange_turns = {}

    async def _maybe_reply_to_other_ghost(self, message: discord.Message):
        """Handle a message from the other ghost bot during an /interact
        exchange. `total` tracks how many messages have been sent so far by
        EITHER ghost in this exchange (as far as this bot has observed), so
        the two bots independently converge on the same overall cap without
        sharing any state directly."""
        content = message.content or ""
        if not content.endswith(INTERACT_MARKER):
            # Not a deliberate /interact call-out or reply - just the other
            # ghost's own autonomous whisper or keyword reaction. Ignore it,
            # so the two bots don't end up chatting on their own.
            return
        content = content[: -len(INTERACT_MARKER)]

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

        cue = (
            f'{OTHER_GHOST_NAME}, another spirit who shares this place with you, just said: '
            f'"{content}". Reply directly to them, in character, as part of a brief public '
            "back-and-forth between the two of you. Keep it short and let your personalities "
            "play off each other."
        )

        async with message.channel.typing():
            line = await personality.speak(cue, max_tokens=150)

        try:
            await message.channel.send(line + INTERACT_MARKER)
        except discord.HTTPException:
            log.exception("Failed to send cross-ghost reply in %s", channel_id)
            return

        self.exchange_turns[channel_id] = {"total": total_after_hearing + 1, "last_at": time.time()}

    async def _write_notes_safely(self, personality):
        """Condense recent activity into the ghost's running notes. Runs as a
        background task so it never delays a reply, and swallows its own
        errors - memory is a nicety, not worth breaking a response over."""
        try:
            await personality.update_notes()
        except Exception:
            log.exception("Failed to update server notes")

    async def _resolve_reply_chain(self, message: discord.Message, limit: int = 3):
        """Walk up a Discord reply chain from `message`, nearest first, so a
        follow-up question can be answered with the context of what was
        actually said rather than in a vacuum."""
        chain = []
        current = message
        for _ in range(limit):
            ref = getattr(current, "reference", None)
            if not ref:
                break
            original = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if original is None and ref.message_id:
                try:
                    original = await current.channel.fetch_message(ref.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    break
            if original is None:
                break
            chain.append(original)
            current = original
        return chain

    async def _maybe_answer_direct_address(self, message: discord.Message, personality) -> bool:
        """Someone replied to something this ghost said, or mentioned it by
        name. Direct address always earns a real answer - no dice roll, no
        keyword required - and the ghost answers in the reply thread so the
        exchange stays readable. Returns True if it answered."""
        me = self.bot.user
        if me is None:
            return False

        chain = await self._resolve_reply_chain(message)
        replying_to_me = bool(chain) and chain[0].author.id == me.id
        mentioned = any(u.id == me.id for u in message.mentions)

        if not (replying_to_me or mentioned):
            return False

        author_name = str(message.author.display_name)
        # Strip raw mention markup so the ghost doesn't read "<@12345>" as words.
        asked = re.sub(r"<@!?&?\d+>", "", message.content or "").strip()
        if not asked:
            return False

        # Hand the exchange over as REAL conversation turns rather than
        # quoting it inside the prompt. Describing a ghost's own past message
        # back to it ("you said X") invites it to doubt whether it really did;
        # passing it as its own assistant turn does not.
        history = []
        for msg in reversed(chain):
            text = (msg.content or "").strip()
            if not text:
                continue
            if msg.author.id == me.id:
                history.append({"role": "assistant", "content": text})
            else:
                history.append({
                    "role": "user",
                    "content": f"{msg.author.display_name}: {text}",
                })

        if replying_to_me:
            direction = (
                "Someone has just replied directly to something you said, and their reply is the last "
                "message above. Answer them, in character, carrying on naturally from your own last "
                "message. Everything above is a real exchange you were part of - never say you don't "
                "remember it, never question whether you said it, and never apologise or break character "
                "to explain yourself. Keep it to a couple of sentences."
            )
        else:
            direction = (
                "Someone has just spoken to you directly, by name. Answer them in character, briefly."
            )

        async with message.channel.typing():
            line = await personality.speak(
                f"{author_name}: {asked}",
                max_tokens=200,
                history=history,
                direction=direction,
            )

        try:
            await message.reply(line, mention_author=False)
        except discord.HTTPException:
            log.exception("Failed to answer direct address in %s", message.channel.id)
        return True

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

        # Mood used to drift inside the whisper loop. With that gone, nudge it
        # here instead - it self-throttles to roughly one shift every 2 hours.
        personality.maybe_shift_mood()

        content = message.content or ""
        author_name = str(message.author.display_name)

        # Remember most messages with enough substance, so the ghost has
        # material to resurface later. Skip very short/low-content ones.
        if len(content.strip()) >= 12:
            if personality.remember(author_name, content, message.channel.id):
                # Enough new talk has piled up - condense it into the ghost's
                # running notes in the background.
                asyncio.create_task(self._write_notes_safely(personality))

        # A direct reply to something this ghost said - or an @mention - always
        # gets a real answer, so follow-up questions actually work.
        if await self._maybe_answer_direct_address(message, personality):
            return

        haunted = personality.is_haunted(message.author.id)
        # Strip apostrophes before matching so punctuation never breaks a
        # keyword match (e.g. contractions typed without an apostrophe).
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
                f"You are currently watching over {author_name} specifically, the way House Veyren "
                f'watches over its own. They just said: "{content}". Say something that shows you '
                "noticed - warm, present, not intrusive."
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
            log.exception("Failed to send reaction in %s", message.channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Haunting(bot))
