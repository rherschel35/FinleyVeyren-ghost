"""
The ghost's diary: long-term memory, one entry per day.

Short-term memory (the last 200 messages and the running notes) covers the
last day or two. This covers months. Every message the ghost hears is kept
in a buffer for the day it was said; once the day is over (or the buffer
gets very full), the ghost writes a short diary entry about it and the raw
messages are let go.

When the ghost speaks:
- the last week of entries is always in mind, and
- older entries are pulled in when what's being said points at them -
  "last month", "on Halloween", "remember when Cassy...", or simply words
  that match what happened that day.

This is not a cog; Personality inherits it. Personality supplies
DIARY_GHOST_NAME, DIARY_MODEL, self.client, self.state and self.save_state.
"""

import asyncio
import datetime as dt
import logging
import re

try:
    from zoneinfo import ZoneInfo
    SERVER_TZ = ZoneInfo("America/Chicago")
except Exception:  # no tz database available - fall back to UTC
    SERVER_TZ = dt.timezone.utc

log = logging.getLogger("ghost.diary")

DIARY_KEEP_DAYS = 400        # how many daily entries are kept (over a year)
BUFFER_FLUSH_AT = 300        # a very busy day gets written up in parts
BUFFER_MSG_CHARS = 200
RECENT_DAYS_IN_MIND = 7      # always carried into a reply
MAX_RECALLED = 3             # older entries pulled in per reply

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9

HOLIDAYS = {
    "halloween": (10, 31), "christmas": (12, 25), "christmas eve": (12, 24),
    "new year": (1, 1), "new years": (1, 1), "new year's": (1, 1),
    "valentine": (2, 14), "valentines": (2, 14), "valentine's": (2, 14),
    "fourth of july": (7, 4), "4th of july": (7, 4),
}

STOPWORDS = set("""
a an the and or but if then so of to in on at by for with from about into over after before
is are was were be been being am do does did done have has had having i me my we our you your
he him his she her it its they them their this that these those what which who whom whose
when where why how all any both each few more most other some such no nor not only own same
than too very can will just dont should now ever still also really like get got go going went
yes yeah ok okay oh um uh hey hi hello lol lmao remember remembers recall time day days week
weeks month months year years ago last back thing things say said tell told know think
""".split())


def today_str(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(SERVER_TZ)).astimezone(SERVER_TZ).date().isoformat()


def date_of_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, SERVER_TZ).date().isoformat()


def nice_date(iso: str, today: str | None = None) -> str:
    d = dt.date.fromisoformat(iso)
    t = dt.date.fromisoformat(today or today_str())
    delta = (t - d).days
    label = d.strftime("%a %b ") + str(d.day)
    if delta == 1:
        return f"yesterday, {label}"
    if 1 < delta < 7:
        return f"{label}, {delta} days ago"
    if delta >= 7:
        weeks = delta // 7
        if delta < 60:
            return f"{label}, about {weeks} week{'s' if weeks != 1 else ''} ago"
        return f"{label}, about {round(delta / 30)} months ago"
    return label


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower().replace("’", "'"))
            if len(w) > 2 and w not in STOPWORDS}


def dates_pointed_at(text: str, today: str | None = None) -> set[str]:
    """Dates the text refers to: 'Oct 12', 'october 12th', '10/12', a holiday."""
    t = dt.date.fromisoformat(today or today_str())
    low = (text or "").lower().replace("’", "'")
    found = set()

    def add(month, day):
        for year in (t.year, t.year - 1):
            try:
                d = dt.date(year, month, day)
            except ValueError:
                continue
            if d <= t:
                found.add(d.isoformat())
                break

    for name, (m, d) in HOLIDAYS.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            add(m, d)
    for m in re.finditer(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", low):
        month = MONTHS.get(m.group(1))
        if month:
            add(month, int(m.group(2)))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})\b", low):
        mo, da = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            add(mo, da)
    return found


