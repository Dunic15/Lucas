"""Probe Laura's deployed HTTP latency without touching live meeting code.

Examples:
    python scripts/latency_probe.py https://dhfgfe6yw6.eu-central-1.awsapprunner.com
    python scripts/latency_probe.py https://dhfgfe6yw6.eu-central-1.awsapprunner.com --include-ask
    python scripts/latency_probe.py https://dhfgfe6yw6.eu-central-1.awsapprunner.com --meeting-url https://meet.google.com/xxx-yyyy-zzz

The default health check is safe and cheap. --include-ask calls /demo/ask, which
can use the configured production LLM provider. --meeting-url calls
/sessions/start and will send a real Recall bot into that meeting.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx


DEFAULT_QUESTION = "What approvals are needed before provisioning access?"


@dataclass
class ProbeResult:
    endpoint: str
    method: str
    status_code: int | None
    elapsed_ms: float
    ok: bool
    detail: str = ""


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> ProbeResult:
    start = time.perf_counter()
    try:
        resp = client.request(method, url, json=json_body)
        elapsed = (time.perf_counter() - start) * 1000
        detail = ""
        if not resp.is_success:
            detail = resp.text[:240].replace("\n", " ")
        return ProbeResult(
            endpoint=url,
            method=method,
            status_code=resp.status_code,
            elapsed_ms=round(elapsed, 1),
            ok=resp.is_success,
            detail=detail,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return ProbeResult(
            endpoint=url,
            method=method,
            status_code=None,
            elapsed_ms=round(elapsed, 1),
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
        )


def _summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return {
        "min_ms": round(min(ordered), 1),
        "median_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[p95_index], 1),
        "max_ms": round(max(ordered), 1),
    }


def _print_table(results: list[ProbeResult]) -> None:
    print("\nendpoint                 method  status  ok     elapsed_ms  detail")
    print("-----------------------  ------  ------  -----  ----------  ------")
    for r in results:
        name = "/" + "/".join(r.endpoint.rstrip("/").split("/")[3:])
        if name == "/":
            name = r.endpoint
        status = "-" if r.status_code is None else str(r.status_code)
        print(
            f"{name[:23]:23}  {r.method:6}  {status:6}  "
            f"{str(r.ok):5}  {r.elapsed_ms:10.1f}  {r.detail}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url",
        nargs="?",
        default=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
        help="Backend base URL. Defaults to PUBLIC_BASE_URL or local dev.",
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--avatar-id", default="laura")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument(
        "--include-ask",
        action="store_true",
        help="Also time /demo/ask. This may call the configured LLM provider.",
    )
    parser.add_argument(
        "--meeting-url",
        help="Also time /sessions/start. This sends a real Recall bot.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    results: list[ProbeResult] = []

    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        for _ in range(args.iterations):
            results.append(_request(client, "GET", f"{base}/health"))

        if args.include_ask:
            body = {"avatar_id": args.avatar_id, "question": args.question}
            for _ in range(args.iterations):
                results.append(_request(client, "POST", f"{base}/demo/ask", json_body=body))

        if args.meeting_url:
            body = {"avatar_id": args.avatar_id, "meeting_url": args.meeting_url}
            results.append(_request(client, "POST", f"{base}/sessions/start", json_body=body))

    grouped: dict[str, list[float]] = {}
    for r in results:
        key = f"{r.method} /" + "/".join(r.endpoint.rstrip("/").split("/")[3:])
        grouped.setdefault(key, []).append(r.elapsed_ms)

    payload = {
        "base_url": base,
        "results": [asdict(r) for r in results],
        "summary": {key: _summary(vals) for key, vals in grouped.items()},
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_table(results)
        print("\nsummary")
        print(json.dumps(payload["summary"], indent=2))

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
