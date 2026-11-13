#!/usr/bin/env python3
"""Tiny static server with CORS headers, used only to feed workflow JSON to the
n8n browser session during local development."""
import http.server
import os
import socketserver

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workflows")
PORT = 8765


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        super().end_headers()

    def log_message(self, *a):
        pass


class Server(socketserver.TCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        print(f"serving {ROOT} on http://127.0.0.1:{PORT}")
        httpd.serve_forever()
