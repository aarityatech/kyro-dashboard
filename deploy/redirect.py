#!/usr/bin/env python3
"""Redirect every request to the NEW dashboard instance with HTTP 301.

Run this on the OLD instance (after taking its dashboard down) so anyone hitting
the old IP is bounced to the new one, preserving the path/query.

Usage:  python3 redirect.py <new_base_url> [listen_port]
  e.g.  python3 redirect.py http://10.0.4.55:8004 8004
"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NEW_BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://NEW_INSTANCE_IP:8004").rstrip("/")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8004


class Redirect(BaseHTTPRequestHandler):
    def _redir(self):
        self.send_response(302)  # temporary — old IP may be recycled later
        self.send_header("Location", NEW_BASE + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_HEAD = do_POST = _redir

    def log_message(self, *a):
        pass


print(f"Redirecting :{PORT} -> {NEW_BASE}")
ThreadingHTTPServer(("0.0.0.0", PORT), Redirect).serve_forever()
