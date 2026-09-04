"""
sentry_personal.py - Calendar and email access for Project FRIDAY.

Calendar: native EventKit (pyobjc) — fast, works with every account added to
macOS (iCloud, Google, Exchange) once Calendar permission is granted.

Email: Apple Mail via AppleScript — works with any account configured in
Mail.app. If the user only uses webmail, they must add the account in
System Settings -> Internet Accounts first.
"""

import subprocess
import threading
import datetime

import EventKit
import Foundation

_store = None
_access_granted = None


def _get_store():
    """Returns an authorized EKEventStore, requesting Calendar access on first use."""
    global _store, _access_granted
    if _store is None:
        _store = EventKit.EKEventStore.alloc().init()
    if _access_granted:
        return _store

    status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
    # 3 = authorized (legacy), 4 = fullAccess (macOS 14+)
    if status in (3, 4):
        _access_granted = True
        return _store

    done = threading.Event()
    result = {"granted": False}

    def handler(granted, error):
        result["granted"] = bool(granted)
        done.set()

    if hasattr(_store, "requestFullAccessToEventsWithCompletion_"):
        _store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        _store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, handler)
    done.wait(timeout=60)
    _access_granted = result["granted"]
    if not _access_granted:
        raise PermissionError(
            "Calendar access denied. Grant it in System Settings -> Privacy & Security -> Calendars "
            "for the terminal/app running FRIDAY, then retry."
        )
    return _store


def _nsdate(dt: datetime.datetime):
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def get_calendar_events(days_ahead: int = 7, days_back: int = 0) -> str:
    """Lists calendar events from days_back days ago to days_ahead days ahead."""
    try:
        store = _get_store()
        now = datetime.datetime.now()
        start = now - datetime.timedelta(days=max(0, int(days_back)))
        end = now + datetime.timedelta(days=max(1, int(days_ahead)))
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(
            _nsdate(start), _nsdate(end), None
        )
        events = store.eventsMatchingPredicate_(pred) or []
        if not events:
            return f"No calendar events between {start.date()} and {end.date()}."

        items = []
        for ev in events:
            try:
                s = datetime.datetime.fromtimestamp(ev.startDate().timeIntervalSince1970())
                e = datetime.datetime.fromtimestamp(ev.endDate().timeIntervalSince1970())
                all_day = bool(ev.isAllDay())
                when = s.strftime("%a %Y-%m-%d") if all_day else f"{s.strftime('%a %Y-%m-%d %H:%M')}–{e.strftime('%H:%M')}"
                cal = ev.calendar().title() if ev.calendar() else "?"
                loc = f" @ {ev.location()}" if ev.location() else ""
                items.append((s, f"{when} | {ev.title()}{loc} [{cal}]"))
            except Exception:
                continue
        items.sort(key=lambda t: t[0])
        lines = [line for _, line in items[:60]]
        return f"Calendar events ({start.date()} to {end.date()}):\n" + "\n".join(lines)
    except PermissionError as e:
        return f"[Error]: {e}"
    except Exception as e:
        return f"[Error]: Calendar read failed: {e}"


def create_calendar_event(title: str, start_iso: str, duration_minutes: int = 60, notes: str = "") -> str:
    """Creates an event in the default calendar. start_iso: 'YYYY-MM-DD HH:MM'."""
    try:
        store = _get_store()
        try:
            start = datetime.datetime.fromisoformat(start_iso)
        except ValueError:
            return f"[Error]: Could not parse start time '{start_iso}'. Use 'YYYY-MM-DD HH:MM'."
        end = start + datetime.timedelta(minutes=max(5, int(duration_minutes)))

        ev = EventKit.EKEvent.eventWithEventStore_(store)
        ev.setTitle_(title)
        ev.setStartDate_(_nsdate(start))
        ev.setEndDate_(_nsdate(end))
        if notes:
            ev.setNotes_(notes)
        ev.setCalendar_(store.defaultCalendarForNewEvents())

        ok, err = store.saveEvent_span_error_(ev, EventKit.EKSpanThisEvent, None)
        if not ok:
            return f"[Error]: Could not save event: {err}"
        return f"Event created: '{title}' on {start.strftime('%a %Y-%m-%d %H:%M')} ({duration_minutes} min) in calendar '{ev.calendar().title()}'."
    except PermissionError as e:
        return f"[Error]: {e}"
    except Exception as e:
        return f"[Error]: Event creation failed: {e}"


