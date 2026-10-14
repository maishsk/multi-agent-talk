"""Pre-talk check. Read-only: identity, inference-profile lookup, and one tiny model call per model."""

import argparse
import sys
import time
import urllib.request

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from config import ORCHESTRATOR_MODEL, REGION, SPECIALIST_MODEL
from stage import green, red, yellow


def ok(msg): print(green(f"  ✓ {msg}"))
def warn(msg): print(yellow(f"  ! {msg}"))
def fail(msg): print(red(f"  ✖ {msg}"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jaeger", action="store_true", help="also check that Jaeger is up on localhost:16686")
    args = parser.parse_args()
    session = boto3.Session(region_name=REGION)
    failures = 0

    print(f"region {REGION} · profile {session.profile_name or '(default chain)'}")
    try:
        arn = session.client("sts").get_caller_identity()["Arn"]
        ok(f"identity {arn}")
        if "admin" in arn.lower():
            warn("admin role: the demos only need bedrock:InvokeModel*; a least-privilege role is safer")
    except (BotoCoreError, ClientError) as e:
        fail(f"no valid credentials: {e}")
        return 1

    try:
        pager = session.client("bedrock").get_paginator("list_inference_profiles")
        profiles = {p["inferenceProfileId"] for page in pager.paginate() for p in page["inferenceProfileSummaries"]}
        for model in (ORCHESTRATOR_MODEL, SPECIALIST_MODEL):
            if model in profiles:
                ok(f"inference profile {model}")
            else:
                warn(f"{model} is not an inference profile here (fine if it is a base model id)")
        claude = sorted(p for p in profiles if "anthropic" in p and ("sonnet" in p or "haiku" in p))
        print("    available Claude Sonnet/Haiku profiles:\n      " + "\n      ".join(claude[-12:]))
    except (BotoCoreError, ClientError) as e:
        warn(f"could not list inference profiles: {e}")

    runtime = session.client("bedrock-runtime")
    for role, model in (("orchestrator", ORCHESTRATOR_MODEL), ("specialist", SPECIALIST_MODEL)):
        try:
            t0 = time.time()
            runtime.converse(modelId=model, messages=[{"role": "user", "content": [{"text": "Say OK."}]}],
                             inferenceConfig={"maxTokens": 5})
            ok(f"{role} {model} answered in {time.time() - t0:.1f}s")
        except (BotoCoreError, ClientError) as e:
            failures += 1
            fail(f"{role} {model}: {e}")

    if args.jaeger:
        try:
            urllib.request.urlopen("http://localhost:16686", timeout=2)
            ok("Jaeger UI on http://localhost:16686")
        except OSError:
            failures += 1
            fail("Jaeger is not running: finch run -d -p 16686:16686 -p 4318:4318 jaegertracing/jaeger")

    print(green("ready") if not failures else red(f"{failures} problem(s); use --offline as the fallback"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
