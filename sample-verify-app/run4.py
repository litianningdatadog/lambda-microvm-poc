# /// script
# dependencies = ["boto3>=1.42.57"]
# ///
"""Run the short resume/suspend scenario in one authenticated AWS session."""
import argparse
from datetime import datetime, timezone
import time


def run(client, image, version, role, events_log):
    vm_id = client.run_microvm(
        imageIdentifier=image, imageVersion=version, executionRoleArn=role,
    )["microvmId"]

    def note(message):
        line = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} run4 {vm_id} {message}"
        with open(events_log, "a") as out:
            out.write(line + "\n")
        print(line, flush=True)

    def wait_state(wanted, poll=1):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                response = client.get_microvm(microvmIdentifier=vm_id)
                state = response["state"]
                reason = response.get("stateReason")
            except client.exceptions.ResourceNotFoundException:
                state, reason = "NOT_FOUND", None
            if state == wanted or (wanted == "TERMINATED" and state == "NOT_FOUND"):
                note(f"reached {state}\t{reason}")
                return
            if state == "TERMINATED":
                raise RuntimeError(f"wanted {wanted}, got {state}: {reason}")
            time.sleep(poll)
        raise TimeoutError(f"waiting for {wanted}; last state={state}")

    note("run-microvm called (image long, no idle policy)")
    try:
        wait_state("RUNNING")
        time.sleep(30)  # Establish trace/stats connections before the first freeze.
        client.suspend_microvm(microvmIdentifier=vm_id)
        note("suspend-microvm called")
        wait_state("SUSPENDED")
        time.sleep(30)
        client.resume_microvm(microvmIdentifier=vm_id)
        note("resume-microvm called")
        # No CLI startup or credential acquisition between resume and suspend.
        wait_state("RUNNING", poll=0.1)
        client.suspend_microvm(microvmIdentifier=vm_id)
        note("suspend-microvm called")
        wait_state("SUSPENDED")
        client.terminate_microvm(microvmIdentifier=vm_id)
        note("terminate-microvm called")
        wait_state("TERMINATED")
    except Exception as exc:
        note(f"ERROR: {exc}")
        # Bound resource lifetime even when the scenario fails midway through.
        try:
            client.terminate_microvm(microvmIdentifier=vm_id)
        except Exception as cleanup_error:
            note(f"ERROR: cleanup failed: {cleanup_error}")
        raise


def main():
    import boto3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--events-log", required=True)
    args = parser.parse_args()
    client = boto3.client("lambda-microvms", region_name=args.region)
    run(client, args.image, args.version, args.role, args.events_log)


if __name__ == "__main__":
    main()
