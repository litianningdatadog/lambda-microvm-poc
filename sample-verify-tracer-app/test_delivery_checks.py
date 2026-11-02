"""Deterministic consumer regressions using serialized evidence, not SDK mocks."""

import copy
import math
import unittest

from delivery_checks import DELIVERY_OPERATIONS, evaluate_cycles, evaluate_phase, evaluate_termination, log_values_equal


RUNTIME = "runtime-after-restore"
VERSION = "delivery-fixture-version"


def identity(number):
    return {"trace_id": str(number), "span_id": str(number + 1000)}


def wire_span(root, operation, marker="marker"):
    return {
        **root, "parent_id": "0", "name": operation,
        "service": "sample-verify-tracer-app",
        "meta": {"runtime-id": RUNTIME, "verify.marker": marker},
        "metrics": {"_dd.measured": 1},
    }


def request(root, operation, marker="marker", at=101, forwarded=True):
    return {
        "sequence": 1, "received_at": at, "completed_at": at + .1,
        "status": 200, "outcome": "accepted", "forwarded": forwarded,
        "bytes": 150, "spans": [wire_span(root, operation, marker)],
    }


def phase(scenario="automatic", forwarded=True):
    keys = {
        "automatic": ["default_interval", "no_manual_flush", "trace_acknowledged", "identity", "deadline"],
        "termination": ["no_manual_flush", "trace_acknowledged", "identity", "deadline", "hook_once"],
        "recovery": ["both_failures_observed", "application_responsive", "workload_bounded",
                     "fresh_delivery", "identity_stable", "no_manual_flush", "no_unexpected_errors"],
    }
    operation = DELIVERY_OPERATIONS[3 if scenario == "termination" else 0]
    root = identity(1)
    value = {
        "verification_schema": 4, "verification_id": VERSION,
        "scenario": scenario, "runtime_id": RUNTIME, "passed": True,
        "checks": {f"{scenario}_{name}": True for name in keys[scenario]},
        "evidence": {
            "runtime_id_before": RUNTIME, "runtime_id_after": RUNTIME,
            "forward_to_agent": forwarded, "manual_flush_count_before": 2,
            "manual_flush_count_after": 2, "started_at": 100,
            "elapsed_ms": 2000, "deadline_ms": 10000, "unexpected_errors": [],
            "operation": operation, "marker": "marker", "span": root,
            "flush_interval_ms": 1000,
            "successful_requests": [request(root, operation, forwarded=forwarded)],
        },
    }
    if scenario == "recovery":
        evidence = value["evidence"]
        for key in ("operation", "marker", "span", "flush_interval_ms", "successful_requests"):
            evidence.pop(key)
        evidence.update(elapsed_ms=10000, deadline_ms=40000, cases=[])
        for index, mode in enumerate(("http503", "disconnect")):
            active = 100 + 5 * index
            offered = [identity(100 + index * 100 + n) for n in range(24)]
            fresh = identity(900 + index)
            operation = f"verify.delivery.recovery.{mode}"
            failure = request(offered[0], f"verify.delivery.outage.{mode}", at=active + .05, forwarded=False)
            failure.update(status=503 if mode == "http503" else None, outcome=mode,
                           completed_at=active + .1 if mode == "http503" else None)
            evidence["cases"].append({
                "mode": mode, "offered_count": 24, "offered_spans": offered,
                "operation": operation, "fresh_span": fresh, "fresh_marker": f"fresh-{mode}",
                "failure_attempts": [failure],
                "fresh_acknowledgements": [request(fresh, operation, f"fresh-{mode}", active + 3, forwarded)],
                "probes": [{"at": active + offset, "status": 200, "runtime_id": RUNTIME, "elapsed_ms": 10}
                           for offset in (.2, .5, .8)],
                "fault_active_at": active, "restored_at": active + 2,
                "fresh_created_at": active + 2.1, "elapsed_ms": 4000, "deadline_ms": 20000,
                "runtime_id_before": RUNTIME, "runtime_id_after": RUNTIME, "unexpected_errors": [],
            })
    if scenario == "termination":
        value.update(marker="marker", lifecycle={"run": 1, "suspend": 3, "resume": 3, "terminate": 1})
        value["evidence"].update(
            marker_log={"msg": "tracer_termination_marker", "verification_id": VERSION,
                        "runtime_id": RUNTIME, "marker": "marker", **root},
            log_correlation={"dd.trace_id": root["trace_id"], "dd.span_id": root["span_id"]},
        )
    return value


def arm():
    return {"verification_schema": 4, "verification_id": VERSION, "scenario": "termination-armed",
            "armed": True, "marker": "marker", "runtime_id": RUNTIME, "operation": DELIVERY_OPERATIONS[3]}