# ---------------------------------------------------------------------------
# Email via Apple Mail
# ---------------------------------------------------------------------------

def _run_applescript(script: str, timeout: int = 45) -> str:
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        err = result.stderr.strip()
        if "not authorized" in err.lower() or "-1743" in err:
            raise PermissionError(
                "Automation permission for Mail denied. Grant it in System Settings -> "
                "Privacy & Security -> Automation (allow the terminal/app running FRIDAY to control Mail)."
            )
        raise RuntimeError(err or "AppleScript failed")
    return result.stdout.strip()


_MAIL_LIST_SCRIPT = '''
tell application "Mail"
    set out to ""
    set msgCount to count of messages of inbox
    if msgCount is 0 then return "(inbox empty)"
    set n to {count}
    if n > msgCount then set n to msgCount
    repeat with i from 1 to n
        set m to message i of inbox
        set flagTxt to ""
        if read status of m is false then set flagTxt to "[UNREAD] "
        set line_i to flagTxt & (date received of m as string) & " | From: " & (sender of m) & " | Subject: " & (subject of m)
        try
            set body_text to content of m
            if length of body_text > 220 then set body_text to text 1 thru 220 of body_text
            set line_i to line_i & linefeed & "    Preview: " & body_text
        end try
        set out to out & line_i & linefeed & linefeed
    end repeat
    return out
end tell
'''

_MAIL_SEARCH_SCRIPT = '''
tell application "Mail"
    set out to ""
    set found to 0
    set msgCount to count of messages of inbox
    set limitN to {scan_limit}
    if limitN > msgCount then set limitN to msgCount
    repeat with i from 1 to limitN
        set m to message i of inbox
        set subj to subject of m
        set sndr to sender of m
        if (subj contains "{query}") or (sndr contains "{query}") then
            set found to found + 1
            set out to out & (date received of m as string) & " | From: " & sndr & " | Subject: " & subj & linefeed
            if found is greater than or equal to {count} then exit repeat
        end if
    end repeat
    if found is 0 then return "(no matches in the most recent " & limitN & " inbox messages)"
    return out
end tell
'''


def get_recent_emails(count: int = 10) -> str:
    """Returns the most recent inbox emails with sender, subject, and a short preview."""
    try:
        count = max(1, min(25, int(count)))
        out = _run_applescript(_MAIL_LIST_SCRIPT.replace("{count}", str(count)), timeout=90)
        return f"Most recent {count} inbox emails:\n{out}" if out else "(no output from Mail)"
    except PermissionError as e:
        return f"[Error]: {e}"
    except subprocess.TimeoutExpired:
        return "[Error]: Mail took too long. Is Mail.app set up with an account?"
    except Exception as e:
        return f"[Error]: Email read failed: {e}. If you don't use Apple Mail, add your account in System Settings -> Internet Accounts."


def search_emails(query: str, count: int = 8) -> str:
    """Searches recent inbox messages by sender/subject substring."""
    try:
        if not query:
            return "[Error]: Empty search query."
        safe_q = query.replace('"', '\\"')
        script = (_MAIL_SEARCH_SCRIPT
                  .replace("{query}", safe_q)
                  .replace("{count}", str(max(1, min(20, int(count)))))
                  .replace("{scan_limit}", "300"))
        out = _run_applescript(script, timeout=120)
        return f"Email search for '{query}':\n{out}"
    except PermissionError as e:
        return f"[Error]: {e}"
    except subprocess.TimeoutExpired:
        return "[Error]: Mail search took too long."
    except Exception as e:
        return f"[Error]: Email search failed: {e}"


if __name__ == "__main__":
    print(get_calendar_events(7))
    print(get_recent_emails(3))
