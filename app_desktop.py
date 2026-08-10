import os
import sys
import time
import threading
import asyncio

import ultron_hub

backend_loop = None

def start_backend():
    """Runs the Ultron engine backend in a background asyncio thread."""
    global backend_loop
    backend_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(backend_loop)
    try:
        backend_loop.run_until_complete(ultron_hub.run_ultron())
    except Exception as e:
        print(f"[Backend Error]: {e}")

def main():
    # 1. Start Python backend in background thread
    backend_thread = threading.Thread(target=start_backend, daemon=True)
    backend_thread.start()

    # Allow WebSocket server time to bind port
    time.sleep(5.0)

    # 2. GUI is served by the backend over HTTP (needed for ES module imports)
    gui_url = "http://127.0.0.1:8766/"

    def request_backend_shutdown():
        # Closing the window abandons the daemon backend thread mid-flight
        # (pending executor futures, an open live session) which crashes the
        # interpreter on the way out. Signal a clean stop and give it a beat
        # to unwind before the process actually exits.
        if backend_loop and backend_loop.is_running():
            backend_loop.call_soon_threadsafe(ultron_hub.shutdown_event.set)
        backend_thread.join(timeout=3.0)

    try:
        import webview
        print(f"[Ultron Desktop] Launching PyWebView Desktop GUI: {gui_url}")
        window = webview.create_window(
            title="Project Ultron - AI Desktop Assistant",
            url=gui_url,
            width=1280,
            height=850,
            resizable=True,
            min_size=(900, 600),
            background_color="#101214"
        )
        window.events.closing += request_backend_shutdown
        webview.start(debug=False)
    except Exception as e:
        import webbrowser
        print(f"[Ultron Desktop] Opening Web GUI in default browser: {gui_url}")
        webbrowser.open(gui_url)
        try:
            backend_thread.join()
        except KeyboardInterrupt:
            print("\nProject Ultron terminated.")
            request_backend_shutdown()


if __name__ == "__main__":
    main()
