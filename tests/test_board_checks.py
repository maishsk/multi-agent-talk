import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

DEMOS = Path(__file__).resolve().parent.parent / "demos"
sys.path.insert(0, str(DEMOS))
os.environ["DEMO_PACE"] = "0"

import mock_infra  # noqa: E402
from board import IncidentBoard, board_tools  # noqa: E402
from checks import audit, check_handoff  # noqa: E402
from mock_infra import infra_tools, outcome  # noqa: E402


@pytest.fixture
def board():
    return IncidentBoard("INC-TEST")


def investigate(board, chaos=False, confidence=0.9):
    b, i = board_tools(board, "investigator"), infra_tools(board, "investigator", chaos=chaos)
    ev_auth = i.search_logs(service="auth-service")["evidence_id"]
    ev_db = i.check_metrics(service="db-prod-01", metric="connections")["evidence_id"]
    b.post_finding(service="auth-service", claim="pool timeouts", confidence=confidence, evidence_ids=[ev_auth])
    b.post_finding(service="db-prod-01", claim="pool exhausted", confidence=0.95, evidence_ids=[ev_db])
    r = board_tools(board, "resolver")
    r.propose_action(action="pause reconciliation-batch job", target="payment-service", risk="low",
                     finding_ids=["F-1", "F-2"])
    return infra_tools(board, "resolver", chaos=chaos)


def test_validation_errors_are_returned_not_raised(board):
    b = board_tools(board, "triage")
    assert b.read_board(section="secrets").startswith("ERROR")
    assert b.post_triage(service="auth-service", severity="SEV1", category="x", summary="y").startswith("ERROR")
    assert b.post_triage(service="billing", severity="P2", category="x", summary="y").startswith("ERROR")
    assert b.post_finding(service="auth-service", claim="x", confidence=0.9, evidence_ids=[]).startswith("ERROR")
    assert b.post_finding(service="auth-service", claim="x", confidence=0.9, evidence_ids=["ev-9"]).startswith("ERROR")
    assert b.post_finding(service="auth-service", claim="x", confidence=1.5, evidence_ids=["ev-1"]).startswith("ERROR")
    assert b.propose_action(action="x", target="auth-service", risk="yolo", finding_ids=["F-1"]).startswith("ERROR")
    assert not board.findings and not board.actions


def test_author_is_stamped_by_the_tool(board):
    board_tools(board, "triage").post_triage(service="auth-service", severity="P2", category="perf", summary="slow")
    assert board.triage["T-1"]["author"] == "triage"


def test_boards_are_isolated():
    a, b = IncidentBoard("INC-A"), IncidentBoard("INC-B")
    board_tools(a, "triage").post_triage(service="auth-service", severity="P2", category="perf", summary="slow")
    assert a.triage and not b.triage


def test_action_confidence_is_computed_from_cited_findings(board):
    investigate(board, confidence=0.75)
    assert board.actions["A-1"]["confidence"] == 0.75


def test_evidence_ledger_records_what_came_back(board):
    i = infra_tools(board, "investigator", chaos=True)
    i.search_logs(service="auth-service")
    i.search_logs(service="order-service")
    assert board.evidence["ev-1"]["requested"] == "auth-service"
    assert board.evidence["ev-1"]["returned"] == "payment-service"
    assert board.evidence["ev-2"]["returned"] == "order-service"


def test_empty_log_result_is_not_contamination(board):
    infra_tools(board, "investigator").search_logs(service="auth-service", level="FATAL")
    assert board.evidence["ev-1"]["returned"] == "auth-service"


def test_clean_board_passes_and_executes(board):
    i = investigate(board)
    assert check_handoff(board, board.actions["A-1"]) == []
    r = i.execute_remediation(action_id="A-1")
    assert r["status"] == "EXECUTED"
    assert outcome(board.executed_actions())["root_cause_active"] is False


