#!/usr/bin/env python3
"""Start the Access Manager Toolkit: python run.py  (then open http://127.0.0.1:8765)"""
import argparse
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from amtoolkit.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("AMT_PORT", 8765)))
    parser.add_argument("--data-dir", default=os.environ.get("AMT_DATA_DIR", str(Path(__file__).parent / "data")),
                        help="Folder for the SQLite database and sandbox state (default: ./data)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Never log request bodies or headers from the HTTP library; they can contain tokens.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    app = create_app(args.data_dir)
    toolkit = app.extensions["toolkit"]
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Access Manager Toolkit running at {url}  (data: {Path(args.data_dir).resolve()})")
    print("Press Ctrl+C to stop. Running jobs are paused safely and can be resumed after restart.")
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
    sys.exit(main())
