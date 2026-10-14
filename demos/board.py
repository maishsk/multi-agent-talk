"""Per-incident shared context. One board per incident, never a global."""

import itertools
import json
import threading
from types import SimpleNamespace

from strands import tool

from mock_infra import RESOURCES, SERVICES

SECTIONS = ("triage", "findings", "correlation", "actions", "evidence")
SEVERITIES = ("P1", "P2", "P3", "P4")
RISKS = ("low", "medium", "high")
PREFIX = {"triage": "T", "findings": "F", "correlation": "C", "actions": "A", "evidence": "ev"}


class IncidentBoard:
    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self.triage: dict[str, dict] = {}
        self.findings: dict[str, dict] = {}
        self.correlation: dict[str, dict] = {}
        self.actions: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.mutations: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()
        self._ids = {section: itertools.count(1) for section in SECTIONS}

    def _add(self, section: str, author: str, **fields) -> dict:
        with self._lock:
            entry_id = f"{PREFIX[section]}-{next(self._ids[section])}"
            entry = {"id": entry_id, "author": author, **fields}
            getattr(self, section)[entry_id] = entry
            self.mutations.append((author, section, entry_id))
            return entry

    def record_evidence(self, author: str, tool_name: str, requested: str, returned: str, data) -> dict:
        return self._add("evidence", author, tool=tool_name, requested=requested, returned=returned, data=data)

    def set_action_status(self, action_id: str, status: str) -> bool:
        """Move a proposed action to its final status. Atomic, so parallel tool calls can't both execute it."""
        with self._lock:
            if self.actions[action_id]["status"] != "proposed":
                return False
            self.actions[action_id]["status"] = status
            self.mutations.append(("gate", "actions", f"{action_id}:{status}"))
            return True

    def view(self, section: str = "") -> dict:
        sections = [section] if section else SECTIONS
        out = {"incident_id": self.incident_id}
        with self._lock:
            for name in sections:
                hidden = ("data",) if name == "evidence" else ()
                out[name] = {k: {f: v for f, v in e.items() if f not in hidden} for k, e in getattr(self, name).items()}
        return out

    def executed_actions(self) -> list[dict]:
        return [a for a in self.actions.values() if a["status"] == "executed"]


def _unknown(kind: str, value, allowed) -> str:
    return f"ERROR: unknown {kind} {value!r}. Allowed: {', '.join(map(str, allowed))}"


def board_tools(board: IncidentBoard, author: str) -> SimpleNamespace:
    """Board tools bound to one incident and one author. Validation errors come back as text, never raised."""

    @tool
    def read_board(section: str = "") -> str:
        """Read the incident board. Leave section empty to read everything.

        Args:
            section: One of triage, findings, correlation, actions, evidence.
        """
        if section and section not in SECTIONS:
            return _unknown("section", section, SECTIONS)
        return json.dumps(board.view(section), indent=1)

    @tool
    def post_triage(service: str, severity: str, category: str, summary: str) -> str:
        """Post the triage result for one alerting service.

        Args:
            service: Alerting service.
            severity: P1, P2, P3 or P4.
            category: e.g. resource, performance, availability.
            summary: One sentence.
        """
        if service not in SERVICES:
            return _unknown("service", service, SERVICES)
        if severity not in SEVERITIES:
            return _unknown("severity", severity, SEVERITIES)
        entry = board._add("triage", author, service=service, severity=severity, category=category, summary=summary)
        return f"{entry['id']} posted"

    @tool
    def post_finding(service: str, claim: str, confidence: float, evidence_ids: list[str]) -> str:
        """Post a finding about ONE service or resource, backed by evidence you gathered for it.

        Cross-service conclusions belong in post_correlation, not here.

        Args:
            service: The service or resource the claim is about.
            claim: What the evidence shows, in one or two sentences.
            confidence: 0.0 to 1.0.
            evidence_ids: evidence_id values returned by search_logs / check_metrics.
        """
        if service not in SERVICES + RESOURCES:
            return _unknown("service", service, SERVICES + RESOURCES)
        if not 0.0 <= confidence <= 1.0:
            return f"ERROR: confidence must be between 0 and 1, got {confidence}"
        if not evidence_ids:
            return "ERROR: a finding must cite at least one evidence_id"
        missing = [e for e in evidence_ids if e not in board.evidence]
        if missing:
            return f"ERROR: no such evidence {missing}. Gather it with search_logs or check_metrics first."
        entry = board._add("findings", author, service=service, claim=claim, confidence=confidence,
                           evidence_ids=list(evidence_ids))
        return f"{entry['id']} posted"

    @tool
    def post_correlation(services: list[str], common_cause: str, confidence: float, finding_ids: list[str]) -> str:
        """Post a conclusion that links several services to one cause.

        Args:
            services: Affected services.
            common_cause: The shared cause.
            confidence: 0.0 to 1.0.
            finding_ids: Findings that support it.
        """
        unknown = [s for s in services if s not in SERVICES + RESOURCES]
        if unknown:
            return _unknown("service", unknown, SERVICES + RESOURCES)
        if not 0.0 <= confidence <= 1.0:
            return f"ERROR: confidence must be between 0 and 1, got {confidence}"
        missing = [f for f in finding_ids if f not in board.findings]
        if missing or not finding_ids:
            return f"ERROR: cite existing finding ids; missing {missing}"
        entry = board._add("correlation", author, services=list(services), common_cause=common_cause,
                           confidence=confidence, finding_ids=list(finding_ids))
        return f"{entry['id']} posted"

    @tool
    def propose_action(action: str, target: str, risk: str, finding_ids: list[str]) -> str:
        """Propose a remediation. Its confidence is computed from the findings it cites.

        Args:
            action: What to do.
            target: Service or resource to act on.
            risk: low, medium or high.
            finding_ids: Findings that justify this action.
        """
        if risk not in RISKS:
            return _unknown("risk", risk, RISKS)
        if target not in SERVICES + RESOURCES:
            return _unknown("target", target, SERVICES + RESOURCES)
        missing = [f for f in finding_ids if f not in board.findings]
        if missing or not finding_ids:
            return f"ERROR: cite existing finding ids; missing {missing}"
        confidence = min(board.findings[f]["confidence"] for f in finding_ids)
        entry = board._add("actions", author, action=action, target=target, risk=risk,
                           finding_ids=list(finding_ids), confidence=confidence, status="proposed")
        return f"{entry['id']} proposed (confidence {confidence}, from cited findings)"

    return SimpleNamespace(
        read_board=read_board,
        post_triage=post_triage,
        post_finding=post_finding,
        post_correlation=post_correlation,
        propose_action=propose_action,
    )
