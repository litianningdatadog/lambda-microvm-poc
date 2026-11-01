"""Offline checks for scenario sequencing and lifecycle metric attribution."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import check
import run4


class CheckAuthenticationTests(unittest.TestCase):
    def test_wrapper_uses_application_key_and_forwards_check_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / "dd-auth"
            shim.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$AUTH_ARGS"\n')
            shim.chmod(0o755)
            args_file = root / "args.txt"
            env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                   "AUTH_ARGS": str(args_file), "DD_AUTH_DOMAIN": "dddev.datadoghq.com"}
            result = subprocess.run(
                ["zsh", str(Path(__file__).with_name("verify.sh")), "check", "run2", "microvm-test"],
                env=env, capture_output=True, text=True, check=True,
            )
            args = args_file.read_text().splitlines()
            self.assertEqual(args[:4], ["--domain", "dddev.datadoghq.com", "--force-app-key", "--"])
            self.assertEqual(args[-2:], ["run2", "microvm-test"])
            self.assertIn("auth=application-key", result.stdout)


class ShortGapTests(unittest.TestCase):
    def client(self, states):
        client = Mock()
        client.run_microvm.return_value = {"microvmId": "microvm-test"}
        client.exceptions.ResourceNotFoundException = LookupError
        client.get_microvm.side_effect = states
        return client

    def execute(self, client):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.log"
            with patch.object(run4.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
                run4.run(client, "image", "43.0", "role", events)
            return events.read_text(), sleep

    def test_persistent_client_resumes_then_polls_and_suspends(self):
        client = self.client([{"state": s} for s in (
            "RUNNING", "SUSPENDED", "RESUMING", "RUNNING", "SUSPENDED", "TERMINATED",
        )])
        events, sleep = self.execute(client)
        self.assertEqual(client.suspend_microvm.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(30), call(30), call(0.1)])
        methods = [c[0] for c in client.mock_calls]
        resumed = methods.index("resume_microvm")
        self.assertEqual(methods[resumed:resumed + 4], [
            "resume_microvm", "get_microvm", "get_microvm", "suspend_microvm",
        ])
        self.assertIn("run4 microvm-test reached TERMINATED", events)
        client.run_microvm.assert_called_once_with(
            imageIdentifier="image", imageVersion="43.0", executionRoleArn="role",
        )

    def test_unexpected_termination_fails_and_attempts_cleanup(self):
        client = self.client([{"state": s} for s in ("RUNNING", "SUSPENDED", "TERMINATED")])
        with self.assertRaisesRegex(RuntimeError, "wanted RUNNING, got TERMINATED"):
            self.execute(client)
        client.terminate_microvm.assert_called_once_with(microvmIdentifier="microvm-test")

    def test_disappearance_after_termination_is_accepted(self):
        client = self.client([
            *({"state": s} for s in ("RUNNING", "SUSPENDED", "RUNNING", "SUSPENDED")),
            LookupError(),
        ])
        events, _ = self.execute(client)
        self.assertIn("reached NOT_FOUND", events)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        check.results.clear()

    def execute(self, deploy, points, known=()):
        output = io.StringIO()
        with patch.object(check, "dd_buckets", return_value=points) as query, contextlib.redirect_stdout(output):
            check.check_distribution_control(deploy, "service:sample-verify-app", 100, 120,
                                             {100: 10}, [(100, 110, "suspend")], known, {})
        self.output = output.getvalue()
        return query

    def test_old_images_do_not_fail_for_a_control_they_never_emitted(self):
        query = self.execute("deploy long stats_computation=false", {})
        query.assert_not_called()
        self.assertEqual(check.results[0][0], "INFO")

    def test_new_image_requires_custom_distribution_delivery(self):
        query = self.execute("deploy long distribution_control=true", {})
        self.assertEqual(check.results[0][0], "FAIL")
        self.assertEqual(query.call_args.args[0],
                         "count:mvmtest.distribution{service:sample-verify-app}.rollup(10)")

    def test_delivered_control_passes(self):
        self.execute("deploy long distribution_control=true", {100: 10})
        self.assertEqual(check.results[0][0], "PASS")

    def test_final_suspend_open_bucket_retains_known_exception(self):
        self.execute("deploy long distribution_control=true", {}, known={100})
        self.assertEqual(check.results[0][0], "PASS")
        self.assertIn("KNOWN", self.output)


class MetricVisibilityTests(unittest.TestCase):
    def setUp(self):
        check.results.clear()

    def execute(self, attributes):
        with patch.object(check, "dd", return_value={"data": {"attributes": attributes}}), \
                contextlib.redirect_stdout(io.StringIO()):
            check.probe_indexed_metric("aws.lambda.microvm.enhanced.terminate", "microvm-test")
        self.assertEqual(check.results[0][0], "INFO")
        return check.results[0][2]

    def test_indexed_vm_reports_visibility_gap_without_passing_count(self):
        message = self.execute({"tags": ["lambda_microvm_id:microvm-test"]})
        self.assertIn("this VM tag is indexed", message)
        self.assertIn("do not validate", message)

    def test_ingested_but_unindexed_tag_is_distinguished(self):
        message = self.execute({"ingested_tags": ["lambda_microvm_id:microvm-test"]})
        self.assertIn("ingested but not indexed", message)

    def test_other_vm_tag_does_not_prove_this_vm_was_indexed(self):
        message = self.execute({"tags": ["lambda_microvm_id:another-vm"]})
        self.assertIn("does not prove data loss", message)

    def test_lookup_failure_does_not_mask_original_verdict(self):
        import urllib.error
        check.results.append(("FAIL", "lifecycle-term", "missing count"))
        failure = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        self.addCleanup(failure.close)
        with patch.object(check, "dd", side_effect=failure), \
                contextlib.redirect_stdout(io.StringIO()):
            check.probe_indexed_metric("aws.lambda.microvm.enhanced.terminate", "microvm-test")
        self.assertEqual(check.results[0][0], "FAIL")
        self.assertIn("lookup unavailable", check.results[1][2])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        check.results.clear()
        self.index_probe_patch = patch.object(check, "probe_indexed_metric")
        self.index_probe = self.index_probe_patch.start()
        self.addCleanup(self.index_probe_patch.stop)

    def response(self, count, scope="lambda_microvm_id:microvm-test"):
        return {"series": [{"scope": scope, "pointlist": [[100000, count]]}]} if count else {"series": []}

    def execute(self, hooks, responses):
        lifecycle = [(100 + n, f"MicroVM lifecycle: {hook} — flushing telemetry")
                     for n, hook in enumerate(hooks)]
        with patch.object(check, "dd", side_effect=responses) as dd, contextlib.redirect_stdout(io.StringIO()):
            check.check_lifecycle_metrics(lifecycle, "microvm-test", 101, 119)
        return dd

    def test_terminate_is_scoped_and_queries_whole_surrounding_buckets(self):
        dd = self.execute(["terminate"], [self.response(1)])
        params = dd.call_args.args[1]
        self.assertEqual((params["from"], params["to"]), (90, 130))
        self.assertEqual(params["query"],
                         "count:aws.lambda.microvm.enhanced.terminate{lambda_microvm_id:microvm-test}.rollup(10)")
        self.assertEqual(check.results[0][0], "PASS")

    def test_last_suspend_can_be_missing_but_previous_one_must_arrive(self):
        self.execute(["suspend", "suspend"], [self.response(1)])
        self.assertEqual(check.results[0][0], "KNOWN")
        self.assertIn("expected=1..2", check.results[0][2])

    def test_unfiltered_other_vm_data_does_not_satisfy_missing_terminate(self):
        dd = self.execute(["terminate"], [
            self.response(0), self.response(4),
            self.response(4, "lambda_microvm_id:another-vm"), self.response(0),
        ])
        self.assertEqual(check.results[0][0], "FAIL")
        self.assertEqual(dd.call_count, 4)
        self.assertIn("{*} by {lambda_microvm_id}", dd.call_args_list[2].args[1]["query"])
        self.assertTrue(any("another-vm" in detail for _, _, detail in check.results))
        self.index_probe.assert_called_once_with("aws.lambda.microvm.enhanced.terminate", "microvm-test")

    def test_missing_id_tag_does_not_hide_unfiltered_series(self):
        self.execute(["terminate"], [self.response(0), self.response(4), self.response(0), self.response(0)])
        self.assertEqual(check.results[0][0], "FAIL")
        self.assertTrue(any("count=4" in detail for _, _, detail in check.results))
        self.assertTrue(any("0 series" in detail for status, name, detail in check.results
                            if name == "lifecycle-tags"))

    def test_final_suspend_without_resume_remains_known(self):
        self.execute(["suspend"], [self.response(0)] * 3)
        self.assertEqual(check.results[0][0], "KNOWN")
        self.assertFalse(any(status == "FAIL" for status, _, _ in check.results))

    def test_api_query_errors_are_not_treated_as_zero_counts(self):
        with self.assertRaisesRegex(RuntimeError, "metric query failed"):
            self.execute(["terminate"], [{"errors": ["invalid query"]}])


if __name__ == "__main__":
    unittest.main()
