#!/usr/bin/env python3
"""Minimal static file server with HTTP Range request and CORS support.

Required by the PMTiles JS client which fetches byte ranges from .pmtiles
archives.  Python's built-in ``http.server`` ignores Range headers.

Usage::

    python3 serve-html-maps.py                # serve on port 8000
    python3 serve-html-maps.py --port 9000    # custom port
    python3 serve-html-maps.py --dir /path    # custom root
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from http import HTTPStatus
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# Ensure .pmtiles gets a sensible content-type.
mimetypes.add_type("application/octet-stream", ".pmtiles")


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler extended with Range and CORS support."""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802 – HTTP method
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        range_header = self.headers.get("Range")
        if not range_header or not range_header.startswith("bytes="):
            # No Range header → fall back to the default full-file response.
            super().do_GET()
            return

        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        file_size = os.path.getsize(path)

        # Parse "bytes=START-END" (only single ranges).
        range_spec = range_header[len("bytes="):]
        parts = range_spec.split("-", 1)
        try:
            start = int(parts[0]) if parts[0] else None
            end = int(parts[1]) if parts[1] else None
        except ValueError:
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return

        if start is None and end is not None:
            # "bytes=-500" → last 500 bytes.
            start = max(0, file_size - end)
            end = file_size - 1
        elif end is None:
            end = file_size - 1

        if start is None or start >= file_size or end >= file_size or start > end:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.end_headers()
            return

        length = end - start + 1
        ctype = self.guess_type(path)

        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except BrokenPipeError:
                    break
                remaining -= len(chunk)


def main() -> None:
    # Resolve the default directory to AFL_CACHE_ROOT/osm so existing URLs
    # like /html/ continue to resolve.
    default_data = os.environ.get("AFL_DATA_ROOT", "/Volumes/afl_data")
    default_cache = os.environ.get(
        "AFL_CACHE_ROOT", os.path.join(default_data, "cache")
    )
    default_dir = os.path.join(default_cache, "osm")

    parser = argparse.ArgumentParser(description="Serve HTML maps with Range request support")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dir", default=default_dir, help="Root directory to serve")
    args = parser.parse_args()

    os.chdir(args.dir)
    server = HTTPServer(("0.0.0.0", args.port), RangeRequestHandler)
    print(f"Serving {args.dir} on http://localhost:{args.port}/")
    print(f"Map:    http://localhost:{args.port}/html/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
