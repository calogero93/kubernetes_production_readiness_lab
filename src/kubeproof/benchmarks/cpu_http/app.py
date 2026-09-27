"""Small HTTP service with a fixed amount of CPU work per request."""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_ITERATIONS = 20_000
MAX_ITERATIONS = 2_000_000


def work_digest(iterations: int) -> str:
    """Return a reproducible result from bounded, CPU-bound work."""
    if not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be between 1 and {MAX_ITERATIONS}")
    return hashlib.pbkdf2_hmac(
        "sha256", b"kubeproof-cpu-fixture", b"stable-workload-v1", iterations
    ).hex()


def make_server(host: str, port: int, iterations: int) -> HTTPServer:
    """Create a single-worker server so one Pod has a clear CPU bottleneck."""
    work_digest(iterations)  # Validate before binding a port.

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._reply(200, b"ok\n", "text/plain; charset=utf-8")
            elif self.path == "/work":
                body = (
                    json.dumps(
                        {
                            "schema_version": "1",
                            "iterations": iterations,
                            "digest": work_digest(iterations),
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
                self._reply(200, body, "application/json")
            else:
                self._reply(404, b"not found\n", "text/plain; charset=utf-8")

        def _reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            # Per-request logging would itself perturb the benchmark.
            pass

    return HTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a bounded CPU-bound HTTP fixture")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 1 <= args.iterations <= MAX_ITERATIONS:
        parser.error(f"--iterations must be between 1 and {MAX_ITERATIONS}")
    with make_server(args.host, args.port, args.iterations) as server:
        print(f"CPU fixture listening on http://{args.host}:{args.port}", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == "__main__":
    main()
