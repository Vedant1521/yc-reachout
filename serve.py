#!/usr/bin/env python3
"""Run the site locally: python3 serve.py  ->  http://localhost:8765

Serves index.html and routes /api/yc to the same handler Vercel runs.
"""
import os, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import yc

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/api/yc"):
                return yc.handler.do_GET(self)
            if self.path.startswith("/api/"):
                return self.send_error(404)
            return super().do_GET()
        except Exception as e:
            print(f"Error handling {self.path}: {e}", file=sys.stderr, flush=True)

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    port = int(os.environ.get("PORT", 8765))
    print(f"http://localhost:{port}", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
