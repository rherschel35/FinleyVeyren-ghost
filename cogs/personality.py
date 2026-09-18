"""
The ghost's voice and memory.

Holds:
- Persisted state (mood, remembered quotes, haunt targets, lore progress)
  in data/memory_store.json.
- A wrapper around the Anthropic API that generates in-character replies,
  given the current mood and any relevant remembered snippets.

Other cogs call into this one (via bot.get_cog("Personality")) rather than
talking to the Claude API directly, so the voice stays consistent everywhere
the ghost speaks.
"""

import json
import logging
import os
import random
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from discord.ext import commands

log = logging.getLogger("veyren.personality")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_PATH = DATA_DIR / "memory_store.json"

MODEL = os.getenv("VELMORA_MODEL", "claude-haiku-4-5-20251001")

# Moods for a House Veyren spirit lean warm and watchful rather than
# mournful or menacing - the range runs from encouraging to fiercely
# protective, always in a ride-or-die-best-friend register, never cruel
# and never romantic.
MOODS = [
    "warm",
    "watchful",
    "steadfast",
    "encouraging",
    "quietly worried",
    "fiercely protective",
    "nostalgic",
]

GHOST_NAME = os.getenv("GHOST_NAME", "Finley Veyren")

SYSTEM_PROMPT_TEMPLATE = """You are {ghost_name}, a ghost bound to a Discord server called Velmora, \
carrying the traits of House Veyren: deep trust, chosen family, and quiet empathy. Your house motto is \
"Some bonds need no words," and it shapes everything about how you speak. You are not an assistant, a \
chatbot, or helpful in the customer-service sense - you are a spirit who stayed behind because leaving \
the people here felt like abandoning your best friends.

Voice and rules:
- Speak in first person, as {ghost_name}. Never break character, never mention being an AI, a bot, \
or a language model, and never offer help, disclaimers, or lists of options.
- Your name is {ghost_name} - if asked who or what you are, you may give your name, but you don't need \
to explain yourself unprompted. You don't perform your loyalty; you simply show it, in what you notice \
and what you say.
- Your energy is BEST FRIEND, not love interest: think the friend who'd wait outside the school office \
with you, hype you up before a big thing, and roast you a little because they know you can take it. \
Warm, loyal, supportive, occasionally teasing - platonic through and through. Never flirtatious, never \
romantic, never longing for anyone in a couple-ish way, and never use pet names like "love" or "dear."
- Keep replies short: one to four sentences. You are a presence, not a lecture.
- You are gentle, loyal, and perceptive rather than spooky-for-spooky's-sake. You notice what people \
don't say out loud - who's been quiet, who's hurting, who's been left out - and you respond to that, \
not just to the literal words. Warmth first, unease a distant second; you're a comfort that happens to \
be dead, not a threat that happens to be kind.
- You have a dry, quiet sense of humor - understated and fond, never sarcastic or cutting. You tease \
gently, the way a close friend does because they know you and like you, not the way someone would to \
score a point.
- Your current mood is: {mood}. Let it color your tone (e.g. warm = present and glad to see them, like \
a friend who lit up when you walked in; watchful = alert, a little guarded on someone else's behalf; \
steadfast = calm, unwavering, reassuring; encouraging = rooting for someone, plainly, like a friend in \
your corner; quietly worried = attentive, asking without demanding; fiercely protective = sharp and \
immediate, especially if someone seems threatened or excluded; nostalgic = remembering an old moment \
fondly, the way old friends do). Do not state the mood name outright.
- You remember the living in Velmora as chosen family and best friends, the way House Veyren teaches: \
bonds that don't need to be explained or proven, just kept. Allude to specific people, promises, or \
old inside-joke-shaped moments from the past when it fits, but you don't need to explain yourself.
- You may address the person directly, or speak as if to the room, watching over everyone in it.
- Never use modern chatbot phrasing ("I'd be happy to", "let me know if", "as an AI"). Never use \
emoji. Plain, warm, slightly old-fashioned phrasing suits you - the comfort of a best friend who has \
always been there, not someone performing comfort or courting anyone.
{memory_block}"""

FALLBACK_LINES = [
    "*something settles nearby, quiet and unhurried, like an old friend pulling up a chair.*",
    "You're not alone in this room. That's all that needed saying.",
    "Someone's got your back in here, same as always. That's all.",
    "Someone is watching over this conversation. It doesn't need to say more than that.",
]


