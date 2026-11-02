"""Offline evidence fixtures for automatic delivery, fault recovery, and termination."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import check_delivery as delivery


def marker(scenario, wall, resumes=0, terminated=0):
    return {"msg": "mvmtest_delivery", "schema": 1, "scenario": scenario,
            "marker": str(uuid.uuid4()), "runId": "run-test", "wall": wall,
            "value": 42, "counters": {"runCount": 1, "resumeCount": resumes,
                                      "suspendCount": resumes, "terminateCount": terminated}}


def logs(row):
    return {"data": [{"id": "log-1", "type": "log", "attributes": {
        "message": "mvmtest_delivery", "attributes": copy.deepcopy(row)}}],
        "meta": {"status": "done"}}


def sample(row):
    return {"marker": row, "observedAt": row["wall"] + 1, "logs": logs(row),
            "metrics": {"status": "ok", "series": [{"metric": "mvmtest.delivery",
                "scope": "service:sample-verify-app,marker:" + row["marker"] + ",run_id:run-test",
                "pointlist": [[row["wall"] * 1000, 42]]}]},
            "health": [{"status": 200, "elapsedMs": 25, "at": row["wall"] - 0.5}]}


def evidence():
    samples = [sample(marker("initial", 1001)),
               sample(marker("recovery-http503", 1010)),
               sample(marker("recovery-disconnect", 1020))]
    recoveries = []
    for mode, start in (("http503", 1003), ("disconnect", 1013)):
        recoveries.append({"mode": mode, "startedAt": start, "restoredAt": start + 5,
            "freshScenario": "recovery-" + mode,
            "health": [{"status": 200, "elapsedMs": 25, "at": start + n + 1}
                       for n in range(3)],
            "requests": [{"pipeline": pipeline, "mode": mode,
                "outcome": "injected-" + mode, "status": 503 if mode == "http503" else None,
                "receivedAt": start + 1, "completedAt": start + 2}
                for pipeline in ("logs", "metrics")]})
    cycles = []
    for n in range(1, 4):
        start = 1020 + n * 10
        cycles.append({"cycle": n, "suspended": {"state": "SUSPENDED", "at": start},
                       "resumed": {"state": "RUNNING", "at": start + 2}})
        samples.append(sample(marker(f"resumed-{n}", start + 3, n)))
    arm = marker("termination", 1060, 3)
    final = copy.deepcopy(arm)
    final["wall"] = 1061
    final["counters"]["terminateCount"] = 1
    arm["armed"] = True
    return {"schema": 1, "startedAt": 1000, "endedAt": 1070, "deadlineSeconds": 180,
            "vm": {"microvmId": "microvm-test", "imageArn": "arn:image", "imageVersion": "1.0",
                   "terminated": True, "terminatedAt": 1062},
            "samples": samples, "cycles": cycles, "recoveries": recoveries,
            "termination": {"arm": arm, "observedAt": 1064, "logs": logs(final)},
            "cloudwatch": [{"at": 1062, "row": final}]}


class DeliveryEvidenceTests(unittest.TestCase):
    def assert_rejected(self, fixture, check=None):
        result = delivery.evaluate(fixture)
        self.assertFalse(result["passed"])
        if check:
            self.assertFalse(result["checks"][check])

    def test_complete_run_passes_without_mutating_evidence(self):
        fixture = evidence()
        before = copy.deepcopy(fixture)
        result = delivery.evaluate(fixture)
        self.assertTrue(result["passed"], result["checks"])
        self.assertTrue(all(type(value) is bool for value in result["checks"].values()))
        self.assertIs(result["evidence"], fixture)
        self.assertEqual(fixture, before)

    def test_missing_or_malformed_top_level_never_passes(self):
        for fixture in (None, [], {}, {"schema": 1}, {"samples": [None, {}]}):
            with self.subTest(fixture=fixture):
                self.assert_rejected(fixture)
        for key in evidence():
            fixture = evidence()
            del fixture[key]
            with self.subTest(key=key):
                self.assert_rejected(fixture)

    def test_backend_marker_must_match_every_field(self):
        replacements = {"marker": str(uuid.uuid4()), "runId": "other", "scenario": "resumed-1",
                        "value": 41, "schema": 2, "wall": 900, "msg": "wrong",
                        "counters": {"runCount": 99}}
        for key, value in replacements.items():
            fixture = evidence()
            fixture["samples"][0]["logs"]["data"][0]["attributes"]["attributes"][key] = value
            with self.subTest(key=key):
                self.assert_rejected(fixture, "initial.backend")

    def test_backend_metric_requires_matching_metric_scope_and_42(self):
        mutations = [("metric", "another.metric"), ("scope", "service:sample-verify-app"),
                     ("scope", "service:sample-verify-app,marker:wrong,run_id:run-test"),
                     ("scope", None), ("pointlist", [[1000000, 41]]),
                     ("pointlist", [[1000000, None]]), ("pointlist", [[1000000]]),
                     ("pointlist", [[1000000, True]]), ("pointlist", [])]
        for key, value in mutations:
            fixture = evidence()
            fixture["samples"][0]["metrics"]["series"][0][key] = value
            with self.subTest(key=key, value=value):
                self.assert_rejected(fixture, "initial.backend")

    def test_poll_helper_requires_both_backends_unless_explicit_log_only(self):
        candidate = sample(marker("initial", 1001))
        self.assertTrue(delivery.marker_delivered(candidate))
        del candidate["metrics"]
        self.assertFalse(delivery.marker_delivered(candidate))
        self.assertTrue(delivery.marker_delivered(candidate, require_metric=False))
        candidate["logs"] = {"data": []}
        self.assertFalse(delivery.marker_delivered(candidate, require_metric=False))

    def test_partial_errors_and_malformed_responses_fail(self):
        for field, response in (("logs", {"data": []}), ("logs", {"data": [None]}),
                                ("logs", {"data": [{"attributes": []}]}),
                                ("metrics", {"status": "error", "series": []})):
            fixture = evidence()
            fixture["samples"][0][field] = response
            with self.subTest(response=response):
                self.assert_rejected(fixture, "initial.backend")
        for field in ("logs", "metrics"):
            for key, value in (("errors", ["backend failure"]), ("warnings", ["partial"]),
                               ("meta", {"status": "timeout"}),
                               ("meta", {"page": {"after": "next"}})):
                fixture = evidence()
                fixture["samples"][0][field][key] = value
                with self.subTest(field=field, key=key, value=value):
                    self.assert_rejected(fixture, "initial.backend")

    def test_remapped_message_and_tag_set_are_supported(self):
        fixture = evidence()
        candidate = fixture["samples"][0]
        del candidate["logs"]["data"][0]["attributes"]["attributes"]["msg"]
        series = candidate["metrics"]["series"][0]
        series["tag_set"] = series.pop("scope").split(",")
        self.assertTrue(delivery.evaluate(fixture)["passed"])

    def test_empty_tag_set_retains_exact_aggregate_scope_requirements(self):
        fixture = evidence()
        series = fixture["samples"][0]["metrics"]["series"][0]
        series["tag_set"] = []
        self.assertTrue(delivery.evaluate(fixture)["passed"])
        for scope in ("*", "service:another-app", ""):
            with self.subTest(scope=scope):
                series["scope"] = scope
                self.assert_rejected(fixture, "initial.backend")

    def test_duplicate_missing_or_shared_identity_samples_fail(self):
        fixture = evidence()
        fixture["samples"].pop()
        self.assert_rejected(fixture, "samples")
        fixture = evidence()
        fixture["samples"][1] = copy.deepcopy(fixture["samples"][0])
        self.assert_rejected(fixture, "samples")
        fixture = evidence()
        fixture["samples"][1]["marker"]["runId"] = "other"
        self.assert_rejected(fixture, "identity")
        fixture = evidence()
        fixture["samples"][1]["marker"]["marker"] = fixture["samples"][0]["marker"]["marker"]
        self.assert_rejected(fixture, "identity")

    def test_each_cycle_requires_order_states_counters_and_fast_health(self):
        for n in range(1, 4):
            for mutation in ("missing", "state", "order", "counter", "health"):
                fixture = evidence()
                if mutation == "missing":
                    fixture["cycles"].pop(n - 1)
                elif mutation == "state":
                    fixture["cycles"][n - 1]["suspended"]["state"] = "SUSPENDING"
                elif mutation == "order":
                    fixture["cycles"][n - 1]["resumed"]["at"] = 1000
                elif mutation == "counter":
                    fixture["samples"][n + 2]["marker"]["counters"]["resumeCount"] = n - 1
                else:
                    fixture["samples"][n + 2]["health"][0]["elapsedMs"] = 1000
                with self.subTest(cycle=n, mutation=mutation):
                    self.assert_rejected(fixture)
        fixture = evidence()
        fixture["cycles"][1]["suspended"]["at"] = 1033
        self.assert_rejected(fixture, "cycle-2.ordering")

    def test_each_fault_requires_both_pipelines_and_complete_timing(self):
        for index in range(2):
            for mutation in ("missing", "mode", "upstream", "timestamp", "unfinished", "health", "restore"):
                fixture = evidence()
                recovery = fixture["recoveries"][index]
                if mutation == "missing":
                    recovery["requests"].pop()
                elif mutation == "mode":
                    recovery["requests"][0]["mode"] = "normal"
                elif mutation == "upstream":
                    recovery["requests"][0]["outcome"] = "upstream-error"
                elif mutation == "timestamp":
                    recovery["requests"][0]["receivedAt"] = recovery["startedAt"] - 1
                elif mutation == "unfinished":
                    recovery["requests"][0]["completedAt"] = None
                elif mutation == "health":
                    recovery["health"].pop()
                else:
                    recovery["restoredAt"] = fixture["samples"][index + 1]["marker"]["wall"] + 1
                with self.subTest(index=index, mutation=mutation):
                    self.assert_rejected(fixture)

    def test_normal_inflight_accepted_requests_do_not_hide_fault_evidence(self):
        fixture = evidence()
        fixture["recoveries"][0]["requests"].append({"pipeline": "logs", "mode": "normal",
            "outcome": "accepted", "status": 202, "receivedAt": 1003, "completedAt": 1004})
        self.assertTrue(delivery.evaluate(fixture)["passed"])

    def test_automatic_deadlines_are_nonnegative_bounded_and_not_configurable_upward(self):
        for index in range(6):
            for delta in (-1, 181):
                fixture = evidence()
                fixture["samples"][index]["observedAt"] = fixture["samples"][index]["marker"]["wall"] + delta
                self.assert_rejected(fixture, fixture["samples"][index]["marker"]["scenario"] + ".deadline")
        fixture = evidence()
        fixture["deadlineSeconds"] = 900
        self.assert_rejected(fixture, "run")

    def test_termination_needs_actual_cw_row_backend_match_and_vm_state(self):
        for mutation in ("no-cw", "counter", "runId", "marker", "backend", "wall", "late", "state", "arm"):
            fixture = evidence()
            if mutation == "no-cw":
                fixture["cloudwatch"] = []
            elif mutation in ("counter", "runId", "marker"):
                row = fixture["cloudwatch"][0]["row"]
                if mutation == "counter":
                    row["counters"]["terminateCount"] = 0
                else:
                    row[mutation] = "other"
            elif mutation == "backend":
                fixture["termination"]["logs"]["data"][0]["attributes"]["attributes"]["wall"] -= 1
            elif mutation == "wall":
                fixture["cloudwatch"][0]["row"]["wall"] = 1059
            elif mutation == "late":
                fixture["termination"]["observedAt"] = 1300
            elif mutation == "state":
                fixture["vm"]["terminated"] = False
            else:
                fixture["termination"]["arm"]["counters"]["resumeCount"] = 2
            with self.subTest(mutation=mutation):
                self.assert_rejected(fixture)

    def test_health_probes_must_complete_inside_the_observed_state_window(self):
        for index in range(2):
            for position in ("missing", "before", "after", "completion-after"):
                fixture = evidence()
                recovery = fixture["recoveries"][index]
                probe = recovery["health"][0]
                if position == "missing":
                    del probe["at"]
                elif position == "before":
                    probe["at"] = recovery["startedAt"] - 1
                elif position == "after":
                    probe["at"] = recovery["restoredAt"] + 1
                else:
                    probe["at"] = recovery["restoredAt"] - 0.001
                with self.subTest(index=index, position=position):
                    self.assert_rejected(fixture, recovery["freshScenario"] + ".health")
        for n in range(1, 4):
            for position in ("before-resume", "after-marker"):
                fixture = evidence()
                sample = fixture["samples"][n + 2]
                sample["health"][0]["at"] = (
                    fixture["cycles"][n - 1]["resumed"]["at"] - 1
                    if position == "before-resume" else sample["marker"]["wall"])
                with self.subTest(cycle=n, position=position):
                    self.assert_rejected(fixture, f"resumed-{n}.health")

    def test_termination_requires_confirmed_arming(self):
        for armed in (False, None, 1, "true"):
            fixture = evidence()
            if armed is None:
                del fixture["termination"]["arm"]["armed"]
            else:
                fixture["termination"]["arm"]["armed"] = armed
            with self.subTest(armed=armed):
                self.assert_rejected(fixture, "termination.arm")

    def test_final_deadline_is_measured_from_emission_not_arm_or_termination(self):
        fixture = evidence()
        emitted = fixture["cloudwatch"][0]["row"]["wall"]
        fixture["termination"]["observedAt"] = emitted + 180
        fixture["endedAt"] = emitted + 181
        self.assertTrue(delivery.evaluate(fixture)["passed"])
        fixture["termination"]["observedAt"] += 1
        self.assert_rejected(fixture, "termination.deadline")

    def test_runner_error_prevents_success_even_with_complete_backend_evidence(self):
        fixture = evidence()
        fixture["error"] = "cleanup failed"
        self.assert_rejected(fixture, "no_runner_error")

    def test_cli_preserves_source_and_emits_named_result(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory, "run.json"), Path(directory, "result.json")
            fixture = evidence()
            source.write_text(json.dumps(fixture))
            before = source.read_bytes()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = delivery.main([str(source), "--output", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(json.loads(output.read_text())["evidence"], fixture)
            self.assertIn("PASS termination.backend", stdout.getvalue())

    def test_cli_rejects_source_aliases_without_touching_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, alias = Path(directory, "run.json"), Path(directory, "alias.json")
            source.write_text(json.dumps(evidence()))
            before = source.read_bytes()
            alias.hardlink_to(source)
            for output in (source, alias):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    delivery.main([str(source), "--output", str(output)])
                self.assertEqual(source.read_bytes(), before)

    def test_cli_failing_evidence_writes_failure_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory, "run.json"), Path(directory, "result.json")
            source.write_text("{}")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(delivery.main([str(source), "--output", str(output)]), 1)
            result = json.loads(output.read_text())
            self.assertFalse(result["passed"])
            self.assertEqual(result["evidence"], {})


if __name__ == "__main__":
    unittest.main()
