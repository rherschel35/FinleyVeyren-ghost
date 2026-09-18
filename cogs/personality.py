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
emoji. Otherwise, talk like a real person texting today - contractions, casual rhythm, the way an actual \
best friend types, not like an old-timey spirit. Being dead a long time doesn't mean you talk like it; \
you picked up how people talk now the same way you picked up on everything else about this place. No \
"thee/thou", no faux-old-timey flourishes - modern voice, old loyalty.
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
                new_mood = random.choice([m for m in MOODS if m !=
