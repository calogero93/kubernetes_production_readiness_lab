"""Bounded HTTP traffic from a Kubernetes Job to a Service DNS name."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import time
from collections import Counter


def probe(host: str, port: int, path: str, requests: int) -> dict[str, object]:
    latencies: list[float] = []
    outcomes: Counter[str] = Counter()
    for _ in range(requests):
        started = time.perf_counter()
        connection = http.client.HTTPConnection(host, port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read(4096)
            outcomes[str(response.status)] += 1
        except (OSError, http.client.HTTPException):
            outcomes["network_error"] += 1
        finally:
            latencies.append(round((time.perf_counter() - started) * 1000, 3))
            connection.close()
    ordered = sorted(latencies)
    return {
        "schema_version": "2",
        "target": f"http://{host}:{port}{path}",
        "requests": requests,
        "outcomes": dict(sorted(outcomes.items())),
        "latencies_ms": latencies,
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "all_responses_200": outcomes == {"200": requests},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("service")
    parser.add_argument("namespace")
    parser.add_argument("port", type=int)
    parser.add_argument("path")
    parser.add_argument("requests", type=int)
    args = parser.parse_args()
    if not 1 <= args.requests <= 50 or not 1 <= args.port <= 65535:
        parser.error("requests must be 1-50 and port must be 1-65535")
    host = f"{args.service}.{args.namespace}.svc"
    print(json.dumps(probe(host, args.port, args.path, args.requests), sort_keys=True))


if __name__ == "__main__":
    main()
