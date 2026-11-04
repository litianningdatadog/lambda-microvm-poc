"""Required evidence for the #21429 cleanup-error regression controls."""

CONFIGURE_FIELDS = frozenset({
    "failure_injected", "configuration_completed", "replacement_published",
    "span_processors_rebuilt", "context_provider_updated", "user_processor_applied",
    "warning_logged", "retirement_state_correct", "fresh_trace_received", "cleanup_completed",
})
SHUTDOWN_FIELDS = frozenset({
    "retirements_created", "all_writers_attempted", "all_failures_logged",
    "active_error_preserved", "tracer_state_correct", "failed_retirements_retained", "retry_completed",
})
MICROVM_CASES = {
    "configure_flush_queue": CONFIGURE_FIELDS,
    "configure_stop": CONFIGURE_FIELDS,
    "shutdown_retired_failures": SHUTDOWN_FIELDS,
    "shutdown_active_failure": SHUTDOWN_FIELDS,
    "replacement_build_failure": frozenset({"error_preserved", "configuration_unchanged", "cleanup_completed"}),
}
REGULAR_CASES = {
    "configure_flush_queue": frozenset({"failure_injected", "error_preserved", "configuration_unchanged", "cleanup_completed"}),
    "configure_stop": frozenset({"failure_injected", "error_preserved", "configuration_unchanged", "cleanup_completed"}),
    "shutdown_active_failure": frozenset({"error_preserved", "tracer_state_correct", "retry_completed"}),
}


def evaluate_retirements(evidence, microvm=True):
    """Fail closed for missing phases, missing fields, and non-boolean results."""
    cases = MICROVM_CASES if microvm else REGULAR_CASES
    evidence = evidence if isinstance(evidence, dict) else {}
    return {
        "21429_" + name: isinstance(evidence.get(name), dict)
        and all(evidence[name].get(field) is True for field in fields)
        for name, fields in cases.items()
    }
