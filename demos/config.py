"""Model selection. Override with env vars; run any demo with --offline to use the scripted model instead of Bedrock."""

import os

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
ORCHESTRATOR_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "global.anthropic.claude-sonnet-4-6")
SPECIALIST_MODEL = os.environ.get("SPECIALIST_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")

_offline = os.environ.get("DEMO_OFFLINE") == "1"


def configure(*, offline: bool = False) -> None:
    global _offline
    _offline = _offline or offline


def is_offline() -> bool:
    return _offline


def model_for(role: str):
    if _offline:
        from offline import ScriptedModel

        return ScriptedModel(role)

    from botocore.config import Config
    from strands.models import BedrockModel

    return BedrockModel(
        model_id=ORCHESTRATOR_MODEL if role == "orchestrator" else SPECIALIST_MODEL,
        region_name=REGION,
        max_tokens=4096,
        boto_client_config=Config(read_timeout=120, retries={"max_attempts": 6, "mode": "adaptive"}),
    )


def banner() -> str:
    if _offline:
        return "model: scripted (offline)"
    return f"models: orchestrator={ORCHESTRATOR_MODEL}  specialists={SPECIALIST_MODEL}  region={REGION}"
