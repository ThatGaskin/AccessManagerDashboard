"""Command-line launcher, shared by `python run.py`, `python -m amtoolkit` and the single-file
`AccessManagerToolkit.pyz` release."""
import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__, logs

APP_NAME = "AccessManagerToolkit"


def default_data_dir() -> Path:
    """`./data` in a source checkout; a per-user folder when running from the .pyz file
    (whose contents are read-only)."""
    checkout = Path(__file__).resolve().parent.parent
    if checkout.is_dir() and (checkout / "run.py").is_file():
        return checkout / "data"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main(argv=None) -> int:
    if sys.version_info < (3, 11):
        print("Access Manager Toolkit needs Python 3.11 or newer.")
        return 1
    parser = argparse.ArgumentParser(prog="AccessManagerToolkit",
                                     description="Start the Access Manager Toolkit on http://127.0.0.1:8765")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AMT_PORT", 8765)))
    parser.add_argument("--data-dir", default=os.environ.get("AMT_DATA_DIR", str(default_data_dir())),
                        help=f"Folder for the SQLite database and sandbox state (default: {default_data_dir()})")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.add_argument("--log-level", choices=logs.LEVELS, default=os.environ.get("AMT_LOG_LEVEL", "INFO").upper(),
                        help="DEBUG also shows every call to the target system and the UI's polling (default: INFO)")
    parser.add_argument("--debug", action="store_true", help="Same as --log-level DEBUG")
    args = parser.parse_args(argv)

    log_file = logs.setup(args.data_dir, "DEBUG" if args.debug else args.log_level)
    logging.getLogger(__name__).info("Access Manager Toolkit %s starting (log file: %s)", __version__, log_file)

    url = f"http://127.0.0.1:{args.port}/"
    if port_in_use(args.port):
        # Most likely the toolkit is already running (e.g. started twice).
        print(f"Port {args.port} is already in use — the toolkit is probably already running at {url}")
        print("Use --port to start a separate instance.")
        if not args.no_browser:
            webbrowser.open(url)
        return 1

    from .app import create_app

    app = create_app(args.data_dir)
    toolkit = app.extensions["toolkit"]
    print(f"Access Manager Toolkit running at {url}  (data: {Path(args.data_dir).resolve()}, log: {log_file})")
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
