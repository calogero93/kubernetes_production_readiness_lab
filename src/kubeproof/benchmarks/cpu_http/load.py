"""Bounded, fixed-rate request generator for the CPU fixture."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from kubeproof.benchmarks.cpu_http.app import DEFAULT_ITERATIONS, work_digest


@dataclass(frozen=True)
class RequestResult:
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class LoadSummary:
    schema_version: str
    started_at: str
    target_url: str
    work_iterations: int
    target_rps: float
    planned_requests: int
    sent_requests: int
    dropped_by_client: int
    succeeded: int
    failed: int
    failure_kinds: dict[str, int]
    elapsed_seconds: float
    max_schedule_lag_ms: float
    success_rps: float
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    latency_ms_max: float | None


class LoadCancelled(RuntimeError):
    """A runtime safety monitor stopped further scheduled requests."""


def validate_target(url: str) -> tuple[str, int]:
    """Refuse to direct this load generator at a non-loopback service."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/work"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("target must be an explicit loopback http://HOST:PORT/work URL")
    return parsed.hostname, parsed.port


def _request(
    host: str, port: int, timeout_seconds: float, iterations: int, expected_digest: str
) -> RequestResult:
    started = time.perf_counter()
    connection = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
    try:
        connection.request("GET", "/work", headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(4097)
        if response.status != 200:
            return RequestResult((time.perf_counter() - started) * 1000, "http_error")
        if len(body) > 4096:
            raise ValueError("unexpected response size")
        payload = json.loads(body)
        if not isinstance(payload, dict) or (
            payload.get("schema_version") != "1"
            or payload.get("iterations") != iterations
            or payload.get("digest") != expected_digest
        ):
            raise ValueError("response does not match the configured CPU work")
    except (http.client.HTTPException, TimeoutError, OSError):
        return RequestResult((time.perf_counter() - started) * 1000, "network_error")
    except (ValueError, UnicodeError):
        return RequestResult((time.perf_counter() - started) * 1000, "invalid_response")
    finally:
        connection.close()
    return RequestResult((time.perf_counter() - started) * 1000)


def _percentile(sorted_values: list[float], percentage: int) -> float | None:
    if not sorted_values:
        return None
    return round(sorted_values[math.ceil(len(sorted_values) * percentage / 100) - 1], 3)


def run_load(
    target_url: str,
    *,
    target_rps: float,
    requests: int,
    work_iterations: int = DEFAULT_ITERATIONS,
    max_in_flight: int = 16,
    timeout_seconds: float = 2.0,
    cancel_if: Callable[[], bool] | None = None,
) -> LoadSummary:
    """Schedule fixed-rate arrivals, recording client saturation as dropped requests."""
    host, port = validate_target(target_url)
    if not 0 < target_rps <= 200:
        raise ValueError("target_rps must be greater than 0 and at most 200")
    if not 1 <= requests <= 10_000:
        raise ValueError("requests must be between 1 and 10000")
    if not 1 <= max_in_flight <= 64:
        raise ValueError("max_in_flight must be between 1 and 64")
    if not 0 < timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be greater than 0 and at most 30")
    expected_digest = work_digest(work_iterations)
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    in_flight: set[Future[RequestResult]] = set()
    results: list[RequestResult] = []
    sent = 0
    max_schedule_lag_ms = 0.0

    with ThreadPoolExecutor(max_workers=max_in_flight) as pool:
        for index in range(requests):
            if cancel_if is not None and cancel_if():
                raise LoadCancelled("CPU load was cancelled by the runtime safety monitor")
            scheduled_at = started + index / target_rps
            wait = scheduled_at - time.perf_counter()
            while wait > 0:
                time.sleep(min(wait, 0.25))
                if cancel_if is not None and cancel_if():
                    raise LoadCancelled("CPU load was cancelled by the runtime safety monitor")
                wait = scheduled_at - time.perf_counter()
            max_schedule_lag_ms = max(
                max_schedule_lag_ms, (time.perf_counter() - scheduled_at) * 1000
            )
            completed = {future for future in in_flight if future.done()}
            for future in completed:
                results.append(future.result())
            in_flight.difference_update(completed)
            if len(in_flight) >= max_in_flight:
                continue
            in_flight.add(
                pool.submit(_request, host, port, timeout_seconds, work_iterations, expected_digest)
            )
            sent += 1
        for future in as_completed(in_flight):
            results.append(future.result())

    elapsed = time.perf_counter() - started
    successful = sorted(result.latency_ms for result in results if result.error is None)
    failures = Counter(result.error for result in results if result.error is not None)
    return LoadSummary(
        schema_version="1",
        started_at=started_at,
        target_url=target_url,
        work_iterations=work_iterations,
        target_rps=target_rps,
        planned_requests=requests,
        sent_requests=sent,
        dropped_by_client=requests - sent,
        succeeded=len(successful),
        failed=sum(failures.values()),
        failure_kinds=dict(sorted(failures.items())),
        elapsed_seconds=round(elapsed, 3),
        max_schedule_lag_ms=round(max_schedule_lag_ms, 3),
        success_rps=round(len(successful) / max(elapsed, requests / target_rps), 3),
        latency_ms_p50=_percentile(successful, 50),
        latency_ms_p95=_percentile(successful, 95),
        latency_ms_max=round(successful[-1], 3) if successful else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Send bounded fixed-rate CPU-fixture requests")
    parser.add_argument("--url", default="http://127.0.0.1:8080/work")
    parser.add_argument("--rate", type=float, required=True, help="Offered requests per second")
    parser.add_argument("--requests", type=int, required=True)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--max-in-flight", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    try:
        summary = run_load(
            args.url,
            target_rps=args.rate,
            requests=args.requests,
            work_iterations=args.iterations,
            max_in_flight=args.max_in_flight,
            timeout_seconds=args.timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(summary), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