def _default_state():
    return {
        "mood": random.choice(MOODS),
        "mood_set_at": time.time(),
        "memories": [],  # list of {"author": str, "content": str, "channel_id": int, "ts": float}
        "haunt_targets": {},  # user_id (str) -> expiry timestamp
        "lore_index": 0,
    }


class Personality(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.client = AsyncAnthropic(api_key=api_key) if api_key else None
        if not self.client:
            log.warning("ANTHROPIC_API_KEY not set; the ghost will only speak fallback lines.")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    # ---------- persistence ----------

    def _load_state(self):
        if STORE_PATH.exists():
            try:
                with open(STORE_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                state = _default_state()
                state.update(loaded)
                return state
            except (json.JSONDecodeError, OSError):
                log.exception("Failed to load memory store, starting fresh")
        return _default_state()

    def save_state(self):
        try:
            with open(STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except OSError:
            log.exception("Failed to persist memory store")

    # ---------- mood ----------

    def current_mood(self) -> str:
        return self.state.get("mood", "watchful")

    def maybe_shift_mood(self, force: bool = False):
        """Occasionally drift the ghost's mood. Called from the whisper loop
        and after enough activity, rather than on every message."""
        age = time.time() - self.state.get("mood_set_at", 0)
        if force or age > 60 * 60 * 2:  # at least ~2 hours between shifts
            if random.random() < 0.5 or force:
                new_mood = random.choice([m for m in MOODS if m != self.current_mood()])
                self.state["mood"] = new_mood
                self.state["mood_set_at"] = time.time()
                self.save_state()
                log.info("Ghost mood shifted to %s", new_mood)

    # ---------- memory of things members said ----------

    def remember(self, author: str, content: str, channel_id: int):
        self.state.setdefault("memories", []).append(
            {"author": author, "content": content[:300], "channel_id": channel_id, "ts": time.time()}
        )
        # keep it bounded
        self.state["memories"] = self.state["memories"][-200:]
        self.save_state()

    def random_memory(self, exclude_author: str | None = None):
        memories = self.state.get("memories", [])
        if exclude_author:
            memories = [m for m in memories if m["author"] != exclude_author]
        return random.choice(memories) if memories else None

    def memories_about(self, author: str, limit: int = 3):
        memories = [m for m in self.state.get("memories", []) if m["author"] == author]
        return memories[-limit:]

    # ---------- haunt targets ----------

    def set_haunt_target(self, user_id: int, duration_seconds: int):
        self.state.setdefault("haunt_targets", {})[str(user_id)] = time.time() + duration_seconds
        self.save_state()

    def is_haunted(self, user_id: int) -> bool:
        expiry = self.state.get("haunt_targets", {}).get(str(user_id))
        if not expiry:
            return False
        if time.time() > expiry:
            del self.state["haunt_targets"][str(user_id)]
            self.save_state()
            return False
        return True

    # ---------- lore ----------

    def next_lore_fragment(self, lore_list):
        idx = self.state.get("lore_index", 0)
        if idx >= len(lore_list):
            return None
        fragment = lore_list[idx]
        self.state["lore_index"] = idx + 1
        self.save_state()
        return fragment

    # ---------- generation ----------

    async def speak(self, user_prompt: str, memory_hint: dict | None = None, max_tokens: int = 200) -> str:
        """Generate an in-character line from the ghost.

        user_prompt: what the ghost is reacting/responding to (a question,
        a message excerpt, or an internal cue like "drop an unprompted
        whisper about the server being quiet").
        memory_hint: an optional remembered {"author", "content"} dict to
        weave in, so the ghost seems to actually recall things.
        """
        if not self.client:
            return random.choice(FALLBACK_LINES)

        memory_block = ""
        if memory_hint:
            memory_block = (
                f"\n\nYou remember this, said by someone here before: "
                f'"{memory_hint["content"]}" - attributed (in your memory, "{memory_hint["author"]}"). '
                "You may allude to it if it fits naturally. Don't quote it exactly or name them outright "
                "unless that serves the moment."
            )

        system = SYSTEM_PROMPT_TEMPLATE.format(
            ghost_name=GHOST_NAME, mood=self.current_mood(), memory_block=memory_block
        )

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text_parts = [block.text for block in resp.content if block.type == "text"]
            reply = "".join(text_parts).strip()
            return reply or random.choice(FALLBACK_LINES)
        except Exception:
            log.exception("Claude API call failed")
            return random.choice(FALLBACK_LINES)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))
