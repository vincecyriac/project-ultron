"""
Assembles the context for the return-to-desk briefing card.

This module only gathers and phrases local facts. It does not call a model:
the hub already owns a skeleton-then-hydrate pipeline that mounts a card
instantly and fills it from the widget generator, and routing the briefing
through that same path means one code path to maintain and one place where
card HTML gets sanitised.

Anything that cannot be read locally — markets, weather — is asked for by name
so the generator's search grounding fetches it. Nothing is invented here.
"""

import asyncio
import os
import shutil
from datetime import datetime

try:
    import psutil
except ImportError:
    psutil = None

import sentry_personal

# Weather needs somewhere to be about. There is no location in this project and
# guessing one produces confidently wrong forecasts, so the section is simply
# omitted unless the operator names a place.
USER_LOCATION = os.getenv("ULTRON_LOCATION", "").strip()

BRIEFING_WIDGET_ID = "daily_briefing"
CALENDAR_LOOKAHEAD_DAYS = 1
AGENDA_MAX_CHARS = 1200


def greeting_phase(now: datetime = None) -> str:
    hour = (now or datetime.now()).hour
    if hour < 12:
        return "Morning"
    if hour < 17:
        return "Afternoon"
    return "Evening"


def briefing_title(now: datetime = None) -> str:
    now = now or datetime.now()
    return f"INTELLIGENCE BRIEFING // {now.strftime('%d %b %Y').upper()}"


def system_health() -> str:
    if psutil is None:
        return "System health: unavailable (psutil not installed)."
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        used_gb = (vm.total - vm.available) / (1024 ** 3)
        total_gb = vm.total / (1024 ** 3)
        disk = shutil.disk_usage(os.path.expanduser("~"))
        free_gb = disk.free / (1024 ** 3)
        disk_total_gb = disk.total / (1024 ** 3)
        parts = [f"CPU {cpu:.0f}%",
                 f"memory {used_gb:.1f} of {total_gb:.0f} GB used",
                 f"disk {free_gb:.0f} GB free of {disk_total_gb:.0f} GB"]
        battery = getattr(psutil, "sensors_battery", lambda: None)()
        if battery is not None:
            state = "charging" if battery.power_plugged else "on battery"
            parts.append(f"battery {battery.percent:.0f}% ({state})")
        return "System health: " + ", ".join(parts) + "."
    except Exception as e:
        return f"System health: unavailable ({e})."


async def agenda() -> str:
    """Today's calendar. EventKit is synchronous and can block, so it runs off-loop."""
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(
            None, lambda: sentry_personal.get_calendar_events(
                days_ahead=CALENDAR_LOOKAHEAD_DAYS))
    except Exception as e:
        return f"Agenda: calendar unavailable ({e})."
    raw = (raw or "").strip()
    if not raw:
        return "Agenda: nothing scheduled in the next 24 hours."
    # sentry_personal reports permission and lookup problems as an "[Error]"
    # string rather than raising. Passing that through would print the raw
    # diagnostic into the card as if it were a calendar entry.
    if raw.startswith("[Error]"):
        return ("Agenda: unavailable — the calendar could not be read. Omit the "
                "schedule column entirely; do not invent events.")
    return "Agenda (verbatim from macOS Calendar, do not alter times or titles):\n" \
           + raw[:AGENDA_MAX_CHARS]


async def build_briefing_context(now: datetime = None) -> str:
    """The full instruction handed to the card generator."""
    now = now or datetime.now()
    health, sched = system_health(), await agenda()

    lines = [
        f"This card greets Vince as he returns to his desk. It is {now.strftime('%A, %B %d')} "
        f"at {now.strftime('%I:%M %p').lstrip('0')} — the {greeting_phase(now).lower()}.",
        "",
        "Build a three-column briefing at a glance. Left: the date, time and the system "
        "figures below. Centre: today's schedule as a timeline feed, one row per event, "
        "soonest first. Right: market pulse and weather.",
        "",
        health,
        "",
        sched,
        "",
        "Look up and include today's move for the major indices (S&P 500, Nasdaq, Dow) "
        "with the direction and percentage.",
    ]
    if USER_LOCATION:
        lines.append(f"Look up the current weather and today's high and low for {USER_LOCATION}, "
                     f"and show it as a compact stat block.")
    else:
        lines.append("No location is configured, so omit weather entirely rather than "
                     "guessing a city. Do not invent a forecast.")
    lines += [
        "",
        "FACTS ONLY. Every value on this card must come from the block above or from a "
        "search result. You do not know this machine's hostname, its uptime, its serial, "
        "its security posture, its network, or any build or version string, so never show "
        "one. Do not invent status words like NOMINAL or OPTIMAL to fill a gap, and do not "
        "make up an identifier for the system.",
        "If a column has no real data — no agenda, no weather — drop that column and build "
        "the card from the two that do. A shorter honest card is correct; a padded one is not.",
    ]
    return "\n".join(lines)


def spoken_welcome(now: datetime = None) -> str:
    """The voice cue. Deliberately under ten words — detail belongs on screen."""
    return f"Welcome back, Vince. Your {greeting_phase(now).lower()} briefing is up."
