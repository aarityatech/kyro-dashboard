#!/usr/bin/env python3
"""Minimal static file server for the Kyro dashboard.

Uses an explicit chdir to an absolute path *before* importing http.server, so it
works even when the launching process has an inaccessible current directory
(which breaks `python -m http.server`, which calls os.getcwd() at startup).

Usage: python3 serve.py [port]
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8004
print(f"Kyro Analytics dashboard -> http://0.0.0.0:{port}")
ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()
