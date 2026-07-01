#!/usr/bin/env python3
"""Serve analysis_output locally for lazy-loading report preview."""
from __future__ import annotations
import argparse
import http.server
import socketserver
from functools import partial
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve analysis_output locally for report preview.")
    parser.add_argument("--dir", default="analysis_output", help="Output folder to serve. Default: analysis_output")
    parser.add_argument("--port", type=int, default=8000, help="Local port. Default: 8000")
    args = parser.parse_args()
    root = Path(args.dir).resolve()
    if not root.exists():
        raise SystemExit(f"Output folder does not exist: {root}")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"Serving {root}")
        print(f"Open http://localhost:{args.port}/report.html")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
