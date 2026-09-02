"""
Notices when Vince comes back to the desk.

Two tiers, cheapest first. The HID idle timer is free — the window server is
already counting — and it answers the only question that really matters: has a
human touched this machine in the last few seconds. A camera check can confirm
*who* returned, but it costs a lens activation, so it is opt-in rather than the
default path.

The monitor never imports the hub. It calls a callback instead, because the hub
imports this module and a direct import back would be circular.
"""

import asyncio
import time

import Quartz

DEFAULT_IDLE_THRESHOLD_S = 900.0     # 15 minutes away before a return counts
DEFAULT_COOLDOWN_S = 4 * 3600.0      # at most one briefing every four hours
DEFAULT_POLL_S = 5.0
RETURN_IDLE_S = 5.0                  # input this recent means someone is here now

# Tier 2 is off by default. The idle timer already proves a person is at the
# keyboard; opening the camera unannounced to confirm *which* person is a
# privacy decision, not a technical one, so it is left to the operator.
VERIFY_WITH_CAMERA = False
CAMERA_VERIFY_TIMEOUT_S = 4.0


def get_system_idle_time() -> float:
    """Seconds since the last keyboard, mouse or trackpad event."""
    try:
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState,
            Quartz.kCGAnyInputEventType))
    except Exception:
        return 0.0


async def _face_present(webcam_factory) -> bool:
    """Tier 2. True when a face is visible, and True on any failure.

    Failing open is deliberate: a camera already in use by a call, or a closed
    lid, must not silently suppress the briefing. Tier 1 has already established
    that somebody is typing.
    """
    import sentry_recognition
    loop = asyncio.get_running_loop()

    def probe():
        cam = webcam_factory()
        try:
            cam.start()
            frame = cam.read_frame()
            if not frame:
                return True
            return bool(sentry_recognition.extract_face_embeddings(frame))
        finally:
            try:
                cam.stop()
            except Exception:
                pass

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, probe),
                                      timeout=CAMERA_VERIFY_TIMEOUT_S)
    except Exception:
        return True


class PresenceMonitor:
    """Fires on_return() the first time input resumes after a long silence."""

    def __init__(self, on_return, idle_threshold_s: float = DEFAULT_IDLE_THRESHOLD_S,
                 cooldown_s: float = DEFAULT_COOLDOWN_S, poll_s: float = DEFAULT_POLL_S,
                 verify_with_camera: bool = VERIFY_WITH_CAMERA, webcam_factory=None,
                 log=print):
        self.on_return = on_return
        self.idle_threshold_s = idle_threshold_s
        self.cooldown_s = cooldown_s
        self.poll_s = poll_s
        self.verify_with_camera = verify_with_camera
        self.webcam_factory = webcam_factory
        self.log = log
        self.is_away = False
        self.last_briefing_at = 0.0
        self.returns_seen = 0

    def _cooldown_remaining(self, now: float) -> float:
        if not self.last_briefing_at:
            return 0.0
        return max(0.0, self.cooldown_s - (now - self.last_briefing_at))

    async def tick(self, now: float = None) -> str:
        """One poll. Returns what it decided, which is what the tests assert on."""
        now = now if now is not None else time.time()
        idle = get_system_idle_time()

        if idle > self.idle_threshold_s:
            if not self.is_away:
                self.is_away = True
                self.log(f"Presence: away (idle {idle / 60:.0f}m).")
            return "away"

        if not self.is_away:
            return "active"

        # Still counted as away, but input has only just resumed.
        if idle > RETURN_IDLE_S:
            return "away"

        self.is_away = False
        self.returns_seen += 1

        remaining = self._cooldown_remaining(now)
        if remaining > 0:
            self.log(f"Presence: welcome back — briefing suppressed for "
                     f"another {remaining / 60:.0f}m.")
            return "returned-cooldown"

        if self.verify_with_camera and self.webcam_factory is not None:
            if not await _face_present(self.webcam_factory):
                self.log("Presence: input resumed but no face seen; holding the briefing.")
                return "returned-unverified"

        self.last_briefing_at = now
        self.log("Presence: welcome back — building the briefing.")
        try:
            await self.on_return()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(f"Presence: briefing trigger failed: {e}")
        return "briefing"

    async def run(self, shutdown_event: asyncio.Event):
        # A cold start is not a return. Without this the very first poll after
        # launch fires a briefing at anyone who opens the app.
        self.is_away = get_system_idle_time() > self.idle_threshold_s
        while not shutdown_event.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"Presence loop error: {e}")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=self.poll_s)
            except asyncio.TimeoutError:
                pass
