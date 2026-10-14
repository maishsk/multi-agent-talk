"""Live debug #2: every agent succeeded.

Same team as demo 2, with one planted production bug: a log-cache key collision makes
search_logs("auth-service") return payment-service's logs. Nothing errors. Find it.
"""

import argparse

from board import IncidentBoard
from checks import audit
from config import banner, configure
from demo2_orchestrated import build_response_team, print_board, run
from stage import dim, heading, red, yellow
from trace_tree import SpanCollector, render, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="use the scripted model instead of Bedrock")
    parser.add_argument("--no-gate", action="store_true", help="let execute_remediation run without handoff checks")
    parser.add_argument("--jaeger", action="store_true", help="also export spans to Jaeger on localhost:4318")
    args = parser.parse_args()
    configure(offline=args.offline)
    print(dim(banner()))

    collector = SpanCollector(jaeger=args.jaeger)
    board = IncidentBoard("INC-1008")
    run(board, build_response_team(board, chaos=True, gate=not args.no_gate))
    print_board(board)

    collector.flush()
    spans = collector.spans()
    heading("TRACE")
    print(render(spans))
    print(dim(f"\n    {summary(spans)}"))

    heading("AUDIT")
    for line in audit(board):
        colour = red if "contaminated" in line or "problem" in line else (lambda s: s)
        print(colour(f"    {line}"))
    if collector.jaeger:
        print(yellow("\n    Jaeger: http://localhost:16686  (service: strands-agents)"))


if __name__ == "__main__":
    main()