def window_pointed_at(text: str, today: str | None = None) -> tuple[int, int] | None:
    """(oldest, newest) days-ago window for phrases like 'last month'."""
    low = (text or "").lower()
    if re.search(r"\blast month\b|\ba month ago\b", low):
        return (45, 20)
    if re.search(r"\b(two|2|couple(?: of)?) weeks ago\b", low):
        return (18, 10)
    if re.search(r"\blast week\b|\ba week ago\b", low):
        return (13, 5)
    m = re.search(r"\b(\d{1,2}|two|three|four|five|six) months? ago\b", low)
    if m:
        n = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}.get(m.group(1)) or int(m.group(1))
        return (n * 30 + 15, n * 30 - 15)
    return None


class DiaryMixin:
    DIARY_GHOST_NAME = "the ghost"
    DIARY_MODEL = "claude-haiku-4-5-20251001"

    # ---------- recording ----------

    def _diary_state(self):
        self.state.setdefault("diary", [])            # [{"date", "text"}]
        self.state.setdefault("day_buffer", [])       # [{"author", "content", "ts"}]
        self.state.setdefault("day_buffer_date", None)
        return self.state

    def diary_record(self, author: str, content: str, ts: float):
        """Called for every remembered message. Never blocks; a finished day
        is written up in the background."""
        st = self._diary_state()
        day = date_of_ts(ts)
        if not st["day_buffer_date"]:
            st["day_buffer_date"] = day
        if day != st["day_buffer_date"] and st["day_buffer"]:
            self._schedule_flush()
        st["day_buffer"].append({"author": author, "content": (content or "")[:BUFFER_MSG_CHARS], "ts": ts})
        st["day_buffer_date"] = day
        if len(st["day_buffer"]) >= BUFFER_FLUSH_AT:
            self._schedule_flush()

    def _schedule_flush(self):
        """Hand the buffered messages off to be written up, grouped by day."""
        st = self._diary_state()
        pending = st["day_buffer"]
        if not pending:
            return
        st["day_buffer"] = []
        st["day_buffer_date"] = None
        by_day: dict[str, list] = {}
        for m in pending:
            by_day.setdefault(date_of_ts(m["ts"]), []).append(m)
        st.setdefault("diary_pending", []).extend(
            {"date": d, "messages": msgs} for d, msgs in sorted(by_day.items())
        )
        try:
            asyncio.get_running_loop().create_task(self.write_pending_diary())
        except RuntimeError:
            pass  # no loop (tests / startup) - it'll be written on the next run

    async def write_pending_diary(self):
        if getattr(self, "_diary_writing", False):
            return
        self._diary_writing = True
        try:
            st = self._diary_state()
            while st.get("diary_pending"):
                chunk = st["diary_pending"][0]
                ok = await self._write_entry(chunk["date"], chunk["messages"])
                if not ok:
                    break  # keep it pending; try again next time
                st["diary_pending"].pop(0)
                self.save_state()
        finally:
            self._diary_writing = False

    async def _write_entry(self, day: str, messages: list) -> bool:
        if not messages:
            return True
        if not getattr(self, "client", None):
            return False
        transcript = "\n".join(f'{m["author"]}: {m["content"]}' for m in messages)
        existing = next((e for e in self.state["diary"] if e["date"] == day), None)
        also = ""
        if existing:
            also = ("\n\nYou already wrote this about the same day earlier - add only what's new, "
                    f"don't repeat it:\n{existing['text']}")
        system = (
            f"You are {self.DIARY_GHOST_NAME}, a ghost who lives in a Discord server called Velmora. "
            f"Below is what people said there on {dt.date.fromisoformat(day).strftime('%A, %B')} "
            f"{dt.date.fromisoformat(day).day}. Write your private diary entry for that day: 2 to 5 "
            "sentences, in your own voice, recording what actually happened - who was around (by the "
            "names shown), what they talked about or did, anything decided, won, started or finished, "
            "and anything memorable or funny. Be specific: names, events, results. Record only what "
            "is in the transcript; never invent. If truly nothing happened worth remembering, reply "
            "with the single word NOTHING. Output only the entry." + also
        )
        try:
            resp = await self.client.messages.create(
                model=self.DIARY_MODEL, max_tokens=320, system=system,
                messages=[{"role": "user", "content": transcript}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception:
            log.exception("Failed to write diary entry for %s", day)
            return False
        if not text or text.upper().strip(" .") == "NOTHING":
            return True
        if existing:
            existing["text"] = (existing["text"] + " " + text).strip()
        else:
            self.state["diary"].append({"date": day, "text": text})
            self.state["diary"].sort(key=lambda e: e["date"])
            self.state["diary"] = self.state["diary"][-DIARY_KEEP_DAYS:]
        log.info("Diary entry written for %s (%d entries kept)", day, len(self.state["diary"]))
        return True

    def diary_backfill_from_memories(self):
        """First run only: seed the diary from the messages already remembered,
        so it doesn't start completely empty."""
        st = self._diary_state()
        if st.get("diary_backfilled"):
            return
        st["diary_backfilled"] = True
        today = today_str()
        mems = [m for m in self.state.get("memories", []) if m.get("ts")]
        past = [m for m in mems if date_of_ts(m["ts"]) < today]
        todays = [m for m in mems if date_of_ts(m["ts"]) == today]
        if past:
            by_day: dict[str, list] = {}
            for m in past:
                by_day.setdefault(date_of_ts(m["ts"]), []).append(
                    {"author": m["author"], "content": m["content"][:BUFFER_MSG_CHARS], "ts": m["ts"]})
            st.setdefault("diary_pending", []).extend(
                {"date": d, "messages": msgs} for d, msgs in sorted(by_day.items()))
        if todays and not st["day_buffer"]:
            st["day_buffer"] = [{"author": m["author"], "content": m["content"][:BUFFER_MSG_CHARS],
                                 "ts": m["ts"]} for m in todays]
            st["day_buffer_date"] = today
        self.save_state()

    # ---------- recalling ----------

    def diary_block(self, prompt_text: str) -> str:
        """The part of the system prompt carrying long-term memory."""
        st = self._diary_state()
        today = today_str()
        diary = [e for e in st["diary"] if e["date"] < today]
        if not diary:
            return ""

        t = dt.date.fromisoformat(today)
        def age(e):
            return (t - dt.date.fromisoformat(e["date"])).days

        recent = [e for e in diary if age(e) <= RECENT_DAYS_IN_MIND]
        older = [e for e in diary if age(e) > RECENT_DAYS_IN_MIND]

        recalled = []
        wanted_dates = dates_pointed_at(prompt_text, today)
        recalled += [e for e in diary if e["date"] in wanted_dates and e not in recent]

        window = window_pointed_at(prompt_text, today)
        if window:
            oldest, newest = window
            in_window = [e for e in older if newest <= age(e) <= oldest]
            words = _words(prompt_text)
            in_window.sort(key=lambda e: len(words & _words(e["text"])), reverse=True)
            recalled += [e for e in in_window if e not in recalled][:MAX_RECALLED]

        words = _words(prompt_text)
        if words:
            scored = [(len(words & _words(e["text"])), e) for e in older if e not in recalled]
            scored = [(s, e) for s, e in scored if s >= 2 or (s >= 1 and len(words) <= 2)]
            scored.sort(key=lambda p: (p[0], p[1]["date"]), reverse=True)
            recalled += [e for _, e in scored]
        recalled = recalled[:MAX_RECALLED]

        out = [f"\n\nTODAY IS {t.strftime('%A, %B')} {t.day}, {t.year}."]
        if recent:
            out.append(
                "YOUR DIARY FROM THE PAST WEEK - what actually happened here, in your own words:\n"
                + "\n".join(f"- {nice_date(e['date'], today)}: {e['text']}" for e in recent)
            )
        if recalled:
            recalled.sort(key=lambda e: e["date"])
            out.append(
                "OLDER DIARY ENTRIES THAT JUST CAME BACK TO YOU, because of what's being said:\n"
                + "\n".join(f"- {nice_date(e['date'], today)}: {e['text']}" for e in recalled)
            )
        out.append(
            "These are your real memories. If someone asks about the past, answer from them "
            "confidently and specifically. If they ask about something that isn't in them, you can "
            "say it's hazy - never invent what happened."
        )
        return "\n\n".join(out)
