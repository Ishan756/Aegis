"""A minimal HTTP service for exercising the Docker deployment workflow.

Deliberately dependency-free and built on the standard library, so the image
builds in a few seconds and the integration test does not depend on a package
index being reachable.

It exists to be observable: it answers ``/healthz`` for the HEALTHCHECK, and it
logs one line per request to stdout, unbuffered, so ``container_logs`` has
something real to return.
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
STARTED_AT = time.time()


class Handler(BaseHTTPRequestHandler):
    """Two routes: a liveness probe and a greeting."""

    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path == "/healthz":
            self._respond(200, {"status": "ok", "uptime_seconds": round(time.time() - STARTED_AT, 1)})
        elif self.path == "/":
            self._respond(200, {"service": "aegis-sample", "message": "hello from the container"})
        else:
            self._respond(404, {"error": "not found", "path": self.path})

    def log_message(self, fmt: str, *args: object) -> None:
        # Explicit stdout, and the server is started with -u, so the log is not
        # held in a buffer that would only appear when the container stopped.
        print(f"[sample-app] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    print(f"[sample-app] listening on 0.0.0.0:{PORT}", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[sample-app] shutting down", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()