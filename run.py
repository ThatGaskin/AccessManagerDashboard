#!/usr/bin/env python3
"""Start the Access Manager Toolkit: python run.py  (then open http://127.0.0.1:8765)"""
import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # True when running as the packaged Windows .exe


def default_data_dir() -> Path:
    if FROZEN:
        # The .exe unpacks itself to a temporary folder, so data must live somewhere permanent.
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
        return Path(base) / "AccessManagerToolkit"
    return Path(__file__).parent / "data"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("AMT_PORT", 8765)))
    parser.add_argument("--data-dir", default=os.environ.get("AMT_DATA_DIR", str(default_data_dir())),
                        help=f"Folder for the SQLite database and sandbox state (default: {default_data_dir()})")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Never log request bodies or headers from the HTTP library; they can contain tokens.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    url = f"http://127.0.0.1:{args.port}/"
    if port_in_use(args.port):
        # Most likely the toolkit is already running (e.g. the .exe was double-clicked twice).
        print(f"Port {args.port} is already in use — the toolkit is probably already running at {url}")
        print("Use --port to start a separate instance.")
        if not args.no_browser:
            webbrowser.open(url)
        return 1

    from amtoolkit.app import create_app

    app = create_app(args.data_dir)
    toolkit = app.extensions["toolkit"]
    print(f"Access Manager Toolkit running at {url}  (data: {Path(args.data_dir).resolve()})")
    print("Keep this window open while you work. Press Ctrl+C to stop; running jobs are paused safely "
          "and can be resumed after restart.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        try:
            from waitress import serve
        except ImportError:
            app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
        else:
            serve(app, host="127.0.0.1", port=args.port, threads=8, ident=None)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nPausing running jobs…")
        toolkit.engine.shutdown()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        if not FROZEN:
            raise
        # A double-clicked console window closes instantly on error; keep it open so the message can be read.
        logging.exception("The toolkit could not start")
        code = 1
    if FROZEN and code:
        input("Press Enter to close this window…")
    sys.exit(code)
