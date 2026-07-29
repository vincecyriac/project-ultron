import os
import sys
import time
import threading
import asyncio

import ultron_hub

def start_backend():
    """Runs the Ultron engine backend in a background asyncio thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ultron_hub.run_ultron())
    except Exception as e:
        print(f"[Backend Error]: {e}")

def main():
    # 1. Start Python backend in background thread
    backend_thread = threading.Thread(target=start_backend, daemon=True)
    backend_thread.start()
    
    # Allow WebSocket server time to bind port
    time.sleep(5.0)
    
    # 2. Locate web_gui index.html
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "web_gui", "index.html"))
    
    try:
        import webview
        print(f"[Ultron Desktop] Launching PyWebView Desktop GUI: file://{html_path}")
        window = webview.create_window(
            title="Project Ultron - AI Desktop Assistant",
            url=f"file://{html_path}",
            width=1280,
            height=850,
            resizable=True,
            min_size=(900, 600),
            background_color="#101214"
        )
        webview.start(debug=False)
    except Exception as e:
        import webbrowser
        print(f"[Ultron Desktop] Opening Web GUI in default browser: file://{html_path}")
        webbrowser.open(f"file://{html_path}")
        try:
            backend_thread.join()
        except KeyboardInterrupt:
            print("\nProject Ultron terminated.")


if __name__ == "__main__":
    main()
