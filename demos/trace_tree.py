"""Collect Strands OpenTelemetry spans in memory and render them as a tree small enough for a slide."""

import json
import socket

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands.telemetry import StrandsTelemetry

from stage import args_repr, dim, red, yellow

HIDDEN = ("execute_event_loop_cycle", "chat")


class SpanCollector:
    """Must be created before any Agent: the tracer binds to the global provider when agents are built."""

    def __init__(self, jaeger: bool = False):
        self.exporter = InMemorySpanExporter()
        self.telemetry = StrandsTelemetry()
        self.telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.jaeger = jaeger and _listening("localhost", 4318)
        if jaeger and not self.jaeger:
            print(yellow("Jaeger is not listening on localhost:4318; continuing without it"))
        if self.jaeger:
            self.telemetry.setup_otlp_exporter(endpoint="http://localhost:4318/v1/traces")

    def spans(self):
        return list(self.exporter.get_finished_spans())

    def flush(self):
        self.telemetry.tracer_provider.force_flush()


def _listening(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


def _tool_input(span) -> dict:
    raw = span.attributes.get("gen_ai.tool.call.arguments")
    if raw is None:
        raw = next((e.attributes.get("content") for e in span.events if e.name == "gen_ai.tool.message"), None)
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"input": raw}


def _secs(span) -> str:
    return f"{(span.end_time - span.start_time) / 1e9:.1f}s"


def _tokens(span) -> str:
    total = span.attributes.get("gen_ai.usage.total_tokens")
    return f"{total:,} tok" if total else ""


def _flags(span) -> list[str]:
    a = span.attributes
    flags = []
    if "evidence.returned" in a and a["evidence.returned"] != a["evidence.requested"]:
        flags.append(yellow(f"⚠ {a['evidence.id']}: asked for {a['evidence.requested']}, got {a['evidence.returned']}"))
    if a.get("gate.blocked"):
        flags.append(red("⛔ gate blocked"))
    if a.get("gen_ai.tool.status") == "error" or not span.status.is_ok:
        flags.append(red("✖ error"))
    return flags


def render(spans) -> str:
    by_id = {s.context.span_id: s for s in spans}
    children: dict = {}
    for s in spans:
        parent = s.parent.span_id if s.parent and s.parent.span_id in by_id else None
        children.setdefault(parent, []).append(s)

    def visible_children(span_id):
        out = []
        for c in sorted(children.get(span_id, []), key=lambda s: s.start_time):
            if c.name in HIDDEN:
                out += visible_children(c.context.span_id)
            else:
                out.append(c)
        return out

    lines = []

    def walk(span, prefix: str, last: bool, root: bool = False):
        kids = visible_children(span.context.span_id)
        name, attrs = span.name, span.attributes
        if name.startswith("execute_tool "):
            tool_name = name.removeprefix("execute_tool ")
            agent_kid = next((k for k in kids if k.name == f"invoke_agent {tool_name}"), None)
            if agent_kid is not None:
                label = f"{tool_name} ▸ agent  {dim(_secs(agent_kid))}  {dim(_tokens(agent_kid))}"
                kids = visible_children(agent_kid.context.span_id)
            else:
                label = f"{tool_name}" + dim(f"({args_repr(_tool_input(span), 44)})  {_secs(span)}")
        else:
            label = f"{name.removeprefix('invoke_agent ')} ▸ agent  {dim(_secs(span))}  {dim(_tokens(span))}"
        flags = _flags(span)
        branch = "" if root else ("└─ " if last else "├─ ")
        lines.append(prefix + branch + label + ("  " + "  ".join(flags) if flags else ""))
        if attrs.get("gate.blocked"):
            pad = prefix + ("" if root else ("   " if last else "│  ")) + "   "
            lines.extend(red(f"{pad}· {p}") for p in attrs.get("gate.problems", ()))
        child_prefix = prefix + ("" if root else ("   " if last else "│  "))
        for i, k in enumerate(kids):
            walk(k, child_prefix, i == len(kids) - 1)

    for root in visible_children(None):
        walk(root, "", True, root=True)
    return "\n".join(lines)


def summary(spans) -> str:
    tools = [s for s in spans if s.name.startswith("execute_tool ")]
    agents = [s for s in spans if s.name.startswith("invoke_agent ")]
    errors = [s for s in spans if not s.status.is_ok or s.attributes.get("gen_ai.tool.status") == "error"]
    return (f"{len(agents)} agent invocations · {len(tools)} tool calls · "
            f"{len(errors)} spans with error status")
