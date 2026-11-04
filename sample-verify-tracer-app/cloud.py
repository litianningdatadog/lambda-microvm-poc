"""AWS operations for run-all.sh. Credentials and proxy tokens stay out of evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

# CLI invocations below use argument lists, never shell interpretation.
import subprocess  # nosec B404
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

from lifecycle_checks import SCHEMA


APP = Path(__file__).resolve().parent
REGION = os.environ.get("REGION", "us-east-2")
NAME = os.environ.get("IMAGE_NAME", "sample-verify-tracer-app")
PROFILE = os.environ.get("AWS_VAULT_PROFILE", "sso-serverless-sandbox-account-admin")
BUILD_ROLE = os.environ.get("BUILD_ROLE_ARN", "arn:aws:iam::425362996713:role/microvm-build-role")


def aws(service, operation, *args):
    command = [
        "aws-vault",
        "exec",
        PROFILE,
        "--",
        "aws",
        service,
        operation,
        "--region",
        REGION,
        "--output",
        "json",
        *args,
    ]
    result = subprocess.run(  # nosec B603
        command, check=True, capture_output=True, text=True, env={**os.environ, "AWS_PAGER": ""}
    )
    return json.loads(result.stdout or "{}")


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
    rss_alias = os.environ.get("VERIFY_RUNTIME_RSS_ALIAS", "")
    if rss_alias and (not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.]+", rss_alias) or rss_alias == "runtime.python.mem.rss"):
        raise ValueError("VERIFY_RUNTIME_RSS_ALIAS must be a distinct DogStatsD metric name")
    aws("lambda-microvms", "list-managed-microvm-images")
    # Run the fixed build target in this sample's Makefile.
    subprocess.run(["make", "prepare"], cwd=APP, check=True)  # nosec B603, B607
    wheel = Path(
        os.environ.get(
            "WHEEL", "/Users/tianning.li/DataDog/dd-trace-py/dist/ddtrace-4.17.0rc1-cp312-cp312-linux_aarch64.whl"
        )
    ).name
    archive = results / "app.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        dockerfile = (APP / "Dockerfile").read_text().splitlines()
        package.writestr(
            "Dockerfile",
            "\n".join(
                f"ARG DD_TRACE_WHEEL={wheel}" if line.startswith("ARG DD_TRACE_WHEEL=") else line for line in dockerfile
            )
            + "\n",
        )
        for name in (
            "app.py",
            "intake.py",
            "delivery.py",
            "delivery_checks.py",
            "features.py",
            "feature_checks.py",
            "lifecycle_checks.py",
            "lifecycle_control.py",
            "retirement_checks.py",
            "retirement_control.py",
            "requirements.txt",
            wheel,
            "serverless-init-linux-arm64",
        ):
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
            "ready": "ENABLED",
            "readyTimeoutInSeconds": 60,
            "validate": "ENABLED",
            "validateTimeoutInSeconds": 30,
        },
        "microvmHooks": {
            "run": "ENABLED",
            "runTimeoutInSeconds": 5,
            "terminate": "ENABLED",
            "terminateTimeoutInSeconds": 30,
            "suspend": "ENABLED",
            "suspendTimeoutInSeconds": 30,
            "resume": "ENABLED",
            "resumeTimeoutInSeconds": 30,
        },
    }
    environment = {
        "DD_API_KEY": key,
        "DD_SITE": os.environ.get("DD_SITE", "datadoghq.com"),
        "DD_SERVICE": "sample-verify-tracer-app",
        "DD_VERSION": results.name,
        "DD_ENV": "devmicrovm",
        "DD_LOGS_ENABLED": "true",
        "DD_SOURCE": "python",
        "DD_LOG_LEVEL": "debug",
        "DD_AWS_MICROVM_USER_APP_PORT": "8080",
        "DD_AWS_MICROVM_ENABLE_READY": "true",
        "DD_AWS_MICROVM_ENABLE_VALIDATE": "true",
        "DD_AWS_MICROVM_ENABLE_RUN": "true",
        "DD_AWS_MICROVM_ENABLE_TERMINATE": "true",
        "DD_AWS_MICROVM_ENABLE_SUSPEND": "true",
        "DD_AWS_MICROVM_ENABLE_RESUME": "true",
        "DD_AWS_MICROVM_FORWARD_TIMEOUT_MS": "15000",
        "VERIFY_FORWARD_TO_AGENT": "true",
        "VERIFY_RUNTIME_RSS_ALIAS": rss_alias,
        "DD_TRACE_AGENT_URL": "http://127.0.0.1:18126",
        "DD_DOGSTATSD_URL": "udp://127.0.0.1:18125",
    }
    # Use an owner-only temporary file; never print the environment or save it in results.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as secret:
        json.dump(environment, secret)
        secret.flush()
        args = [
            "--code-artifact",
            f"uri=s3://{bucket}/{object_key}",
            "--base-image-arn",
            os.environ.get("BASE_IMAGE_ARN", f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"),
            "--build-role-arn",
            BUILD_ROLE,
            "--hooks",
            json.dumps(hooks),
            "--environment-variables",
            "file://" + secret.name,
            "--cpu-configurations",
            '[{"architecture":"ARM_64"}]',
        ]
        existing = aws("lambda-microvms", "list-microvm-images", "--name-filter", NAME)
        image = next((i["imageArn"] for i in existing.get("items", []) if i["name"] == NAME), None)
        if image:
            response = aws("lambda-microvms", "update-microvm-image", "--image-identifier", image, *args)
        else:
            response = aws("lambda-microvms", "create-microvm-image", "--name", NAME, *args)
    metadata = {
        "imageArn": response["imageArn"],
        "imageVersion": str(response["imageVersion"]),
        "imageName": NAME,
        "region": REGION,
        "verification_id": results.name,
        "verification_schema": SCHEMA,
        "runtime_rss_alias": rss_alias or None,
        "start": time.time(),
        "logGroup": f"/aws/lambda-microvms/{NAME}",
        "wheel_sha256": hashlib.sha256((APP / wheel).read_bytes()).hexdigest(),
        "serverless_init_sha256": hashlib.sha256((APP / "serverless-init-linux-arm64").read_bytes()).hexdigest(),
    }
    save(results / "deployment.json", metadata)
    wait(
        "get-microvm-image-version",
        ["--image-identifier", metadata["imageArn"], "--image-version", metadata["imageVersion"]],
        "SUCCESSFUL",
        int(os.environ.get("BUILD_TIMEOUT", "1800")),
    )
    print(f"Image ready: {metadata['imageArn']}:{metadata['imageVersion']}")


def run(results):
    deployment = json.loads((results / "deployment.json").read_text())
    response = aws(
        "lambda-microvms",
        "run-microvm",
        "--image-identifier",
        deployment["imageArn"],
        "--image-version",
        deployment["imageVersion"],
        "--execution-role-arn",
        os.environ.get("EXECUTION_ROLE_ARN", BUILD_ROLE),
        "--maximum-duration-in-seconds",
        "600",
        "--idle-policy",
        '{"autoResumeEnabled":false,"maxIdleDurationSeconds":900,"suspendedDurationSeconds":300}',
    )
    # Persist the ID immediately, so the EXIT trap can terminate even if startup fails.
    vm = {"microvmId": response["microvmId"], "endpoint": response["endpoint"], "start": time.time(), "cycles": []}
    save(results / "vm.json", vm)
    wait("get-microvm", ["--microvm-identifier", vm["microvmId"]], "RUNNING", 300)
    token = aws(
        "lambda-microvms",
        "create-microvm-auth-token",
        "--microvm-identifier",
        vm["microvmId"],
        "--expiration-in-minutes",
        "15",
        "--allowed-ports",
        '[{"port":8080}]',
    )["authToken"]["X-aws-proxy-auth"]
    endpoint = vm["endpoint"]
    if not endpoint.startswith("https://"):
        endpoint = "https://" + endpoint
    if urllib.parse.urlparse(endpoint).scheme != "https":
        raise ValueError("MicroVM ingress requires HTTPS")

    def post(route, filename):
        request = urllib.request.Request(
            endpoint.rstrip("/") + route,
            data=b"{}",
            headers={"Content-Type": "application/json", "X-aws-proxy-auth": token},
        )
        with urllib.request.urlopen(request, timeout=90) as reply:  # nosec B310
            value = json.load(reply)
        save(results / filename, value)
        checks = value.get("checks", {})
        for name, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'} {filename}: {name}")
        if not checks or not all(passed is True for passed in checks.values()) or value.get("passed") is not True:
            raise RuntimeError(f"Guest verification failed; see {filename}")
        return value

    # AWS already called /ready and /run; never synthesize them on a running VM.
    post("/verify", "verification.json")
    post("/delivery/automatic", "delivery-automatic.json")
    post("/delivery/recovery", "delivery-recovery.json")
    args = ["--microvm-identifier", vm["microvmId"]]
    for cycle in range(1, 4):
        transition = {"cycle": cycle}
        vm["cycles"].append(transition)
        save(results / "vm.json", vm)
        for action, wanted, key in (
            ("suspend-microvm", "SUSPENDED", "suspended"),
            ("resume-microvm", "RUNNING", "resumed"),
        ):
            aws("lambda-microvms", action, *args)
            observed = wait("get-microvm", args, wanted, 120)
            transition[key] = {
                "state": observed["state"],
                "time": time.time(),
                "microvmId": vm["microvmId"],
            }
            save(results / "vm.json", vm)
        post("/verify/features", f"verification-resumed-{cycle}.json")
        post("/delivery/automatic", f"automatic-resumed-{cycle}.json")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/delivery/termination",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-aws-proxy-auth": token},
    )
    with urllib.request.urlopen(request, timeout=15) as reply:  # nosec B310
        armed = json.load(reply)
    save(results / "termination-armed.json", armed)
    if armed.get("armed") is not True:
        raise RuntimeError("Final termination batch was not armed")
    vm["end"] = time.time()
    save(results / "vm.json", vm)


def terminate(results):
    path = results / "vm.json"
    if path.exists():
        vm = json.loads(path.read_text())
        args = ["--microvm-identifier", vm["microvmId"]]
        try:
            aws("lambda-microvms", "terminate-microvm", *args)
        except subprocess.CalledProcessError as error:
            if "ResourceNotFoundException" not in (error.stderr or ""):
                raise
            vm["end"] = time.time()
            vm["terminated"] = True
            save(path, vm)
            return
        deadline = time.monotonic() + 120
        while True:
            try:
                status = aws("lambda-microvms", "get-microvm", *args)["state"]
            except subprocess.CalledProcessError as error:
                if "ResourceNotFoundException" not in (error.stderr or ""):
                    raise
                status = "TERMINATED"
            if status == "TERMINATED":
                print(f"Terminated: {vm['microvmId']}")
                vm["end"] = time.time()
                vm["terminated"] = True
                save(path, vm)
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Termination did not finish for {vm['microvmId']}")
            time.sleep(5)


def logs(results):
    deployment = json.loads((results / "deployment.json").read_text())
    response = aws(
        "logs",
        "filter-log-events",
        "--log-group-name",
        deployment["logGroup"],
        "--start-time",
        str(int(deployment["start"] * 1000)),
    )
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


if __name__ == "__main__":
    main()
