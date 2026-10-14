"""Live fix: a model-driven orchestrator, fresh specialists per call, and a per-incident board."""

import argparse
import time

from strands import Agent

from board import IncidentBoard, board_tools
from config import banner, configure, model_for
from mock_infra import ALERTS, infra_tools, outcome
from stage import StreamText, dim, green, heading, narrate, red, short

ORCHESTRATOR_PROMPT = """You coordinate an incident response team: triage, investigator, resolver.
They share an incident board; read it with read_board.

Before dispatching anyone, call get_service_dependencies for the alerting services.
If they share a dependency, treat the alerts as ONE incident and investigate them together in a single investigator call.
Order: triage, then investigator, then resolver. Read the board after each specialist returns.
Never call the same specialist twice at the same time.
If the resolver reports BLOCKED or NEEDS_APPROVAL, stop and escalate to a human. Do not retry.
Before each call, say in one sentence why. Finish with a three-line summary: status, root cause, action."""

TRIAGE_PROMPT = """You triage the alerts of one incident.
For each alert: classify_alert, then post_triage. Reply with one line per alert."""

INVESTIGATOR_PROMPT = """You investigate one incident. Start by reading the triage section of the board.
Gather evidence with search_logs and check_metrics, for the services and for their shared dependencies.
Post one finding per service or resource with post_finding, citing the evidence_id values you got back.
Confidence is how directly that evidence supports the claim.
If several services share a cause, post_correlation. Reply with a two-line summary."""

RESOLVER_PROMPT = """You resolve one incident. Read the board first.
Propose remediations for the root cause with propose_action, citing the findings that justify each.
Execute the lowest-risk action that addresses the root cause with execute_remediation.
If it returns BLOCKED or NEEDS_APPROVAL, do not retry: report the problems verbatim. Reply in two lines."""

ALERT_PROMPT = "Incident {incident_id}. Alerts:\n" + "\n".join(
    f"- {a['id']} {a['service']}: {a['description']} (fired {a['fired_at']})" for a in ALERTS
) + "\nCoordinate the response."


def build_response_team(board: IncidentBoard, *, chaos: bool = False, gate: bool = True) -> Agent:
    scope = {"incident.id": board.incident_id}

    def tools(author):
        return board_tools(board, author), infra_tools(board, author, chaos=chaos, gate=gate)

    b, i = tools("triage")
    triage = Agent(
        name="triage",
        description="Classifies every alert of the incident and posts triage entries to the board.",
        system_prompt=TRIAGE_PROMPT,
        tools=[i.classify_alert, b.read_board, b.post_triage],
        model=model_for("specialist"), trace_attributes=scope, callback_handler=None,
    )
    b, i = tools("investigator")
    investigator = Agent(
        name="investigator",
        description="Gathers logs and metrics for the incident's services and posts evidence-backed findings.",
        system_prompt=INVESTIGATOR_PROMPT,
        tools=[b.read_board, i.search_logs, i.check_metrics, i.get_service_dependencies,
               b.post_finding, b.post_correlation],
        model=model_for("specialist"), trace_attributes=scope, callback_handler=None,
    )
    b, i = tools("resolver")
    resolver = Agent(
        name="resolver",
        description="Proposes remediations from the board's findings and executes the safest one.",
        system_prompt=RESOLVER_PROMPT,
        tools=[b.read_board, b.propose_action, i.execute_remediation],
        model=model_for("specialist"), trace_attributes=scope, callback_handler=None,
    )

    b, i = tools("orchestrator")
    orchestrator = Agent(
        name="orchestrator",
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=[i.get_service_dependencies, b.read_board, triage, investigator, resolver],
        model=model_for("orchestrator"),
        trace_attributes=scope,
        callback_handler=StreamText(prefix="  "),
    )
    for agent in (triage, investigator, resolver):
        narrate(agent, indent="      ")
    narrate(orchestrator, indent="  ")
    return orchestrator


def print_board(board: IncidentBoard) -> None:
    heading(f"BOARD {board.incident_id}")
    for t in board.triage.values():
        print(f"    {t['id']:<4} triage   {t['service']:<16} {t['severity']} {t['category']}")
    for f in board.findings.values():
        print(f"    {f['id']:<4} finding  {f['service']:<16} {f['confidence']:.2f}  {short(f['claim'], 70)}"
              + dim(f"  {f['evidence_ids']}"))
    for c in board.correlation.values():
        print(f"    {c['id']:<4} cause    {short(c['common_cause'], 90)}")
    for a in board.actions.values():
        status = {"executed": green, "blocked": red}.get(a["status"], dim)(a["status"])
        print(f"    {a['id']:<4} action   {a['action']} -> {a['target']}  risk={a['risk']} "
              f"conf={a['confidence']:.2f}  {status}")


def run(board: IncidentBoard, orchestrator: Agent) -> float:
    heading(f"INCIDENT {board.incident_id}")
    t0 = time.time()
    orchestrator(ALERT_PROMPT.format(incident_id=board.incident_id))
    return time.time() - t0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="use the scripted model instead of Bedrock")
    configure(offline=parser.parse_args().offline)
    print(dim(banner()))

    board = IncidentBoard("INC-1007")
    elapsed = run(board, build_response_team(board))
    print_board(board)

    heading("OUTCOME")
    result = outcome(board.executed_actions())
    print(f"    {result['remediations_executed']} production change(s) · "
          + (red("root cause still active") if result["root_cause_active"] else green("root cause cleared"))
          + dim(f" · {len(board.findings)} findings, {len(board.evidence)} evidence items · {elapsed:.1f}s"))


if __name__ == "__main__":
    main()
