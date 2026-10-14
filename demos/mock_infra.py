"""Mock production: three services that share one database, and the tools agents use to look at it."""

import os
import time
from types import SimpleNamespace

from opentelemetry import trace
from strands import tool

from checks import check_handoff

SERVICES = ("payment-service", "auth-service", "order-service")
RESOURCES = ("db-prod-01", "stripe-gateway", "redis-sessions", "inventory-api")

DEPENDENCIES = {
    "payment-service": ["db-prod-01", "stripe-gateway"],
    "auth-service": ["db-prod-01", "redis-sessions"],
    "order-service": ["db-prod-01", "inventory-api"],
}

ALERTS = [
    {"id": "AL-1", "service": "payment-service", "description": "CPU > 90% for 5 minutes", "fired_at": "2026-10-07T14:30:00Z"},
    {"id": "AL-2", "service": "auth-service", "description": "p99 latency spike to 5s", "fired_at": "2026-10-07T14:30:15Z"},
    {"id": "AL-3", "service": "order-service", "description": "Error rate > 10%", "fired_at": "2026-10-07T14:30:22Z"},
]

LOGS = {
    "payment-service": [
        {"ts": "14:27:02Z", "level": "INFO", "source": "payment-service/pod-7f2c",
         "msg": "reconciliation-batch started: 350 workers, holding 1 DB connection per worker"},
        {"ts": "14:29:41Z", "level": "WARN", "source": "payment-service/pod-7f2c",
         "msg": "connection acquire timeout after 5000ms (pool=db-prod-01)"},
        {"ts": "14:30:05Z", "level": "ERROR", "source": "payment-service/pod-9a1d",
         "msg": "retry storm: 3,412 DB retries/min, worker threads 200/200 busy"},
    ],
    "auth-service": [
        {"ts": "14:30:11Z", "level": "WARN", "source": "auth-service/pod-3b8e",
         "msg": "session lookup p99=4.8s (baseline 40ms)"},
        {"ts": "14:30:14Z", "level": "ERROR", "source": "auth-service/pod-3b8e",
         "msg": "timeout acquiring connection from pool db-prod-01"},
    ],
    "order-service": [
        {"ts": "14:30:19Z", "level": "ERROR", "source": "order-service/pod-c41a",
         "msg": "HTTP 503 on POST /orders: could not obtain DB connection"},
        {"ts": "14:30:21Z", "level": "FATAL", "source": "order-service/pod-c41a",
         "msg": "db-prod-01: remaining connection slots are reserved for superuser"},
    ],
}

METRICS = {
    "payment-service": {
        "cpu": {"value": 94.2, "unit": "%", "threshold": 80, "trend": "rising since 14:27"},
        "error_rate": {"value": 3.1, "unit": "%", "threshold": 1.0, "trend": "rising since 14:29"},
    },
    "auth-service": {
        "latency_p99": {"value": 4800, "unit": "ms", "threshold": 500, "trend": "rising since 14:29"},
        "cpu": {"value": 21.0, "unit": "%", "threshold": 80, "trend": "flat"},
    },
    "order-service": {
        "error_rate": {"value": 12.4, "unit": "%", "threshold": 1.0, "trend": "rising since 14:30"},
        "cpu": {"value": 18.5, "unit": "%", "threshold": 80, "trend": "flat"},
    },
    "db-prod-01": {
        "connections": {
            "value": 498, "max": 500, "unit": "connections", "trend": "60 -> 498 since 14:27",
            "by_client": {
                "payment-service/reconciliation-batch": 352,
                "payment-service/api": 60,
                "auth-service": 46,
                "order-service": 40,
            },
        },
        "cpu": {"value": 41.0, "unit": "%", "threshold": 85, "trend": "flat"},
    },
}

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
CATEGORIES = {"cpu": "resource", "latency": "performance", "error": "availability"}
TOOL_LATENCY_S = 0.4 * float(os.environ.get("DEMO_PACE", "1"))


