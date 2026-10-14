# Why your multi-agent system fails in production (live debugging)

Runnable demo code for a talk on multi-agent coordination failures. Three Strands
agents respond to a production incident, and the demos show how a shared incident
board, per-agent tool scoping, evidence minted by tools, and a deterministic gate
catch a silent data-poisoning bug before it reaches production.

The three runs are **reproduce the bug, fix the coordination, then catch a silent failure**.

| Path | What it is |
|---|---|
| `demos/` | The demo scripts, plus the agents, tools, board, checks and telemetry they share |
| `tests/` | Offline tests (no model calls) |
| `requirements.txt` | `strands-agents[otel]` 1.x and `pytest` |

## Setup

Requires Python 3.10+ and Bedrock access to Claude.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
aws sso login --profile <your-bedrock-profile>
export AWS_PROFILE=<your-bedrock-profile> AWS_REGION=us-west-2
cd demos && ../.venv/bin/python preflight.py
```

The models default to `global.anthropic.claude-sonnet-4-6` for the orchestrator and
`global.anthropic.claude-haiku-4-5-20251001-v1:0` for the specialists. You can override either one:

```bash
export ORCHESTRATOR_MODEL=...  SPECIALIST_MODEL=...
```

`preflight.py` checks your identity, lists the Claude inference profiles available in your account,
and sends one 5-token request to each model. It only makes read-only calls apart from those two
requests. Add `--jaeger` to also check that Jaeger is up.

## Run

All demos run from `demos/`:

```bash
../.venv/bin/python demo1_naive.py          # reproduce: pipeline, context bleed, 3 fixes for 1 cause
../.venv/bin/python demo2_orchestrated.py   # fix: orchestrator + fresh specialists + per-incident board
../.venv/bin/python demo3_debug.py          # debug: planted bug, trace tree, gate blocks, audit
../.venv/bin/python demo3_debug.py --no-gate --jaeger
../.venv/bin/python extra_human_gate.py     # human-in-the-loop snippet (Strands interrupts), not a stage demo
```

| Flag / variable | Effect |
|---|---|
| `--offline` or `DEMO_OFFLINE=1` | Every demo accepts this. A scripted model (`offline.py`) drives the same agents, tools, board, checks and telemetry. Use it for rehearsal, for tests, and as the on-stage fallback. |
| `DEMO_PACE=0` | Removes the simulated tool latency and scripted-model delays. Defaults to `1`. |
| `--no-gate` | `demo3_debug.py` only. Lets `execute_remediation` run without the handoff checks. |
| `--jaeger` | `demo3_debug.py` only. Also exports spans over OTLP to `localhost:4318`. Start Jaeger first with `finch run -d -p 16686:16686 -p 4318:4318 jaegertracing/jaeger` (or `docker run`), then open http://localhost:16686. |
| `--answer approve\|reject` | `extra_human_gate.py` only. Answers the approval prompt without asking. |

Run the tests from the repo root:

```bash
.venv/bin/python -m pytest tests -q
```

## Code map

All modules live in `demos/`.

| Module | Role |
|---|---|
| `demo1_naive.py` | Hand-wired triage → investigator → resolver pipeline that reuses the same agents for every alert. Shows context bleed, and three fixes for one cause. |
| `demo2_orchestrated.py` | A model-driven orchestrator with the specialists as tools, plus one board per incident. Also exports `build_response_team`, `print_board` and `run`, which demo 3 reuses. |
| `demo3_debug.py` | The demo 2 team with `chaos=True`. Prints the trace tree and the post-run audit. |
| `extra_human_gate.py` | High-risk remediation that pauses on a Strands interrupt until a human approves it. |
| `preflight.py` | Pre-talk check of credentials, inference profiles, the two models, and optionally Jaeger. |
| `mock_infra.py` | Three services sharing `db-prod-01`: alerts, logs, metrics, and the tools. `chaos=True` plants the log-cache collision. `execute_remediation` runs the gate. |
| `board.py` | `IncidentBoard`, one per incident, plus `board_tools(board, author)`. Writes are validated, errors come back as text, and the author is stamped by the tool. Status changes are atomic, so parallel tool calls can't execute an action twice. |
| `checks.py` | `check_handoff` (the invariants that run before any production change) and `audit` (the post-run report). |
| `trace_tree.py` | Collects the in-memory OTel spans and renders the trace tree. Optional OTLP export to Jaeger. |
| `stage.py` | Terminal narration through Strands hooks: who called what, and what came back. |
| `config.py` | Bedrock model selection, and the offline switch. |
| `offline.py` | The scripted model. |

## Safety

"Production" here is a Python dict. The demos create no AWS resources. The only AWS calls they make
are Bedrock model invocations, plus the read-only STS and inference-profile lookups in `preflight.py`.

## License

MIT. See [LICENSE](LICENSE).
