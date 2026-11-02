#!/usr/bin/env python3
"""Drive local synthetic hooks and native delivery; never claims AWS/backend proof."""

import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from delivery_checks import evaluate_cycles
from delivery_checks import evaluate_phase
from delivery_checks import evaluate_termination
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

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def capture(route, filename, timeout=15):
        value = post(route, timeout)
        (output.parent / filename).write_text(json.dumps(value, indent=2) + "\n")
        return value

    elapsed = None
    result = {}
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
        if not result["passed"]:
            raise RuntimeError("Initial refresh verification failed")
        local_checks = {}
        for name in ("automatic", "recovery"):
            phase = capture("/delivery/" + name, f"delivery-{name}.json", timeout=90)
            local_checks.update({
                f"{name}_{key}": value
                for key, value in evaluate_phase(phase, result["runtime_id"], require_forward=False).items()
            })
        vm = {"microvmId": "local-synthetic-hooks", "execution": "local-synthetic-hooks-capture-only", "cycles": []}
        samples = []
        for cycle in range(1, 4):
            transition = {"cycle": cycle}
            for hook, wanted in (("suspend", "SUSPENDED"), ("resume", "RUNNING")):
                post("/aws/lambda-microvms/runtime/v1/" + hook)
                transition["suspended" if hook == "suspend" else "resumed"] = {
                    "state": wanted, "time": time.time(), "microvmId": vm["microvmId"]
                }
            vm["cycles"].append(transition)
            (output.parent / "vm-local.json").write_text(json.dumps(vm, indent=2) + "\n")
            sample = capture("/verify/features", f"verification-resumed-{cycle}.json")
            samples.append(sample)
            phase = capture("/delivery/automatic", f"automatic-resumed-{cycle}.json")
            local_checks.update({
                f"resumed_{cycle}_{key}": value
                for key, value in evaluate_phase(phase, result["runtime_id"], require_forward=False).items()
            })
        local_checks.update(evaluate_cycles(vm, samples, result["runtime_id"]))
        armed = capture("/delivery/termination", "termination-armed.json")
        final = capture("/aws/lambda-microvms/runtime/v1/terminate", "termination.json")
        local_checks.update(evaluate_termination(armed, final, result["runtime_id"], require_forward=False))
        result["local_delivery"] = {
            "execution": "local-synthetic-hooks-capture-only",
            "checks": local_checks,
            "passed": bool(local_checks) and all(local_checks.values()),
        }
        result["passed"] = result["passed"] and result["local_delivery"]["passed"]
    except (OSError, ValueError, RuntimeError) as error:
        result.update(passed=False, error=str(error))
        result.setdefault("checks", {})["scenario_completed"] = False
    finally:
        try:
            post("/release", timeout=1)
        except OSError:
            pass
    output.write_text(json.dumps(result, indent=2) + "\n")
    for name, passed in result["checks"].items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    if "error" in result:
        print(result["error"])
    for name, passed in result.get("local_delivery", {}).get("checks", {}).items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    if elapsed is not None:
        print(f"/run: {elapsed:.3f}s")
    print(f"Evidence: {output}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
