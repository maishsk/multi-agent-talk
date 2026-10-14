"""Slide 15 snippet, runnable: a human gate built on Strands interrupts. Not a stage demo."""

import argparse

from strands import Agent, tool
from strands.types.tools import ToolContext

from config import configure, model_for


@tool(context=True)
def execute_remediation(action: str, risk: str, tool_context: ToolContext) -> str:
    """Execute a remediation. High-risk actions wait for a human.

    Args:
        action: What to do.
        risk: low, medium or high.
    """
    if risk == "high":
        answer = tool_context.interrupt("approval", reason={"action": action, "risk": risk})
        if answer != "approve":
            return f"REJECTED by on-call: {action}"
    return f"EXECUTED: {action}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--answer", help="answer the approval prompt non-interactively")
    args = parser.parse_args()
    configure(offline=args.offline)

    agent = Agent(name="resolver", tools=[execute_remediation], model=model_for("specialist"), callback_handler=None)
    result = agent("db-prod-01 is out of connections. Raise max_connections to 800 (risk: high).")

    while result.stop_reason == "interrupt":
        responses = []
        for interrupt in result.interrupts:
            answer = args.answer or input(f"Approve {interrupt.reason}? [approve/reject] ")
            responses.append({"interruptResponse": {"interruptId": interrupt.id, "response": answer}})
        result = agent(responses)

    print(result)


if __name__ == "__main__":
    main()
