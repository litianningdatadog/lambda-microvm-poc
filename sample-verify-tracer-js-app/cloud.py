"""Real AWS snapshot verification. No local hook simulation; tokens stay in memory."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

APP = Path(__file__).resolve().parent
REGION = os.environ.get("REGION", "us-east-2")
NAME = os.environ.get("IMAGE_NAME", "sample-verify-tracer-js-app")
PROFILE = os.environ.get("AWS_VAULT_PROFILE", "sso-serverless-sandbox-account-admin")
BUILD_ROLE = os.environ.get("BUILD_ROLE_ARN", "arn:aws:iam::425362996713:role/microvm-build-role")
CANDIDATE_LIMIT = 6
FILES = (
    "Dockerfile", "app.js", "intake.js", "features.js", "feature-checks.js",
    "package.json", "package-lock.json", "serverless-init-linux-arm64",
)


def aws(service, operation, *args):
    response = subprocess.run(
        ["aws-vault", "exec", PROFILE, "--", "aws", service, operation,
         "--region", REGION, "--output", "json", *args],
        check=True, capture_output=True, text=True, env={**os.environ, "AWS_PAGER": ""},
    )
    return json.loads(response.stdout or "{}")


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def wait(operation, args, wanted, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = aws("lambda-microvms", operation, *args)
            status = value["state"]
        except subprocess.CalledProcessError as error:
            if "ResourceNotFoundException" not in (error.stderr or ""):
                raise
            value, status = {}, "NOT_VISIBLE_YET"
        print(f"{operation}: {status}", flush=True)
        if status == wanted:
            return value
        if status in {"FAILED", "TERMINATING", "TERMINATED"}:
            raise RuntimeError(f"Wanted {wanted}, got {status}: {value.get('stateReason', '')}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for {wanted}")
        time.sleep(5)


def deploy(results):
    key = os.environ["DD_API_KEY"]
    subprocess.run(["make", "prepare"], cwd=APP, check=True)
    manifest = json.loads((APP / "package.json").read_text())
    lock = json.loads((APP / "package-lock.json").read_text())
    if (manifest.get("dependencies", {}).get("dd-trace") != "6.15.0"
            or lock.get("packages", {}).get("node_modules/dd-trace", {}).get("version") != "6.15.0"):
        raise ValueError("Both package.json and package-lock.json must pin dd-trace 6.15.0")
    archive = results / "app.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name in FILES:
            package.write(APP / name, name)
    bucket = os.environ.get("S3_BUCKET", NAME)
    try:
        aws("s3api", "head-bucket", "--bucket", bucket)
    except subprocess.CalledProcessError:
        args = ["--bucket", bucket]
        if REGION != "us-east-1":
            args += ["--create-bucket-configuration", f"LocationConstraint={REGION}"]
        aws("s3api", "create-bucket", *args)
    object_key = f"{results.name}/app.zip"
    aws("s3api", "put-object", "--bucket", bucket, "--key", object_key, "--body", str(archive))
    hooks = {
        "port": 9000,
        "microvmImageHooks": {
            "ready": "ENABLED", "readyTimeoutInSeconds": 60,
            "validate": "ENABLED", "validateTimeoutInSeconds": 60,
        },
        "microvmHooks": {
            name: value
            for hook in ("run", "suspend", "resume", "terminate")
            for name, value in ((hook, "ENABLED"), (hook + "TimeoutInSeconds", 30))
        },
    }
    environment = {
        "DD_API_KEY": key,
        "DD_SITE": os.environ.get("DD_SITE", "datadoghq.com"),
        "DD_SERVICE": "sample-verify-tracer-js-app", "DD_ENV": "devmicrovm", "DD_VERSION": results.name,
        "DD_LOGS_ENABLED": "true", "DD_LOGS_INJECTION": "true", "DD_SOURCE": "nodejs",
        "DD_TRACE_ENABLED": "true", "DD_TRACE_SAMPLE_RATE": "1", "DD_TRACE_STARTUP_LOGS": "true",
        "DD_RUNTIME_METRICS_ENABLED": "true", "DD_RUNTIME_METRICS_RUNTIME_ID_ENABLED": "true",
        "DD_AWS_MICROVM_USER_APP_PORT": "8080", "VERIFY_FORWARD_TO_AGENT": "true",
        "DD_TRACE_AGENT_URL": "http://127.0.0.1:18126", "DD_DOGSTATSD_URL": "udp://127.0.0.1:18125",
        **{"DD_AWS_MICROVM_ENABLE_" + hook.upper(): "true"
           for hook in ("ready", "validate", "run", "suspend", "resume", "terminate")},
    }
    # NamedTemporaryFile is owner-only. Never persist this environment in evidence.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as secret:
        json.dump(environment, secret)
        secret.flush()
        args = [
            "--code-artifact", f"uri=s3://{bucket}/{object_key}",
            "--base-image-arn", os.environ.get("BASE_IMAGE_ARN", f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"),
            "--build-role-arn", BUILD_ROLE, "--hooks", json.dumps(hooks),
            "--environment-variables", "file://" + secret.name,
            "--cpu-configurations", '[{"architecture":"ARM_64"}]',
        ]
        existing = aws("lambda-microvms", "list-microvm-images", "--name-filter", NAME)
        image = next((row["imageArn"] for row in existing.get("items", []) if row["name"] == NAME), None)
        if image:
            response = aws("lambda-microvms", "update-microvm-image", "--image-identifier", image, *args)
        else:
            response = aws("lambda-microvms", "create-microvm-image", "--name", NAME, *args)
    deployment = {
        "imageArn": response["imageArn"], "imageVersion": str(response["imageVersion"]),
        "imageName": NAME, "region": REGION, "version": results.name, "schema": 1,
        "start": time.time(), "logGroup": f"/aws/lambda-microvms/{NAME}",
        "sha256": {name: hashlib.sha256((APP / name).read_bytes()).hexdigest() for name in FILES},
    }
    save(results / "deployment.json", deployment)
    wait("get-microvm-image-version", ["--image-identifier", deployment["imageArn"],
         "--image-version", deployment["imageVersion"]], "SUCCESSFUL", int(os.environ.get("BUILD_TIMEOUT", "1800")))
    print(f"Image ready: {deployment['imageArn']}:{deployment['imageVersion']}")


def launch(results, number, deployment, selection):
    response = aws(
        "lambda-microvms", "run-microvm", "--image-identifier", deployment["imageArn"],
        "--image-version", deployment["imageVersion"], "--execution-role-arn",
        os.environ.get("EXECUTION_ROLE_ARN", BUILD_ROLE), "--maximum-duration-in-seconds", "600",
        "--idle-policy", json.dumps({"maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 300,
                                     "autoResumeEnabled": False}),
    )
    # Persist the cleanup inventory before reading the endpoint or waiting for startup.
    vm = {"microvmId": response["microvmId"], "start": time.time(),
          "imageArn": deployment["imageArn"], "imageVersion": deployment["imageVersion"]}
    path = results / "candidates" / f"vm{number}.json"
    save(path, vm)
    selection["candidates"].append({
        "number": number, "microvmId": vm["microvmId"], "beforeRuntimeId": None,
    })
    save(results / "selection.json", selection)
    ready = wait("get-microvm", ["--microvm-identifier", vm["microvmId"]], "RUNNING", 180)
    vm["endpoint"] = ready.get("endpoint") or response["endpoint"]
    save(path, vm)
    return vm


def guest_request(vm, path, data=None):
    endpoint = vm["endpoint"]
    if "://" not in endpoint:
        endpoint = "https://" + endpoint
    if urllib.parse.urlparse(endpoint).scheme != "https":
        raise ValueError("MicroVM ingress requires HTTPS")
    token = aws("lambda-microvms", "create-microvm-auth-token", "--microvm-identifier", vm["microvmId"],
                "--expiration-in-minutes", "15", "--allowed-ports", '[{"port":8080}]')["authToken"]["X-aws-proxy-auth"]
    request = urllib.request.Request(endpoint.rstrip("/") + path, data=data, headers={
        "Content-Type": "application/json", "X-aws-proxy-auth": token, "X-aws-proxy-port": "8080",
    })
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def prepared_runtime_id(state):
    if not isinstance(state, dict):
        raise ValueError("Expected a prepared MicroVM /state object")
    before, run = state.get("before"), state.get("run")
    before_id = before.get("runtimeId") if isinstance(before, dict) else None
    run_id = run.get("runtimeId") if isinstance(run, dict) else None
    if (state.get("prepared") is not True or type(state.get("runCount")) is not int
            or state["runCount"] != 1
            or not isinstance(before_id, str) or not before_id.strip()
            or not isinstance(run_id, str) or not run_id.strip()
            or state.get("runtimeId") != run_id):
        raise ValueError("Expected prepared=true, runCount=1, nonempty before/run runtimeId, "
                         "and current runtimeId matching the real run hook; see candidates/state*.json")
    return before_id


def verify(results, vm, filename):
    # Only selected candidates reach this repeatable endpoint; AWS alone calls ready/run.
    result = guest_request(vm, "/verify", b"{}")
    save(results / filename, result)
    vm["end"] = time.time()
    for name, passed in result.get("checks", {}).items():
        print(f"{'PASS' if passed is True else 'FAIL'} {filename}: {name}")
    if not result.get("checks") or result.get("passed") is not True:
        raise RuntimeError(f"Guest verification failed; see {filename}")


def run(results):
    deployment = json.loads((results / "deployment.json").read_text())
    (results / "candidates").mkdir(exist_ok=True)
    selection = {"status": "inconclusive", "limit": CANDIDATE_LIMIT, "candidates": []}
    save(results / "selection.json", selection)
    try:
        seen = {}
        matched = []
        for number in range(1, CANDIDATE_LIMIT + 1):
            vm = launch(results, number, deployment, selection)
            state = guest_request(vm, "/state")
            save(results / "candidates" / f"state{number}.json", state)
            before_id = prepared_runtime_id(state)
            selection["candidates"][-1]["beforeRuntimeId"] = before_id
            if before_id in seen:
                matched = [seen[before_id], (number, vm)]
                selection.update(status="matched", selected=[row[0] for row in matched])
            save(results / "selection.json", selection)
            if matched:
                break
            seen[before_id] = (number, vm)
        if not matched:
            selection["reason"] = (
                f"No two of {CANDIDATE_LIMIT} candidates shared a prepared before.runtimeId. "
                "The image version may contain multiple snapshot builds; rerun to obtain a shared baseline. "
                "The shared_snapshot_before_identity assertion is still required."
            )
            save(results / "selection.json", selection)
            raise RuntimeError("Inconclusive snapshot selection: " + selection["reason"] + " See selection.json.")

        def save_selected(sample):
            number, vm = matched[sample - 1]
            save(results / "candidates" / f"vm{number}.json", vm)
            save(results / f"vm{sample}.json", vm)

        first, second = (row[1] for row in matched)
        save_selected(1)
        save_selected(2)
        verify(results, first, "verification-1.json")
        save_selected(1)
        verify(results, second, "verification-2.json")
        save_selected(2)
        args = ["--microvm-identifier", first["microvmId"]]
        aws("lambda-microvms", "suspend-microvm", *args)
        suspended = wait("get-microvm", args, "SUSPENDED", 90)
        first["suspended"] = {"state": suspended["state"], "time": time.time()}
        save_selected(1)
        aws("lambda-microvms", "resume-microvm", *args)
        resumed = wait("get-microvm", args, "RUNNING", 90)
        first["resumed"] = {"state": resumed["state"], "time": time.time()}
        save_selected(1)
        verify(results, first, "verification-resumed.json")
        save_selected(1)
        from check import evaluate_guest
        checks = evaluate_guest(results)
        save(results / "clone-checks.json", {"passed": all(checks.values()), "checks": checks})
        for name, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'} {name}")
        if not all(checks.values()):
            raise RuntimeError("Snapshot clone/resume assertions failed; see clone-checks.json")
    finally:
        terminate(results)


def terminate(results):
    failures = []
    inventory = {}
    paths = sorted((results / "candidates").glob("vm*.json"))
    paths += [results / f"vm{number}.json" for number in (1, 2)]
    for path in paths:
        if not path.exists():
            continue
        try:
            vm = json.loads(path.read_text())
            inventory.setdefault(vm["microvmId"], []).append((path, vm))
        except (OSError, ValueError, KeyError, TypeError) as error:
            failures.append(f"{path.name}: {error}")
    for microvm_id, records in inventory.items():
        # Root aliases can have newer lifecycle evidence. Merge it rather than replacing it.
        vm = {}
        for _, value in records:
            vm.update(value)
        terminated = next((value for _, value in records if value.get("terminated") is True), None)
        args = ["--microvm-identifier", microvm_id]
        try:
            if terminated is not None:
                vm["terminated"] = True
                if "terminatedAt" in terminated:
                    vm["terminatedAt"] = terminated["terminatedAt"]
            else:
                try:
                    aws("lambda-microvms", "terminate-microvm", *args)
                except subprocess.CalledProcessError as error:
                    if "ResourceNotFoundException" not in (error.stderr or ""):
                        raise
                deadline = time.monotonic() + 120
                while True:
                    try:
                        state = aws("lambda-microvms", "get-microvm", *args)["state"]
                    except subprocess.CalledProcessError as error:
                        if "ResourceNotFoundException" not in (error.stderr or ""):
                            raise
                        state = "TERMINATED"
                    if state == "TERMINATED":
                        vm.update(terminated=True, terminatedAt=time.time())
                        print(f"Terminated: {microvm_id}")
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Termination did not finish for {microvm_id}")
                    time.sleep(5)
            for path, original in records:
                if original != vm:
                    save(path, vm)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError, RuntimeError) as error:
            # Attempt every candidate, even if an earlier termination failed.
            failures.append(f"{microvm_id}: {getattr(error, 'stderr', None) or error}")
    if failures:
        raise RuntimeError("; ".join(failures))


def logs(results):
    deployment = json.loads((results / "deployment.json").read_text())
    response = aws("logs", "filter-log-events", "--log-group-name", deployment["logGroup"],
                   "--start-time", str(int(deployment["start"] * 1000)))
    save(results / "cloudwatch.json", response)
    print(f"Saved {len(response.get('events', []))} CloudWatch events")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "run", "terminate", "logs"])
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    try:
        globals()[args.action](args.results.resolve())
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr or f"Command failed with exit {error.returncode}") from None
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