def _classify(service: str, description: str) -> dict:
    if service not in SERVICES:
        return {"error": f"unknown service {service!r}; known: {', '.join(SERVICES)}"}
    text = description.lower()
    category = next((c for k, c in CATEGORIES.items() if k in text), "unknown")
    return {"service": service, "severity": "P2", "category": category}


def _logs(service: str, level: str) -> list[dict] | str:
    if service not in LOGS:
        return f"no logs for {service!r}; services with logs: {', '.join(LOGS)}"
    level = level.upper()
    if level not in LEVELS:
        return f"unknown level {level!r}; use one of {', '.join(LEVELS)}"
    return [e for e in LOGS[service] if LEVELS.index(e["level"]) >= LEVELS.index(level)]


def _metrics(service: str, metric: str) -> dict | str:
    if service not in METRICS:
        return f"no metrics for {service!r}; known: {', '.join(METRICS)}"
    if not metric:
        return METRICS[service]
    if metric not in METRICS[service]:
        return f"{service} has no metric {metric!r}; available: {', '.join(METRICS[service])}"
    return {metric: METRICS[service][metric]}


def _returned_service(requested: str, entries: list[dict]) -> str:
    """Which service the data is actually about, read from the payload rather than the request."""
    sources = {e["source"].split("/")[0] for e in entries}
    if not sources:
        return requested
    return sources.pop() if len(sources) == 1 else "mixed"


def _effect(target: str, action: str) -> str:
    a = action.lower()
    if any(w in a for w in ("pause", "stop", "suspend", "halt", "disable", "throttle", "kill")) and (
        "reconciliation" in a or "batch" in a
    ):
        return "db-prod-01 connections 498 -> 131 within 60s; all three alerts clearing"
    if "scale" in a and target == "payment-service":
        return "payment-service 6 -> 12 pods; db-prod-01 connections 500/500, error rates rising on all services"
    return f"{target}: {action} completed; db-prod-01 connections still 498/500"


def outcome(executed: list[dict]) -> dict:
    effects = [_effect(e["target"], e["action"]) for e in executed]
    return {
        "remediations_executed": len(executed),
        "root_cause_active": not any("clearing" in fx for fx in effects),
        "made_worse": any("rising on all" in fx for fx in effects),
    }


def naive_tools(executed: list[dict]):
    """Tools for the demo-1 pipeline. `executed` collects every remediation that ran."""

    @tool
    def classify_alert(service: str, description: str) -> dict:
        """Classify an alert: severity (P1-P4) and category.

        Args:
            service: Service the alert fired on.
            description: Alert text.
        """
        time.sleep(TOOL_LATENCY_S)
        return _classify(service, description)

    @tool
    def search_logs(service: str, time_range_minutes: int = 5, level: str = "WARN") -> dict | str:
        """Search recent logs for a service.

        Args:
            service: Service name.
            time_range_minutes: How far back to search.
            level: Minimum log level (DEBUG, INFO, WARN, ERROR, FATAL).
        """
        time.sleep(TOOL_LATENCY_S)
        entries = _logs(service, level)
        return entries if isinstance(entries, str) else {"service": service, "entries": entries}

    @tool
    def check_metrics(service: str, metric: str = "") -> dict | str:
        """Get current metrics for a service. Leave metric empty to get all of them.

        Args:
            service: Service name.
            metric: Metric name, e.g. cpu, latency_p99, error_rate.
        """
        time.sleep(TOOL_LATENCY_S)
        return _metrics(service, metric)

    @tool
    def execute_remediation(service: str, action: str) -> dict:
        """Execute a remediation against production.

        Args:
            service: Target service.
            action: What to do, e.g. "restart pods".
        """
        time.sleep(TOOL_LATENCY_S)
        executed.append({"target": service, "action": action})
        return {"status": "EXECUTED", "service": service, "action": action, "effect": _effect(service, action)}

    return [classify_alert, search_logs, check_metrics, execute_remediation]


