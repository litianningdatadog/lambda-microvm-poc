"""Incomplete or forged success cannot satisfy the cleanup-error controls."""

import copy
import unittest

from retirement_checks import MICROVM_CASES, REGULAR_CASES, evaluate_retirements


def retirement_evidence(microvm=True):
    return {name: dict.fromkeys(fields, True) for name, fields in (MICROVM_CASES if microvm else REGULAR_CASES).items()}


class RetirementCheckTest(unittest.TestCase):
    def test_complete_evidence_passes(self):
        for microvm in (False, True):
            self.assertTrue(all(evaluate_retirements(retirement_evidence(microvm), microvm).values()))

    def test_missing_cases_fail_even_with_passed_flag(self):
        for name in MICROVM_CASES:
            evidence = retirement_evidence()
            del evidence[name]
            evidence["passed"] = True
            self.assertFalse(evaluate_retirements(evidence)["21429_" + name])

    def test_every_required_field_must_be_true(self):
        for microvm, cases in ((True, MICROVM_CASES), (False, REGULAR_CASES)):
            for name, fields in cases.items():
                for field in fields:
                    for invalid in (None, False, 1, "true"):
                        with self.subTest(microvm=microvm, name=name, field=field, invalid=invalid):
                            evidence = retirement_evidence(microvm)
                            evidence[name][field] = invalid
                            self.assertFalse(all(evaluate_retirements(evidence, microvm).values()))
                    evidence = retirement_evidence(microvm)
                    del evidence[name][field]
                    self.assertFalse(all(evaluate_retirements(evidence, microvm).values()))

    def test_invalid_evidence_and_wrong_mode_fail(self):
        for evidence in (None, [], True, {}, retirement_evidence(False)):
            self.assertFalse(all(evaluate_retirements(evidence).values()))
        evidence = copy.deepcopy(retirement_evidence())
        evidence["configure_stop"] = True
        self.assertFalse(all(evaluate_retirements(evidence).values()))


if __name__ == "__main__":
    unittest.main()
