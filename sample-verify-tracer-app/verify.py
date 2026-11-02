#!/usr/bin/env python3
"""Drive one identity refresh; exit nonzero for failed checks or hook timeouts."""

import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from lifecycle_checks import SCHEMA
from lifecycle_checks import required_telemetry_checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--output", default="results/latest.json")
    args = parser.parse_args()
    if urllib.parse.urlparse(args.url).scheme not in {"http", "https"}:
        parser.error("--url must use HTTP or HTTPS")

    def post(path, timeout=15):
        request = urllib.request.Request(
            args.url.rstrip("/") + path, data=b"{}", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.load(response)

    elapsed = None
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(  # nosec B310
                    args.url.rstrip("/") + "/health", timeout=1
                ):
                    break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Application did not become healthy within 30s") from None
                time.sleep(0.2)
        post("/prepare")
        start = time.monotonic()
        post("/aws/lambda-microvms/runtime/v1/run", timeout=2)
        elapsed = time.monotonic() - start
        result = post("/verify")
        result["checks"]["required_telemetry_scenarios_present"] = result.get(
            "verification_schema"
        ) == SCHEMA and required_telemetry_checks(SCHEMA).issubset(result["checks"])
        result["run_seconds"] = elapsed
        result["checks"]["run_under_one_second"] = elapsed < 1
        result["passed"] = all(result["checks"].values())
    except (OSError, ValueError) as error:
        result = {"passed": False, "checks": {"scenario_completed": False}, "error": str(error)}
    finally:
        try:
            post("/release", timeout=1)
        except OSError:
            pass
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    for name, passed in result["checks"].items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    if "error" in result:
        print(result["error"])
    if elapsed is not None:
        print(f"/run: {elapsed:.3f}s")
    print(f"Evidence: {output}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
