"""Pure checks shared by the guest, evaluator, and local lifecycle controls."""

SCHEMA = 3
TELEMETRY_CHECKS = frozenset(
    {
        "20956_worker_replaced",
        "20956_worker_identity",
        "20956_old_exporter_unsubscribed",
        "20956_new_exporter_subscribed",
        "20956_new_telemetry_received",
    }
)
SUBSCRIPTION_CHECKS = frozenset(
    {
        "20956_retired_callback_removed",
        "20956_pre_run_callback_removed",
        "20956_current_callback_once",
        "20956_exporter_requests_received",
        "20956_exporter_responses_received",
    }
)


def required_telemetry_checks(schema):
    return TELEMETRY_CHECKS | SUBSCRIPTION_CHECKS if schema >= SCHEMA else TELEMETRY_CHECKS


def subscriber_count(telemetry_writer, writer):
    # Resolve each weak method once, keeping its owner alive during comparison.
    callbacks = [reference() for reference in list(telemetry_writer._worker_subscribers)]
    return sum(callback == writer._on_telemetry_worker_changed for callback in callbacks if callback is not None)


def exporter_metrics(payloads, runtime_id):
    """Count native exporter observations, including telemetry message batches."""
    totals = {"trace_api.requests": 0, "trace_api.responses": 0}

    def visit(message):
        kind = message.get("request_type")
        payload = message.get("payload", {})
        if kind == "message-batch" and isinstance(payload, list):
            for child in payload:
                visit(child)
        elif kind == "generate-metrics" and isinstance(payload, dict):
            for series in payload.get("series", []):
                name = series.get("metric")
                if (
                    series.get("namespace") == "tracers"
                    and name in totals
                    and "src_library:libdatadog" in series.get("tags", [])
                ):
                    totals[name] += sum(point[1] for point in series.get("points", []))

    for message in payloads:
        if message.get("runtime_id") == runtime_id:
            visit(message)
    return totals
