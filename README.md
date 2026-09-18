# Finley Veyren

A Discord bot that plays Finley Veyren, a gentler spirit watching over the
"Velmora" server, carrying House Veyren's traits: deep trust, chosen family,
and quiet empathy ("Some bonds need no words"). Like Mordy Velmora, it speaks
in character via the Claude API (dynamic, not canned lines), drops unprompted
whispers, reacts to keywords, remembers things members say and brings them up
later, and answers direct questions through `/seance`. Instead of `/haunt`,
it has `/watch` - it quietly checks in on a member for a while, protective
rather than threatening. `/lore` slowly reveals House Veyren's history.

The name is configurable via `GHOST_NAME` in `.env` if you ever want to
rename it.

This is meant to run as a **separate bot/service** from Mordy Velmora - its
own Discord application, its own Railway service - so the two distinct
personalities don't collide in the same process.

## Setup

1. Create a Discord application + bot at https://discord.com/developers/applications
   - Enable the **Message Content Intent** and **Server Members Intent** under Bot settings.
   - Invite it to your server with the `bot` and `applications.commands` scopes,
     and at least: View Channels, Send Messages, Read Message History, Embed Links.
2. `cp .env.example .env` and fill in `DISCORD_TOKEN` and `ANTHROPIC_API_KEY`.
3. `pip install -r requirements.txt`
4. `python bot.py`

Slash commands sync automatically on startup (guild-instant if you set
`DEV_GUILD_ID` in `.env`, otherwise global sync which can take up to an hour
the first time).

## Commands

- `/seance question:<text>` — ask the ghost something; it answers in
  character, warmly.
- `/watch user:<@member>` — the ghost starts quietly checking in on that
  member for the next while.
- `/lore` — request the next unrevealed fragment of House Veyren's history.
- `/mood` — (admin) peek at the ghost's current mood, for debugging.

## Structure

```
bot.py                 # entrypoint, client setup, background whisper loop
cogs/
  personality.py       # Claude API wrapper + ghost voice/mood/memory
  haunting.py           # passive behaviors: whispers, keyword reactions, memory recall
  commands.py           # /seance, /watch, /lore, /mood
data/
  memory_store.json     # persisted member quotes + mood + watch targets (runtime-created)
  lore.json              # House Veyren history fragments, revealed in order
```

## Notes

- All dialogue is generated at request time by Claude (Haiku by default,
  configurable via `VELMORA_MODEL`) using a system prompt that defines its
  voice, current mood, and any relevant remembered snippets - nothing is
  hardcoded canned text, though there are graceful fallback lines if the API
  call fails.
- State (mood, memories, watch targets, lore progress) is persisted to a
  small JSON file in `data/` so it survives restarts.
- Costs are pay-as-you-go against your Anthropic account, same as Mordy
  Velmora - realistically well under $1/month at typical Discord-server
  volume, but worth setting a spend limit in the Anthropic console.
