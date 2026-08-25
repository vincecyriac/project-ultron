"""
Desktop shell for Project Ultron.

Owns the process lifecycle: boots the engine on a background loop, shows the
PyWebView window, and makes sure that however the app ends — window closed,
Ctrl+C, SIGTERM, or the assistant deciding to quit — the engine gets a graceful
shutdown and the audio devices, sockets and servers are released.
"""

import asyncio
import os
import signal
import socket
import sys
import threading
import time

import ultron_hub

GUI_HOST = "127.0.0.1"
GUI_PORT = 8766
# A fixed http:// origin, never file://. Browsers key camera/mic grants to the
# origin, and a file:// or shifting port re-prompts on every launch.
GUI_URL = f"http://{GUI_HOST}:{GUI_PORT}/"

# WKWebView keeps permission grants in its website data store. pywebview's
# default private_mode wipes that store on every start, which is what made the
# camera and microphone prompt come back each launch.
WEBVIEW_STORAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".webview")

BOOT_TIMEOUT = 45.0     # engine has this long to serve the GUI
STOP_TIMEOUT = 10.0     # then this long to shut down before we stop waiting

backend_error = None    # set by the engine thread if boot fails


def start_backend():
    """Run the engine on its own event loop, in this thread."""
    global backend_error
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ultron_hub.run_ultron())
    except ultron_hub.StartupError as e:
        backend_error = str(e)
    except Exception as e:
        backend_error = f"{type(e).__name__}: {e}"
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


def wait_for_gui(backend, timeout=BOOT_TIMEOUT):
    """Block until the engine's HTTP server actually accepts connections.

    Replaces a fixed sleep, which either raced the server on a slow boot or
    wasted seconds on a fast one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend_error or not backend.is_alive():
            return False
        with socket.socket() as probe:
            probe.settimeout(0.25)
            if probe.connect_ex((GUI_HOST, GUI_PORT)) == 0:
                return True
        time.sleep(0.15)
    return False


def stop_backend(backend, reason):
    """Ask the engine to stop and give it a bounded amount of time to do so."""
    ultron_hub.request_shutdown(reason)
    backend.join(timeout=STOP_TIMEOUT)
    if backend.is_alive():
        print("[Ultron Desktop] Engine did not stop in time; exiting anyway.")


def grant_media_capture():
    """Auto-approve the WKWebView camera/mic prompt.

    pywebview installs a UI delegate but never implements the media-capture
    decision method, so WKWebView falls back to asking. Vince already granted
    the app itself at the OS level; asking again inside the embedded page every
    launch is pure friction. No-op off macOS or if the private API shifts.
    """
    try:
        import objc
        from webview.platforms import cocoa
    except Exception:
        return False

    delegate = cocoa.BrowserView.BrowserDelegate
    selector = b"webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:"
    try:
        if delegate.instancesRespondToSelector_(selector):
            return True

        def _decide(self, webView, origin, frame, capture_type, decisionHandler):
            decisionHandler(1)          # WKPermissionDecisionGrant

        objc.classAddMethods(delegate, [
            objc.selector(_decide, selector=selector, signature=b"v@:@@@q@?")
        ])
        return True
    except Exception as e:
        print(f"[Ultron Desktop] Could not auto-grant media capture: {e}")
        return False


def run_windowed(backend):
    """PyWebView window. Must own the main thread on macOS."""
    import webview

    granted = grant_media_capture()
    os.makedirs(WEBVIEW_STORAGE, exist_ok=True)

    window = webview.create_window(
        title="Project Ultron - AI Desktop Assistant",
        url=GUI_URL,
        width=1280,
        height=850,
        resizable=True,
        min_size=(900, 600),
        background_color="#070A0F",
    )

    # User closed the window -> stop the engine.
    window.events.closed += lambda: ultron_hub.request_shutdown("window closed")

    # Engine decided to quit (e.g. "goodbye" -> shutdown_ultron) -> close the
    # window so webview.start() returns and the process can exit on its own.
    def close_window(reason):
        try:
            window.destroy()
        except Exception:
            pass

    ultron_hub.on_shutdown(close_window)

    # private_mode=False keeps the data store (and its permission grants) on
    # disk between launches; storage_path pins where that lives.
    webview.start(debug=False, private_mode=False, storage_path=WEBVIEW_STORAGE)
    if not granted:
        print("[Ultron Desktop] Media capture prompts may still appear.")
    # start() returns once the window is gone, for whichever of the two reasons.
    stop_backend(backend, "window closed")


def run_headless(backend):
    """No PyWebView available: serve the same GUI in the default browser."""
    import webbrowser

    print(f"[Ultron Desktop] Opening Web GUI in default browser: {GUI_URL}")
    webbrowser.open(GUI_URL)
    try:
        while backend.is_alive():
            backend.join(0.5)
    except KeyboardInterrupt:
        pass
    stop_backend(backend, "interrupted")


def main():
    backend = threading.Thread(target=start_backend, name="ultron-engine", daemon=True)
    backend.start()

    # Ctrl+C / kill reach the engine instead of killing the thread mid-write.
    def handle_signal(signum, _frame):
        ultron_hub.request_shutdown(signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass    # not on the main thread, or unsupported platform

    if not wait_for_gui(backend):
        reason = backend_error or f"GUI server did not come up within {BOOT_TIMEOUT:.0f}s"
        print(f"[Ultron Desktop] Engine failed to start: {reason}")
        stop_backend(backend, "startup failure")
        return 1

    try:
        import webview  # noqa: F401
    except ImportError:
        run_headless(backend)
        return 0

    print(f"[Ultron Desktop] Launching PyWebView Desktop GUI: {GUI_URL}")
    try:
        run_windowed(backend)
    except Exception as e:
        # A real window failure — report it rather than silently degrading.
        print(f"[Ultron Desktop] Window error: {e}")
        stop_backend(backend, "window error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