def test_contaminated_evidence_blocks_execution(board):
    i = investigate(board, chaos=True)
    r = i.execute_remediation(action_id="A-1")
    assert r["status"] == "BLOCKED"
    assert "ev-1: asked for auth-service data, got payment-service" in r["problems"]
    assert board.actions["A-1"]["status"] == "blocked"
    assert any("contaminated evidence" in line for line in audit(board))


def test_low_confidence_blocks_execution(board):
    i = investigate(board, confidence=0.5)
    r = i.execute_remediation(action_id="A-1")
    assert r["status"] == "BLOCKED"
    assert any("confidence 0.5 < 0.7" in p for p in r["problems"])


def test_high_risk_needs_approval(board):
    investigate(board)
    board_tools(board, "resolver").propose_action(action="raise max_connections", target="db-prod-01",
                                                  risk="high", finding_ids=["F-2"])
    r = infra_tools(board, "resolver").execute_remediation(action_id="A-2")
    assert r["status"] == "NEEDS_APPROVAL"


def test_no_gate_executes_contaminated_action(board):
    investigate(board, chaos=True)
    r = infra_tools(board, "resolver", chaos=True, gate=False).execute_remediation(action_id="A-1")
    assert r["status"] == "EXECUTED"


def test_action_cannot_run_twice(board):
    i = investigate(board)
    i.execute_remediation(action_id="A-1")
    assert i.execute_remediation(action_id="A-1")["status"] == "ERROR"


def test_concurrent_calls_execute_an_action_once(board, monkeypatch):
    # Strands runs a turn's tool calls in parallel threads; widen the check-then-act window.
    slow_check = mock_infra.check_handoff

    def check_handoff_slowly(*args):
        time.sleep(0.2)
        return slow_check(*args)

    monkeypatch.setattr(mock_infra, "check_handoff", check_handoff_slowly)
    i = investigate(board)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: i.execute_remediation(action_id="A-1"), range(2)))
    assert sorted(r["status"] for r in results) == ["ERROR", "EXECUTED"]


def test_correlation_confidence_is_validated(board):
    investigate(board)
    r = board_tools(board, "investigator").post_correlation(services=["auth-service"], common_cause="x",
                                                            confidence=7, finding_ids=["F-1"])
    assert r.startswith("ERROR") and not board.correlation


def test_board_view_is_a_snapshot(board):
    b = board_tools(board, "triage")
    b.post_triage(service="auth-service", severity="P2", category="perf", summary="slow")
    view = board.view("triage")
    b.post_triage(service="order-service", severity="P2", category="avail", summary="503s")
    assert list(view["triage"]) == ["T-1"]


def run_demo(*args):
    env = {**os.environ, "DEMO_PACE": "0"}
    return subprocess.run([sys.executable, *args, "--offline"], cwd=DEMOS, env=env,
                          capture_output=True, text=True, timeout=120)


def test_demo1_offline():
    p = run_demo("demo1_naive.py")
    assert p.returncode == 0, p.stderr
    assert "3 production changes for one incident" in p.stdout
    assert "root cause still active" in p.stdout
    assert "payment-service, auth-service" in p.stdout


def test_demo2_offline():
    p = run_demo("demo2_orchestrated.py")
    assert p.returncode == 0, p.stderr
    assert "root cause cleared" in p.stdout
    assert "1 production change(s)" in p.stdout


def test_demo3_offline():
    p = run_demo("demo3_debug.py")
    assert p.returncode == 0, p.stderr
    assert re.search(r"⚠ ev-\d+: asked for auth-service, got payment-service", p.stdout)
    assert "⛔ gate blocked" in p.stdout
    assert "0 spans with error status" in p.stdout
    assert "built on contaminated evidence" in p.stdout


def test_demo3_offline_without_gate():
    p = run_demo("demo3_debug.py", "--no-gate")
    assert p.returncode == 0, p.stderr
    assert "gate blocked" not in p.stdout
    assert "built on contaminated evidence" in p.stdout


def test_human_gate_offline():
    p = run_demo("extra_human_gate.py", "--answer", "reject")
    assert p.returncode == 0, p.stderr
    assert "REJECTED" in p.stdout
