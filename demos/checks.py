"""Handoff invariants: deterministic checks that run before any agent is allowed to touch production."""

MIN_CONFIDENCE = 0.7


def check_handoff(board, action) -> list[str]:
    problems = []
    for ev in board.evidence.values():
        if ev["returned"] != ev["requested"]:
            problems.append(f"{ev['id']}: asked for {ev['requested']} data, got {ev['returned']}")
    for f in (board.findings[fid] for fid in action["finding_ids"]):
        if f["confidence"] < MIN_CONFIDENCE:
            problems.append(f"{f['id']}: confidence {f['confidence']} < {MIN_CONFIDENCE}")
        if not f["evidence_ids"]:
            problems.append(f"{f['id']}: no evidence cited")
    return problems


def audit(board) -> list[str]:
    """Post-run report: every invariant, across the whole board, not just the executed action."""
    lines = []
    for action in board.actions.values():
        problems = check_handoff(board, action)
        verdict = "OK" if not problems else f"{len(problems)} problem(s)"
        lines.append(f"{action['id']} [{action['status']}] {action['action']} -> {action['target']}: {verdict}")
        lines += [f"    - {p}" for p in problems]
    tainted = {ev["id"] for ev in board.evidence.values() if ev["returned"] != ev["requested"]}
    for f in board.findings.values():
        bad = tainted.intersection(f["evidence_ids"])
        if bad:
            lines.append(f"{f['id']} ({f['service']}) is built on contaminated evidence {sorted(bad)}: {f['claim']!r}")
    for c in board.correlation.values():
        if any(tainted.intersection(board.findings[fid]["evidence_ids"]) for fid in c["finding_ids"]):
            lines.append(f"{c['id']} correlation depends on a contaminated finding")
    if not board.actions:
        lines.append("no actions proposed")
    return lines
