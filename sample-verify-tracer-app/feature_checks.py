"""Assertions over exported spans from the post-restore feature scenarios."""

FEATURE_OPERATIONS = {
    "manual": "verify.feature.manual",
    "error": "verify.feature.error",
    "http": "verify.feature.http",
    "database": "verify.feature.database",
    "async": "verify.feature.async",
    "threads": "verify.feature.threads",
}

EXPECTED_ERROR = "microvm verification expected error"


def evaluate_features(spans, evidence, runtime_id):
    roots = evidence.get("roots", {})
    by_id = {(str(span["trace_id"]), str(span["span_id"])): span for span in spans}

    def exported(identity):
        return by_id.get((identity.get("trace_id"), identity.get("span_id")), {})

    def children(case, identities):
        root = roots.get(case, {})
        return len(identities) == 2 and all(
            identity.get("trace_id") == root.get("trace_id")
            and str(exported(identity).get("parent_id")) == root.get("span_id")
            for identity in identities
        )

    complete = set(FEATURE_OPERATIONS).issubset(roots) and all(exported(root) for root in roots.values())
    checks = {
        "feature_scenarios_completed": complete and not evidence.get("errors"),
        "feature_independent_traces": complete
        and len({root["trace_id"] for root in roots.values()}) == len(FEATURE_OPERATIONS),
        "feature_runtime_identity": complete
        and all(exported(roots[case]).get("meta", {}).get("runtime-id") == runtime_id for case in FEATURE_OPERATIONS),
        "feature_root_spans": complete
        and all(
            exported(roots[case]).get("name") == operation
            and exported(roots[case]).get("parent_id", 0) == 0
            and exported(roots[case]).get("service") == "sample-verify-tracer-app"
            for case, operation in FEATURE_OPERATIONS.items()
        ),
    }
    manual = evidence.get("manual", {})
    child = exported(manual.get("child", {}))
    root = exported(roots.get("manual", {}))
    checks["feature_manual_parenting"] = bool(child) and (
        child.get("trace_id") == root.get("trace_id") and child.get("parent_id") == root.get("span_id")
    )
    checks["feature_tags_and_metrics"] = (
        root.get("resource") == "manual.after_refresh"
        and child.get("meta", {}).get("verify.case") == "nested"
        and child.get("metrics", {}).get("verify.count") == 3
    )
    log = manual.get("log", {})
    expected = manual.get("expected_log", {})
    checks["feature_log_correlation"] = (
        bool(expected.get("trace_id"))
        and expected.get("trace_id") != "0"
        and expected.get("span_id") == roots.get("manual", {}).get("span_id")
        and log.get("dd.trace_id") == expected.get("trace_id")
        and log.get("dd.span_id") == expected.get("span_id")
        and log.get("dd.service") == "sample-verify-tracer-app"
    )
    error = exported(roots.get("error", {}))
    error_meta = error.get("meta", {})
    checks["feature_error_capture"] = (
        error.get("error") == 1
        and "ValueError" in error_meta.get("error.type", "")
        and error_meta.get("error.message") == EXPECTED_ERROR
        and bool(error_meta.get("error.stack"))
    )
    database_root = roots.get("database", {})
    checks["feature_sqlite_instrumentation"] = evidence.get("database", {}).get("answer") == 42 and any(
        str(span["trace_id"]) == database_root.get("trace_id")
        and str(span.get("parent_id")) == database_root.get("span_id")
        and span.get("type") == "sql"
        and span.get("meta", {}).get("db.system") == "sqlite"
        and span.get("resource") == "SELECT 42 AS answer"
        for span in spans
    )
    for case, label in (("async", "async"), ("threads", "thread")):
        data = evidence.get(case, {})
        tasks = data.get("tasks", [])
        checks[f"feature_{label}_parenting"] = children(case, tasks)
        checks[f"feature_{label}_context_isolation"] = (
            len(tasks) == 2
            and len({task.get("span_id") for task in tasks}) == 2
            and all(task.get("isolated") is True for task in tasks)
            and data.get("parent_restored") is True
        )
    http = evidence.get("http", {})
    http_root = roots.get("http", {})
    # A transport retry repeats IDs; only distinct spans indicate reinstrumentation.
    http_spans = [span for span in by_id.values() if str(span["trace_id"]) == http_root.get("trace_id")]
    clients = [span for span in http_spans if span.get("meta", {}).get("component") == "requests"]
    checks["feature_requests_instrumentation"] = len(clients) == 2 and all(
        str(span.get("parent_id")) == http_root.get("span_id") for span in clients
    )
    server = exported(http.get("server", {}))
    client_ids = {str(span["span_id"]) for span in clients}
    checks["feature_http_single_instrumentation"] = len(clients) == 2 and all(
        sum(
            span.get("meta", {}).get("component") == "flask"
            and str(span.get("parent_id")) == str(client["span_id"])
            for span in http_spans
        ) == 1
        for client in clients
    )
    checks["feature_flask_distributed_parenting"] = (
        bool(server)
        and str(server.get("trace_id")) == http_root.get("trace_id")
        and str(server.get("parent_id")) in client_ids
        and server.get("meta", {}).get("http.status_code") == "200"
        and server.get("meta", {}).get("runtime-id") == runtime_id
    )
    checks["feature_http_error_capture"] = http.get("error_status") == 500 and all(
        any(
            span.get("meta", {}).get("component") == component
            and span.get("meta", {}).get("http.status_code") == "500"
            and span.get("error") == 1
            for span in http_spans
        )
        for component in ("requests", "flask")
    )
    checks["feature_w3c_propagation"] = (
        bool(http.get("traceparent"))
        and http.get("w3c_trace_id") == http_root.get("full_trace_id")
        and http.get("w3c_parent_id") in client_ids
    )
    checks["feature_identity_stable_across_requests"] = http.get("runtime_id") == runtime_id
    return checks