def cycle_evidence():
    vm = {"microvmId": "microvm-fixture", "cycles": []}
    samples = []
    for cycle in range(1, 4):
        vm["cycles"].append({
            "cycle": cycle,
            "suspended": {"state": "SUSPENDED", "time": 1000 + cycle * 10, "microvmId": vm["microvmId"]},
            "resumed": {"state": "RUNNING", "time": 1005 + cycle * 10, "microvmId": vm["microvmId"]},
        })
        samples.append({
            "verification_schema": 4, "verification_id": VERSION,
            "scenario": "resumed-features", "cycle": cycle,
            "runtime_id": RUNTIME, "passed": True, "checks": {"feature_wire_evidence": True},
            "features": {"roots": {"manual": identity(100 + cycle)}},
            "feature_spans": [wire_span(identity(100 + cycle), "verify.feature.manual")],
            "lifecycle": {"run": 1, "suspend": cycle, "resume": cycle, "terminate": 0},
        })
    return vm, samples


class DeliveryChecksTest(unittest.TestCase):
    def assertPass(self, checks):
        self.assertTrue(checks)
        self.assertTrue(all(checks.values()), checks)
        self.assertTrue(all(type(value) is bool for value in checks.values()))

    def assertFail(self, checks):
        self.assertTrue(checks)
        self.assertFalse(all(checks.values()), checks)
        self.assertTrue(all(type(value) is bool for value in checks.values()))


    def test_positive_controlled_wire_fixtures(self):
        for scenario in ("automatic", "recovery", "termination"):
            with self.subTest(scenario=scenario):
                self.assertPass(evaluate_phase(phase(scenario), RUNTIME))
        self.assertPass(evaluate_termination(arm(), phase("termination"), RUNTIME))
        self.assertPass(evaluate_cycles(*cycle_evidence(), RUNTIME))

    def test_capture_is_only_accepted_when_explicitly_local(self):
        for scenario in ("automatic", "recovery", "termination"):
            value = phase(scenario, forwarded=False)
            self.assertFail(evaluate_phase(value, RUNTIME))
            self.assertPass(evaluate_phase(value, RUNTIME, require_forward=False))
        value = phase()
        value["evidence"]["successful_requests"][0]["forwarded"] = False
        self.assertFail(evaluate_phase(value, RUNTIME, require_forward=False))

    def test_missing_malformed_and_old_phases_fail_without_crashing(self):
        for value in (None, False, 4, "passed", [], {}, {"scenario": []}, {"scenario": {}}):
            with self.subTest(value=value):
                self.assertFail(evaluate_phase(value, RUNTIME))
                self.assertFail(evaluate_termination(value, value, RUNTIME))
                self.assertFail(evaluate_cycles(value, value, RUNTIME))
        for key in ("verification_schema", "scenario", "runtime_id", "verification_id", "checks", "passed", "evidence"):
            value = phase()
            value.pop(key)
            self.assertFail(evaluate_phase(value, RUNTIME))
        for schema in (None, 3, True, "4", 5):
            value = phase()
            value["verification_schema"] = schema
            self.assertFail(evaluate_phase(value, RUNTIME))
        for malformed in (None, [], "true", 1):
            value = phase()
            value["evidence"] = malformed
            self.assertFail(evaluate_phase(value, RUNTIME))

    def test_summary_true_does_not_create_wire_proof_and_false_summary_fails(self):
        for key, replacement in (("passed", False), ("passed", 1), ("checks", {}), ("checks", []),
                                 ("checks", {"automatic_default_interval": True})):
            value = phase()
            value[key] = replacement
            self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase()
        value["checks"]["automatic_deadline"] = False
        self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase()
        value["evidence"]["successful_requests"] = []
        self.assertFail(evaluate_phase(value, RUNTIME))

    def test_datadog_nested_marker_measurement_and_correlation_match(self):
        value = phase("termination")
        span = value["evidence"]["successful_requests"][0]["spans"][0]
        span["meta"] = {"runtime-id": RUNTIME, "verify": {"marker": "marker"}}
        span["metrics"] = {"_dd": {"measured": 1}}
        value["evidence"]["log_correlation"] = {"dd": {"trace_id": "1", "span_id": "1001"}}
        original = copy.deepcopy(value)
        self.assertPass(evaluate_termination(arm(), value, RUNTIME))
        self.assertEqual(value, original)
        for field, invalid in (("marker", "other"), ("marker", None),
                               ("measured", 0), ("measured", None), ("measured", "1"),
                               ("trace_id", "2"), ("span_id", "1002")):
            changed = copy.deepcopy(value)
            if field == "marker":
                changed["evidence"]["successful_requests"][0]["spans"][0]["meta"]["verify"]["marker"] = invalid
            elif field == "measured":
                changed["evidence"]["successful_requests"][0]["spans"][0]["metrics"]["_dd"]["measured"] = invalid
            else:
                changed["evidence"]["log_correlation"]["dd"][field] = invalid
            self.assertFail(evaluate_termination(arm(), changed, RUNTIME))

    def test_log_comparison_only_normalizes_dotted_key_structure(self):
        native = {"meta": {"verify.marker": "final", "unrelated": None},
                  "metrics": {"_dd.measured": 1}, "spans": [{"dd.trace_id": "1"}]}
        backend = {"meta": {"verify": {"marker": "final"}, "unrelated": None},
                   "metrics": {"_dd": {"measured": 1}}, "spans": [{"dd": {"trace_id": "1"}}]}
        self.assertTrue(log_values_equal(backend, native))
        original = copy.deepcopy(backend)
        for mutate in (
            lambda row: row["meta"].pop("unrelated"),
            lambda row: row["meta"]["verify"].update(marker="other"),
            lambda row: row["metrics"]["_dd"].update(measured=0),
            lambda row: row["spans"][0]["dd"].update(trace_id="2"),
            lambda row: row["meta"].update({"verify.marker": "final"}),
        ):
            changed = copy.deepcopy(backend)
            mutate(changed)
            self.assertFalse(log_values_equal(changed, native))
        self.assertEqual(backend, original)

    def test_native_root_parent_id_may_be_omitted_but_not_nonzero_or_null(self):
        for parent_id in (0, "0", "omitted"):
            value = phase()
            span = value["evidence"]["successful_requests"][0]["spans"][0]
            if parent_id == "omitted":
                span.pop("parent_id")
            else:
                span["parent_id"] = parent_id
            self.assertPass(evaluate_phase(value, RUNTIME))
        for parent_id in (None, False, 1, "1"):
            value = phase()
            value["evidence"]["successful_requests"][0]["spans"][0]["parent_id"] = parent_id
            self.assertFail(evaluate_phase(value, RUNTIME))

    def test_only_complete_successful_matching_wire_acknowledges(self):
        changes = {
            "status": (None, 503, 300, 199, "200", True),
            "outcome": ("received", "rejected", "upstream_error", "response_error", None),
            "completed_at": (None, 99, 100.5, 102.001, math.inf, math.nan, True),
            "received_at": (None, 99.99, 102.01, math.nan),
            "bytes": (0, -1, "150", True), "sequence": (0, None, True),
            "forwarded": (False, None, "true"), "spans": (None, [], [None], ["span"]),
        }
        for key, alternatives in changes.items():
            for alternative in alternatives:
                with self.subTest(field=key, value=alternative):
                    value = phase()
                    value["evidence"]["successful_requests"][0][key] = alternative
                    self.assertFail(evaluate_phase(value, RUNTIME))
        for malformed in (None, {}, "request", [None, []]):
            value = phase()
            value["evidence"]["successful_requests"] = malformed
            self.assertFail(evaluate_phase(value, RUNTIME))
        for key, alternative in (("trace_id", "99"), ("span_id", "99"), ("parent_id", "1"),
                                 ("name", "verify.delivery.termination"), ("meta", None), ("metrics", {}),
                                 ("service", "another-service")):
            value = phase()
            value["evidence"]["successful_requests"][0]["spans"][0][key] = alternative
            self.assertFail(evaluate_phase(value, RUNTIME))
        for key in ("runtime-id", "verify.marker"):
            value = phase()
            value["evidence"]["successful_requests"][0]["spans"][0]["meta"][key] = "stale"
            self.assertFail(evaluate_phase(value, RUNTIME))

    def test_common_identity_flush_error_and_timer_boundaries(self):
        changes = {
            "runtime_id_before": "old", "runtime_id_after": "other", "manual_flush_count_after": 3,
            "manual_flush_count_before": True, "unexpected_errors": ["writer died"],
            "forward_to_agent": "true", "started_at": -1, "elapsed_ms": 0,
            "deadline_ms": 10001, "flush_interval_ms": 2000,
            "marker": "", "operation": "other", "span": {"trace_id": 1, "span_id": "1001"},
        }
        for key, alternative in changes.items():
            with self.subTest(field=key):
                value = phase()
                value["evidence"][key] = alternative
                self.assertFail(evaluate_phase(value, RUNTIME))
        for key in ("started_at", "elapsed_ms", "deadline_ms"):
            for number in (None, math.inf, math.nan, True, "1000", 10 ** 1000):
                value = phase()
                value["evidence"][key] = number
                self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase()
        value["evidence"].update(elapsed_ms=10000)
        value["evidence"]["successful_requests"][0].update(received_at=110, completed_at=110)
        self.assertPass(evaluate_phase(value, RUNTIME))
        value["evidence"]["successful_requests"][0]["completed_at"] = 110.0001
        self.assertFail(evaluate_phase(value, RUNTIME))

    def test_recovery_requires_both_real_failures_and_bounded_fresh_roots(self):
        replacements = {
            "mode": "disconnect", "offered_count": 25, "offered_spans": [],
            "failure_attempts": [], "fresh_acknowledgements": [], "fresh_marker": "wrong",
            "operation": "verify.delivery.automatic", "runtime_id_after": "changed",
            "unexpected_errors": ["native worker stopped"], "deadline_ms": 20001,
            "elapsed_ms": 20001, "fault_active_at": 99, "restored_at": 105,
            "fresh_created_at": 102, "probes": [],
        }
        for key, replacement in replacements.items():
            with self.subTest(field=key):
                value = phase("recovery")
                value["evidence"]["cases"][0][key] = replacement
                self.assertFail(evaluate_phase(value, RUNTIME))
        for cases in (None, [], [None], [None, None], [None, None, None]):
            value = phase("recovery")
            value["evidence"]["cases"] = cases
            self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase("recovery")
        value["evidence"]["cases"].reverse()
        self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase("recovery")
        first, second = value["evidence"]["cases"]
        second["fault_active_at"] = 103
        self.assertFail(evaluate_phase(value, RUNTIME))
        for field in ("offered_spans", "fresh_span"):
            value = phase("recovery")
            case = value["evidence"]["cases"][0]
            if field == "offered_spans":
                case[field][1] = copy.deepcopy(case[field][0])
            else:
                case[field] = copy.deepcopy(case["offered_spans"][0])
                case["fresh_acknowledgements"][0]["spans"][0].update(case[field])
            self.assertFail(evaluate_phase(value, RUNTIME))

    def test_failure_receipt_must_be_injected_for_an_offered_native_root(self):
        for index in (0, 1):
            for key, replacement in (("outcome", "accepted"), ("status", 200), ("forwarded", True),
                                     ("received_at", 99), ("spans", []), ("bytes", 0)):
                value = phase("recovery")
                value["evidence"]["cases"][index]["failure_attempts"][0][key] = replacement
                self.assertFail(evaluate_phase(value, RUNTIME))
            value = phase("recovery")
            failure = value["evidence"]["cases"][index]["failure_attempts"][0]
            failure["spans"][0]["trace_id"] = "9999"
            self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase("recovery")
        value["evidence"]["cases"][0]["failure_attempts"][0]["completed_at"] = None
        self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase("recovery")
        value["evidence"]["cases"][1]["failure_attempts"][0]["completed_at"] = 106
        self.assertFail(evaluate_phase(value, RUNTIME))

    def test_health_probes_are_three_bounded_ordered_successes_during_fault(self):
        for key, replacement in (("status", 500), ("runtime_id", "old"), ("at", 102.1),
                                 ("elapsed_ms", 0), ("elapsed_ms", 20000), ("elapsed_ms", math.nan)):
            value = phase("recovery")
            value["evidence"]["cases"][0]["probes"][1][key] = replacement
            self.assertFail(evaluate_phase(value, RUNTIME))
        for probes in ([None] * 3, [{"status": 200}] * 3):
            value = phase("recovery")
            value["evidence"]["cases"][0]["probes"] = probes
            self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase("recovery")
        value["evidence"]["cases"][0]["probes"].reverse()
        self.assertFail(evaluate_phase(value, RUNTIME))

    def test_recovery_rejects_cross_case_reuse_and_stale_fresh_ack(self):
        value = phase("recovery")
        first, second = value["evidence"]["cases"]
        second["offered_spans"][1] = copy.deepcopy(first["offered_spans"][1])
        self.assertFail(evaluate_phase(value, RUNTIME))
        for key, replacement in (("received_at", 101.9), ("completed_at", 104.001),
                                 ("status", 503), ("outcome", "received"), ("forwarded", False)):
            value = phase("recovery")
            value["evidence"]["cases"][0]["fresh_acknowledgements"][0][key] = replacement
            self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase("recovery")
        case = value["evidence"]["cases"][0]
        case["offered_spans"][1]["span_id"] = case["offered_spans"][0]["span_id"]
        self.assertFail(evaluate_phase(value, RUNTIME))
        value = phase("recovery")
        case = value["evidence"]["cases"][0]
        case["fresh_span"]["span_id"] = case["offered_spans"][0]["span_id"]
        case["fresh_acknowledgements"][0]["spans"][0]["span_id"] = case["fresh_span"]["span_id"]
        self.assertFail(evaluate_phase(value, RUNTIME))

    def test_cycles_require_real_ordered_transitions_exact_counters_and_identity(self):
        for mutate in (
            lambda vm, samples: vm["cycles"].pop(),
            lambda vm, samples: samples.pop(),
            lambda vm, samples: vm["cycles"].reverse(),
            lambda vm, samples: vm["cycles"][1].update(cycle=1),
            lambda vm, samples: vm["cycles"][0]["suspended"].update(state="RUNNING"),
            lambda vm, samples: vm["cycles"][0]["resumed"].update(state="RESUMING"),
            lambda vm, samples: vm["cycles"][1]["suspended"].update(time=1014),
            lambda vm, samples: vm["cycles"][0]["resumed"].update(time=math.nan),
            lambda vm, samples: vm["cycles"][0]["resumed"].update(microvmId="other"),
            lambda vm, samples: samples[1].update(runtime_id="new"),
            lambda vm, samples: samples[1].update(verification_id="new"),
            lambda vm, samples: samples[1].update(verification_schema=3),
            lambda vm, samples: samples[1].update(cycle=1),
            lambda vm, samples: samples[1].update(checks={}),
            lambda vm, samples: samples[1].update(passed=False),
            lambda vm, samples: samples[1].update(features={}),
            lambda vm, samples: samples[1].update(feature_spans=[]),
            lambda vm, samples: samples[1]["lifecycle"].update(suspend=1),
            lambda vm, samples: samples[1]["lifecycle"].update(resume=1),
            lambda vm, samples: samples[1]["lifecycle"].update(run=True),
            lambda vm, samples: samples[2]["lifecycle"].update(terminate=1),
        ):
            vm, samples = cycle_evidence()
            mutate(vm, samples)
            self.assertFail(evaluate_cycles(vm, samples, RUNTIME))
        for malformed in (None, [], "cycle"):
            vm, samples = cycle_evidence()
            vm["cycles"][0] = malformed
            samples[0] = malformed
            self.assertFail(evaluate_cycles(vm, samples, RUNTIME))

    def test_termination_binds_armed_marker_final_ack_and_correlated_log(self):
        for key, replacement in (("marker", "other"), ("runtime_id", "old"), ("armed", False),
                                 ("verification_id", "other"), ("operation", DELIVERY_OPERATIONS[0]),
                                 ("verification_schema", 3)):
            armed = arm()
            armed[key] = replacement
            self.assertFail(evaluate_termination(armed, phase("termination"), RUNTIME))
        for key, replacement in (("marker", "other"), ("trace_id", "999"), ("span_id", "999"),
                                 ("runtime_id", "old"), ("verification_id", "other"), ("msg", "other")):
            value = phase("termination")
            value["evidence"]["marker_log"][key] = replacement
            self.assertFail(evaluate_termination(arm(), value, RUNTIME))
        for key, replacement in (("run", 0), ("suspend", 2), ("resume", 4), ("terminate", 0), ("terminate", 2)):
            value = phase("termination")
            value["lifecycle"][key] = replacement
            self.assertFail(evaluate_termination(arm(), value, RUNTIME))
        for key in ("marker", "successful_requests", "marker_log", "log_correlation"):
            value = phase("termination")
            value["evidence"].pop(key)
            self.assertFail(evaluate_termination(arm(), value, RUNTIME))
        value = phase("termination")
        value["evidence"]["log_correlation"]["dd.trace_id"] = "abcdef00000000000000000000000001"
        self.assertPass(evaluate_termination(arm(), value, RUNTIME))
        value["evidence"]["log_correlation"]["dd.trace_id"] = "abcdef00000000000000000000000002"
        self.assertFail(evaluate_termination(arm(), value, RUNTIME))
        self.assertFail(evaluate_termination(arm(), phase(), RUNTIME))
        value = phase("termination", forwarded=False)
        self.assertFail(evaluate_termination(arm(), value, RUNTIME))
        self.assertPass(evaluate_termination(arm(), value, RUNTIME, require_forward=False))
        value = phase("termination")
        value["marker"] = "wrong-final-marker"
        self.assertFail(evaluate_termination(arm(), value, RUNTIME))


if __name__ == "__main__":
    unittest.main()
