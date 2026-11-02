# /// script
# dependencies = ["boto3>=1.42.57"]
# ///
"""Run serverless-init automatic delivery, recovery and three AWS resume cycles."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.request
import uuid

import check
import check_delivery


class Runner:
    def __init__(self, client, logs_client, image, version, role, events_log, output):
        self.client, self.logs_client = client, logs_client
        self.image, self.version, self.role = image, version, role
        self.events_log, self.output = Path(events_log), Path(output)
        self.endpoint, self.token = None, None
        self.evidence = {"schema": 1, "startedAt": time.time(), "deadlineSeconds": 180,
                         "vm": {}, "samples": [], "cycles": [], "recoveries": [],
                         "cloudwatch": []}

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.evidence, indent=2) + "\n")

    def note(self, message):
        vm = self.evidence["vm"].get("microvmId", "uncreated")
        line = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} run7 {vm} {message}"
        with self.events_log.open("a") as out:
            out.write(line + "\n")
        print(line, flush=True)
        self.save()

    def wait_state(self, wanted):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                response = self.client.get_microvm(microvmIdentifier=self.evidence["vm"]["microvmId"])
                state = response["state"]
            except self.client.exceptions.ResourceNotFoundException:
                state = "NOT_FOUND"
            if state == wanted or (wanted == "TERMINATED" and state == "NOT_FOUND"):
                self.note(f"reached {state}")
                return {"state": state, "at": time.time()}
            if state == "TERMINATED":
                raise RuntimeError(f"wanted {wanted}, got TERMINATED")
            time.sleep(1)
        raise TimeoutError(f"waiting for {wanted}; last state={state}")

    def app(self, path, data=None, timeout=10):
        request = urllib.request.Request(self.endpoint + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", "X-aws-proxy-auth": self.token,
                     "X-aws-proxy-port": "8080"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return json.loads(body) if path != "/" else response.status

    def health(self):
        at = time.time()
        started = time.monotonic()
        status = self.app("/", timeout=1)
        result = {"at": at, "status": status, "elapsedMs": (time.monotonic() - started) * 1000}
        if status != 200 or result["elapsedMs"] >= 1000:
            raise RuntimeError(f"application health deadline failed: {result}")
        return result

    def emit(self, scenario):
        return self.app("/delivery/marker", {"scenario": scenario, "marker": str(uuid.uuid4())})

    def backend(self, marker, require_metric=True):
        # Use a narrow unique marker, not timing alone, to attribute backend evidence.
        vm = self.evidence["vm"]["microvmId"]
        start, end = marker["wall"] - 30, time.time() + 30
        logs = check.dd("/api/v2/logs/events/search", body={
            "filter": {"query": f'service:{check.SERVICE} lambda_microvm_id:{vm} @marker:{marker["marker"]}',
                       "from": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                       "to": datetime.fromtimestamp(end, timezone.utc).isoformat()},
            "page": {"limit": 1000}})
        sample = {"marker": marker, "logs": logs, "observedAt": time.time()}
        if require_metric:
            tags = f'service:{check.SERVICE},marker:{marker["marker"]},run_id:{marker["runId"]}'
            sample["metrics"] = check.dd("/api/v1/query", {
                "from": int(start), "to": int(end), "query": f"max:mvmtest.delivery{{{tags}}}"})
            sample["observedAt"] = time.time()
        return sample

    def await_delivery(self, marker, require_metric=True):
        while True:
            sample = self.backend(marker, require_metric)
            if check_delivery.marker_delivered(sample, require_metric):
                if sample["observedAt"] - marker["wall"] > self.evidence["deadlineSeconds"]:
                    raise TimeoutError(f'{marker["scenario"]} arrived after its deadline')
                return sample
            if time.time() - marker["wall"] >= self.evidence["deadlineSeconds"]:
                self.evidence["lastUndelivered"] = sample
                raise TimeoutError(f'{marker["scenario"]} backend delivery deadline exceeded')
            time.sleep(5)

    def recovery(self, mode):
        recovery = {"mode": mode, "startedAt": time.time(), "health": [],
                    "freshScenario": "recovery-" + mode, "requests": []}
        self.evidence["recoveries"].append(recovery)
        try:
            self.app("/delivery/fault", {"mode": mode})
            recovery["offered"] = self.emit("recovery-" + mode)
            deadline = time.monotonic() + 60
            while True:
                state = self.app("/delivery/state")
                if state.get("overflow"):
                    raise RuntimeError("relay evidence retention overflow")
                recovery["requests"] = [request for request in state["requests"]
                    if request["receivedAt"] >= recovery["startedAt"] and request.get("completedAt") is not None]
                failed = {request["pipeline"] for request in recovery["requests"]
                          if request["outcome"] == "injected-" + mode}
                if failed == {"logs", "metrics"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{mode}: fault did not reach both Agent pipelines")
                time.sleep(0.5)
            recovery["health"] = [self.health() for _ in range(3)]
        finally:
            # Restore even if fault observation or health fails. Never leave intake broken.
            state = self.app("/delivery/fault", {"mode": "normal"})
            recovery["restoredAt"] = time.time()
            recovery["requests"] = [request for request in state["requests"]
                if recovery["startedAt"] <= request["receivedAt"] <= recovery["restoredAt"]
                and request.get("completedAt") is not None]
            self.save()
        fresh = self.emit(recovery["freshScenario"])
        sample = self.await_delivery(fresh)
        self.evidence["samples"].append(sample)
        self.note(f"{mode} restored; fresh log and metric observed in Datadog")

    def collect_final(self):
        arm = self.evidence["termination"]["arm"]
        deadline = time.monotonic() + 180
        while True:
            pages = self.logs_client.get_paginator("filter_log_events").paginate(
                logGroupName=f"/aws/lambda-microvms/{check.SERVICE}-delivery",
                startTime=int((self.evidence["startedAt"] - 30) * 1000),
                endTime=int((time.time() + 30) * 1000),
                filterPattern='{ $.msg = "mvmtest_delivery" }')
            parsed = []
            for page in pages:
                for event in page.get("events", []):
                    if not event["logStreamName"].endswith(self.evidence["vm"]["microvmId"]):
                        continue
                    try:
                        parsed.append({"at": event["timestamp"] / 1000, "row": json.loads(event["message"])})
                    except ValueError:
                        continue
            self.evidence["cloudwatch"] = parsed
            finals = [item["row"] for item in parsed if item["row"].get("marker") == arm["marker"]
                      and item["row"].get("scenario") == "termination"]
            self.save()
            if len(finals) == 1:
                sample = self.await_delivery(finals[0], require_metric=False)
                self.evidence["termination"].update(observedAt=sample["observedAt"], logs=sample["logs"])
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("unique final CloudWatch marker not found after termination")
            time.sleep(5)

    def run(self):
        try:
            response = self.client.run_microvm(imageIdentifier=self.image, imageVersion=self.version,
                executionRoleArn=self.role,
                idlePolicy={"autoResumeEnabled": False, "maxIdleDurationSeconds": 3600,
                            "suspendedDurationSeconds": 300})
            self.evidence["vm"] = {"microvmId": response["microvmId"], "imageArn": self.image,
                                   "imageVersion": self.version, "terminated": False}
            self.note("run-microvm called (image delivery)")
            self.wait_state("RUNNING")
            vm_id = self.evidence["vm"]["microvmId"]
            endpoint = self.client.get_microvm(microvmIdentifier=vm_id)["endpoint"]
            self.endpoint = endpoint if endpoint.startswith("https://") else "https://" + endpoint
            self.endpoint = self.endpoint.rstrip("/")
            self.token = self.client.create_microvm_auth_token(microvmIdentifier=vm_id,
                expirationInMinutes=60, allowedPorts=[{"port": 8080}])["authToken"]["X-aws-proxy-auth"]
            self.evidence["samples"].append(self.await_delivery(self.emit("initial")))
            self.note("automatic initial log and metric observed before any lifecycle flush")
            for mode in ("http503", "disconnect"):
                self.recovery(mode)
            for cycle in range(1, 4):
                transition = {"cycle": cycle}
                self.evidence["cycles"].append(transition)
                self.client.suspend_microvm(microvmIdentifier=vm_id)
                transition["suspended"] = self.wait_state("SUSPENDED")
                self.client.resume_microvm(microvmIdentifier=vm_id)
                transition["resumed"] = self.wait_state("RUNNING")
                health = self.health()
                sample = self.await_delivery(self.emit(f"resumed-{cycle}"))
                sample["health"] = [health]
                self.evidence["samples"].append(sample)
                self.note(f"cycle {cycle}: healthy; log and metric observed after resume")
            self.evidence["termination"] = {"arm": self.app("/delivery/termination", {"marker": str(uuid.uuid4())})}
            self.save()
            self.client.terminate_microvm(microvmIdentifier=vm_id)
            terminal = self.wait_state("TERMINATED")
            self.evidence["vm"].update(terminated=True, terminatedAt=terminal["at"], terminalState=terminal["state"])
            self.collect_final()
        except Exception as exc:
            self.evidence["error"] = f"{type(exc).__name__}: {exc}"
            self.note("ERROR: " + self.evidence["error"])
        finally:
            vm = self.evidence["vm"]
            if vm.get("microvmId") and not vm.get("terminated"):
                try:
                    self.client.terminate_microvm(microvmIdentifier=vm["microvmId"])
                    terminal = self.wait_state("TERMINATED")
                    vm.update(terminated=True, terminatedAt=terminal["at"], terminalState=terminal["state"])
                except Exception as exc:
                    self.evidence["error"] = self.evidence.get("error", "") + f" cleanup failed: {exc}"
            self.evidence["endedAt"] = time.time()
            self.save()
        result = check_delivery.evaluate(self.evidence)
        evaluation = self.output.with_name(self.output.stem + "-evaluation.json")
        evaluation.write_text(json.dumps(result, indent=2) + "\n")
        for name, passed in result["checks"].items():
            print(f"{'PASS' if passed else 'FAIL'} {name}")
        print(f"run7: {'PASS' if result['passed'] else 'FAIL'}")
        return 0 if result["passed"] else 1


def main():
    import boto3

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "version", "role", "region", "events-log"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path,
        default=Path(__file__).parent / "results" / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-delivery' / "delivery-run.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new evidence path")
    client = boto3.client("lambda-microvms", region_name=args.region)
    logs_client = boto3.client("logs", region_name=args.region)
    return Runner(client, logs_client, args.image, args.version, args.role, args.events_log, args.output).run()


if __name__ == "__main__":
    raise SystemExit(main())