def infra_tools(board, author: str, *, chaos: bool = False, gate: bool = True) -> SimpleNamespace:
    """Board-aware versions of the tools. Every observation is recorded on the incident board as evidence."""

    @tool
    def classify_alert(service: str, description: str) -> dict:
        """Classify an alert: severity (P1-P4) and category.

        Args:
            service: Service the alert fired on.
            description: Alert text.
        """
        time.sleep(TOOL_LATENCY_S)
        return _classify(service, description)

    @tool
    def get_service_dependencies(services: list[str]) -> dict:
        """Look up the downstream dependencies (databases, caches, external APIs) of each service.

        Args:
            services: Service names.
        """
        time.sleep(TOOL_LATENCY_S)
        return {s: DEPENDENCIES.get(s, "unknown service") for s in services}

    @tool
    def search_logs(service: str, time_range_minutes: int = 5, level: str = "WARN") -> dict | str:
        """Search recent logs for a service. The result carries an evidence_id to cite in findings.

        Args:
            service: Service name.
            time_range_minutes: How far back to search.
            level: Minimum log level (DEBUG, INFO, WARN, ERROR, FATAL).
        """
        time.sleep(TOOL_LATENCY_S)
        # Chaos: a log-cache key collision serves payment-service's logs for auth-service.
        lookup = "payment-service" if chaos and service == "auth-service" else service
        entries = _logs(lookup, level)
        if isinstance(entries, str):
            return entries
        ev = board.record_evidence(author, "search_logs", service, _returned_service(service, entries), entries)
        _stamp_evidence(ev)
        return {"evidence_id": ev["id"], "service": service, "entries": entries}

    @tool
    def check_metrics(service: str, metric: str = "") -> dict | str:
        """Get current metrics for a service or resource (e.g. db-prod-01). Leave metric empty for all.

        The result carries an evidence_id to cite in findings.

        Args:
            service: Service or resource name.
            metric: Metric name, e.g. cpu, latency_p99, error_rate, connections.
        """
        time.sleep(TOOL_LATENCY_S)
        data = _metrics(service, metric)
        if isinstance(data, str):
            return data
        ev = board.record_evidence(author, "check_metrics", service, service, data)
        _stamp_evidence(ev)
        return {"evidence_id": ev["id"], "service": service, "metrics": data}

    @tool
    def execute_remediation(action_id: str) -> dict:
        """Execute a proposed action from the incident board against production.

        Args:
            action_id: Id of an action on the board, e.g. "A-1".
        """
        time.sleep(TOOL_LATENCY_S)
        action = board.actions.get(action_id)
        if action is None:
            return {"status": "ERROR", "error": f"no action {action_id!r} on the board; propose it first"}
        problems = []
        if not gate:
            status = "executed"
        elif action["risk"] == "high":
            status = "needs_approval"
        else:
            problems = check_handoff(board, action)
            status = "blocked" if problems else "executed"
        if not board.set_action_status(action_id, status):
            return {"status": "ERROR", "error": f"{action_id} is already {action['status']}"}
        span = trace.get_current_span()
        if status == "needs_approval":
            return {"status": "NEEDS_APPROVAL", "action_id": action_id, "reason": "high-risk action"}
        if status == "blocked":
            span.set_attribute("gate.blocked", True)
            span.set_attribute("gate.problems", problems)
            return {"status": "BLOCKED", "action_id": action_id, "problems": problems,
                    "next": "do not retry; escalate to the on-call human with these problems"}
        effect = _effect(action["target"], action["action"])
        span.set_attribute("remediation.effect", effect)
        return {"status": "EXECUTED", "action_id": action_id, "effect": effect}

    return SimpleNamespace(
        classify_alert=classify_alert,
        get_service_dependencies=get_service_dependencies,
        search_logs=search_logs,
        check_metrics=check_metrics,
        execute_remediation=execute_remediation,
    )


def _stamp_evidence(ev: dict) -> None:
    span = trace.get_current_span()
    span.set_attribute("evidence.id", ev["id"])
    span.set_attribute("evidence.requested", ev["requested"])
    span.set_attribute("evidence.returned", ev["returned"])

