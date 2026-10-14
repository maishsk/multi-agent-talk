"""Scripted stand-in for Bedrock. It drives the real agents, tools, board, checks and telemetry.

Each "brain" looks at which tools the agent was given and what has happened since the current task
arrived, then returns the next turn: some text and, optionally, tool calls. Used for rehearsal, tests,
and as the on-stage fallback when the network dies.
"""

import asyncio
import json
import os
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from strands.models import Model

from mock_infra import ALERTS, SERVICES


@dataclass
class Call:
    name: str
    input: dict
    result: Any
    status: str


@dataclass
class Turn:
    text: str
    tools: list[tuple[str, dict]] = field(default_factory=list)


def _parse(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _context(messages) -> tuple[str, list[Call], list]:
    """The current task, the tool calls made for it so far, and everything that came before it."""
    start = max(
        i for i, m in enumerate(messages)
        if m["role"] == "user" and any("text" in c for c in m["content"])
    )
    task = " ".join(c["text"] for c in messages[start]["content"] if "text" in c)
    pending, calls = {}, []
    for m in messages[start + 1:]:
        for c in m["content"]:
            if "toolUse" in c:
                pending[c["toolUse"]["toolUseId"]] = c["toolUse"]
            elif "toolResult" in c:
                r = c["toolResult"]
                use = pending.get(r["toolUseId"], {"name": "?", "input": {}})
                text = "".join(x.get("text", "") for x in r.get("content", []))
                calls.append(Call(use["name"], use["input"], _parse(text), r.get("status", "success")))
    return task, calls, messages[:start]


def _service_in(text: str) -> str:
    hits = [(text.find(s), s) for s in SERVICES if s in text]
    return min(hits)[1] if hits else SERVICES[0]


def _ids(calls: list[Call], name: str) -> list[str]:
    hits = (re.match(r"[A-Za-z]+-\d+ ", c.result) for c in calls if c.name == name and isinstance(c.result, str))
    return [m.group().strip() for m in hits if m]


def _alert_for(service: str) -> dict:
    return next(a for a in ALERTS if a["service"] == service)


# ---- demo 1: naive pipeline -------------------------------------------------------------------

LOCAL_FIX = {
    "payment-service": ("payment-service is CPU-saturated (94.2%) with a DB retry storm and 200/200 worker threads "
                        "busy. It needs more capacity.", "scale out payment-service from 6 to 12 pods"),
    "auth-service": ("auth-service session lookups are timing out waiting on its DB connection pool "
                     "(p99 4.8s vs 40ms baseline). The pool looks wedged.", "restart auth-service pods to reset the pool"),
    "order-service": ("order-service is failing 12.4% of POST /orders with 503s.", "roll back order-service to the previous release"),
}


def naive_triage(task, calls, history) -> Turn:
    service = _service_in(task)
    if not calls:
        alert = _alert_for(service)
        return Turn("Classifying the alert.", [("classify_alert", {"service": service, "description": alert["description"]})])
    r = calls[-1].result
    return Turn(f"{service}: {r['severity']} {r['category']} incident. Handing off to investigation.")


def naive_investigator(task, calls, history) -> Turn:
    service = _service_in(task)
    if not calls:
        return Turn(f"Checking {service} logs.", [("search_logs", {"service": service, "time_range_minutes": 5})])
    if len(calls) == 1:
        return Turn("Now the metrics.", [("check_metrics", {"service": service})])
    diagnosis, fix = LOCAL_FIX[service]
    earlier = [s for s in SERVICES if s != service and any(s in json.dumps(m) for m in history)]
    if service == "order-service" and "payment-service" in earlier:
        diagnosis += (" This matches the capacity pattern I saw on payment-service a moment ago (CPU 94.2%), "
                      "so the latest order-service release is probably heavier.")
    return Turn(f"{diagnosis}\nRecommended remediation: {fix}.")


def naive_resolver(task, calls, history) -> Turn:
    service = _service_in(task)
    if not calls:
        m = re.search(r"Recommended remediation: (.+?)\.?$", task, re.M)
        action = m.group(1) if m else f"restart {service}"
        return Turn("Executing the recommended remediation.", [("execute_remediation", {"service": service, "action": action})])
    r = calls[-1].result
    return Turn(f"Done: {r['action']} on {r['service']}. {r['effect']}")


# ---- demos 2 and 3: orchestrator + board ------------------------------------------------------

def orchestrator(task, calls, history) -> Turn:
    done = [c.name for c in calls]
    services = [a["service"] for a in ALERTS]
    if "get_service_dependencies" not in done:
        return Turn("Three alerts inside 22 seconds on three different services. Before I dispatch anyone, "
                    "I want to know whether they share anything.",
                    [("get_service_dependencies", {"services": services})])
    if "triage" not in done:
        return Turn("All three depend on db-prod-01. I'll treat this as ONE incident: one triage pass, then one "
                    "investigation across all three services instead of three separate ones.",
                    [("triage", {"input": "Triage all three alerts in the task below and post one triage entry per "
                                          "service.\n\n" + task})])
    if "investigator" not in done:
        return Turn("Triage is on the board. Sending a single investigator across all three services, "
                    "with the shared database as the lead.",
                    [("investigator", {"input": "Investigate payment-service, auth-service and order-service as one "
                                                "incident. All three depend on db-prod-01. Post one finding per "
                                                "service or resource, then a correlation if they share a cause."})])
    if "read_board" not in done:
        return Turn("Checking what the investigator concluded.", [("read_board", {"section": "correlation"})])
    if "resolver" not in done:
        return Turn("One correlated cause on the board. Handing it to the resolver.",
                    [("resolver", {"input": "Propose remediations for the correlated cause on the board and execute "
                                            "the lowest-risk one."})])
    result = str(calls[-1].result)
    if "BLOCKED" in result:
        return Turn("ESCALATED: the resolver's action was BLOCKED by the handoff checks, the evidence on the board "
                    "is not trustworthy.\nNo production change was made.\nPaging the on-call engineer with the "
                    "board and the gate's problem list.")
    return Turn("RESOLVED: one incident, not three. Root cause: payment-service reconciliation-batch exhausted the "
                "db-prod-01 connection pool.\nAction: paused reconciliation-batch (low risk); connections 498 -> 131.\n"
                "Follow-up: cap batch workers below pool size before re-enabling.")


def triage(task, calls, history) -> Turn:
    if not calls:
        return Turn("Classifying all three alerts.",
                    [("classify_alert", {"service": a["service"], "description": a["description"]}) for a in ALERTS])
    if not any(c.name == "post_triage" for c in calls):
        posts = []
        for c in calls:
            if c.name == "classify_alert" and isinstance(c.result, dict) and "severity" in c.result:
                a = _alert_for(c.result["service"])
                posts.append(("post_triage", {"service": a["service"], "severity": c.result["severity"],
                                              "category": c.result["category"], "summary": a["description"]}))
        return Turn("Posting triage.", posts)
    return Turn(f"Triaged 3 alerts: {', '.join(_ids(calls, 'post_triage'))}. All P2; they fired within 22s.")


def investigator(task, calls, history) -> Turn:
    done = [c.name for c in calls]
    if "read_board" not in done:
        return Turn("Reading triage.", [("read_board", {"section": "triage"})])
    if "search_logs" not in done:
        return Turn("Pulling logs for all three services and the shared database's connection metrics.",
                    [("search_logs", {"service": s}) for s in SERVICES]
                    + [("check_metrics", {"service": "db-prod-01", "metric": "connections"})])
    if "post_finding" not in done:
        posts = []
        for c in calls:
            if c.name == "search_logs" and isinstance(c.result, dict):
                last = c.result["entries"][-1]["msg"]
                posts.append(("post_finding", {"service": c.result["service"], "claim": f"logs show: {last}",
                                               "confidence": 0.85, "evidence_ids": [c.result["evidence_id"]]}))
            if c.name == "check_metrics" and isinstance(c.result, dict):
                posts.append(("post_finding", {
                    "service": "db-prod-01",
                    "claim": "498/500 connections; payment-service/reconciliation-batch holds 352 of them, "
                             "up from 60 total before 14:27",
                    "confidence": 0.92, "evidence_ids": [c.result["evidence_id"]]}))
        return Turn("Posting one finding per service, plus the database.", posts)
    if "post_correlation" not in done:
        return Turn("All three failures trace to the same pool.",
                    [("post_correlation", {
                        "services": list(SERVICES) + ["db-prod-01"],
                        "common_cause": "payment-service reconciliation-batch (started 14:27) is exhausting the "
                                        "db-prod-01 connection pool; auth and order are starved of connections",
                        "confidence": 0.9, "finding_ids": _ids(calls, "post_finding")})])
    return Turn(f"Findings {', '.join(_ids(calls, 'post_finding'))} and correlation "
                f"{', '.join(_ids(calls, 'post_correlation'))} posted. Common cause: reconciliation-batch "
                "exhausting db-prod-01.")


def resolver(task, calls, history) -> Turn:
    done = [c.name for c in calls]
    if "read_board" not in done:
        return Turn("Reading the board.", [("read_board", {})])
    if "propose_action" not in done:
        findings = next(c.result for c in calls if c.name == "read_board")["findings"]
        db = [f for f, v in findings.items() if v["service"] == "db-prod-01"] or list(findings)
        return Turn("Two options: stop the batch job, or give the database more connections.",
                    [("propose_action", {"action": "pause reconciliation-batch job", "target": "payment-service",
                                         "risk": "low", "finding_ids": list(findings)}),
                     ("propose_action", {"action": "raise max_connections on db-prod-01 to 800",
                                         "target": "db-prod-01", "risk": "high", "finding_ids": db})])
    if "execute_remediation" not in done:
        first = _ids(calls, "propose_action")[0]
        return Turn(f"Executing the low-risk option, {first}.", [("execute_remediation", {"action_id": first})])
    r = calls[-1].result
    if r.get("status") == "BLOCKED":
        return Turn(f"{r['action_id']} was BLOCKED by the handoff checks: {'; '.join(r['problems'])}. "
                    "Not retrying. This needs a human.")
    return Turn(f"{r['action_id']} {r['status']}: {r.get('effect', '')}")


def human_gate(task, calls, history) -> Turn:
    if not calls:
        return Turn("This needs a database config change.",
                    [("execute_remediation", {"action": "raise max_connections on db-prod-01 to 800", "risk": "high"})])
    return Turn(str(calls[-1].result))


def _brain(tool_specs: list[dict]):
    tool_names = {spec["name"] for spec in tool_specs}
    params = {spec["name"]: spec["inputSchema"]["json"].get("properties", {}) for spec in tool_specs}
    if "risk" in params.get("execute_remediation", {}):
        return human_gate
    if {"triage", "investigator", "resolver"} <= tool_names:
        return orchestrator
    if "post_triage" in tool_names:
        return triage
    if "post_finding" in tool_names:
        return investigator
    if "propose_action" in tool_names:
        return resolver
    if "execute_remediation" in tool_names:
        return naive_resolver
    if "search_logs" in tool_names:
        return naive_investigator
    return naive_triage


class ScriptedModel(Model):
    def __init__(self, role: str = "specialist", pace: float = float(os.environ.get("DEMO_PACE", "1"))):
        self.config = {"model_id": f"scripted-{role}"}
        self.pace = pace

    def update_config(self, **model_config):
        self.config.update(model_config)

    def get_config(self):
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("structured output is not scripted")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        task, calls, history = _context(messages)
        turn = _brain(tool_specs or [])(task, calls, history)

        await asyncio.sleep(self.pace * random.uniform(0.4, 0.9))
        yield {"messageStart": {"role": "assistant"}}
        if turn.text:
            yield {"contentBlockStart": {"start": {}}}
            words = turn.text.split(" ")
            for i in range(0, len(words), 4):
                chunk = " ".join(words[i:i + 4]) + (" " if i + 4 < len(words) else "")
                yield {"contentBlockDelta": {"delta": {"text": chunk}}}
                await asyncio.sleep(self.pace * 0.03)
            yield {"contentBlockStop": {}}
        for name, tool_input in turn.tools:
            tool_id = f"tooluse_{uuid.uuid4().hex[:12]}"
            yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": tool_id}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use" if turn.tools else "end_turn"}}
        prompt_tokens = len(json.dumps(messages)) // 4 + len(system_prompt or "") // 4
        out_tokens = len(turn.text) // 4 + 40 * len(turn.tools)
        yield {"metadata": {"usage": {"inputTokens": prompt_tokens, "outputTokens": out_tokens,
                                      "totalTokens": prompt_tokens + out_tokens},
                            "metrics": {"latencyMs": 0}}}
