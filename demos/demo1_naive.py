"""Live debug #1: reproduce it.

A hand-wired pipeline: triage -> investigator -> resolver, the same three agents reused for every alert.
Three alerts, one real cause. Watch what comes out, then look inside the investigator.
"""

import argparse
import time

from strands import Agent

from config import banner, configure, model_for
from mock_infra import ALERTS, SERVICES, naive_tools, outcome
from stage import bold, dim, heading, narrate, red, short, wrap, yellow

TRIAGE_PROMPT = "You are an SRE triage agent. Classify the alert with classify_alert, then state severity and category in one sentence."
INVESTIGATOR_PROMPT = (
    "You are an SRE investigator. Diagnose the alert for the given service using search_logs and check_metrics. "
    "Be brief. End with exactly one line: 'Recommended remediation: <action>.' for that service."
)
RESOLVER_PROMPT = "You are an SRE resolver. Execute the recommended remediation with execute_remediation, then confirm in one sentence."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="use the scripted model instead of Bedrock")
    configure(offline=parser.parse_args().offline)
    print(dim(banner()))

    executed: list[dict] = []
    classify_alert, search_logs, check_metrics, execute_remediation = naive_tools(executed)

    triage = Agent(name="triage", system_prompt=TRIAGE_PROMPT, tools=[classify_alert],
                   model=model_for("specialist"), callback_handler=None)
    investigator = Agent(name="investigator", system_prompt=INVESTIGATOR_PROMPT, tools=[search_logs, check_metrics],
                         model=model_for("specialist"), callback_handler=None)
    resolver = Agent(name="resolver", system_prompt=RESOLVER_PROMPT, tools=[execute_remediation],
                     model=model_for("specialist"), callback_handler=None)
    for agent in (triage, investigator, resolver):
        narrate(agent)

    diagnoses, task_starts = {}, {}
    t0 = time.time()
    for alert in ALERTS:
        service = alert["service"]
        heading(f"{alert['id']}  {service}: {alert['description']}")
        print(yellow(f"    picked up {time.time() - t0:.1f}s after the first alert"))

        t = triage(f"Alert {alert['id']} on {service}: {alert['description']} (fired {alert['fired_at']})")
        task_starts[service] = len(investigator.messages)
        d = investigator(f"Investigate {service}. Triage said: {t}")
        diagnoses[service] = str(d)
        print(wrap(str(d)))
        resolver(f"Remediate {service}. Investigation:\n{d}")

    heading("DEBUG: what did the investigator see?")
    last = ALERTS[-1]["service"]
    before = investigator.messages[: task_starts[last]]
    carried = [s for s in SERVICES if s != last and any(s in str(m) for m in before)]
    print(f"    investigator.messages: {len(investigator.messages)} messages")
    print(f"    when {last} was investigated, {len(before)} of them were already there, about: "
          + bold(", ".join(carried) or "nothing"))
    leaked = [s for s in SERVICES if s != last and s in diagnoses[last]]
    if leaked:
        print(red(f"    the {last} diagnosis cites {', '.join(leaked)}: ") + short(diagnoses[last], 200))

    heading("OUTCOME")
    for e in executed:
        print(f"    executed: {e['action']}  ({e['target']})")
    result = outcome(executed)
    print(f"    {result['remediations_executed']} production changes for one incident")
    print(red("    root cause still active") if result["root_cause_active"] else "    root cause cleared")
    if result["made_worse"]:
        print(red("    and one of the fixes made it worse"))
    looked = any(
        block.get("toolUse", {}).get("input", {}).get("service") == "db-prod-01"
        for m in investigator.messages for block in m["content"]
    )
    who = "the investigator did check db-prod-01, one alert at a time" if looked else "nobody looked at db-prod-01"
    print(dim(f"    {who} (the shared dependency) · elapsed {time.time() - t0:.1f}s"))


if __name__ == "__main__":
    main()
