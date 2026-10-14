"""Terminal output for the stage: who called what, and what came back."""

import json
import sys
import textwrap

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def dim(t): return _c("2", t)
def bold(t): return _c("1", t)
def yellow(t): return _c("33", t)
def red(t): return _c("31;1", t)
def green(t): return _c("32", t)
def cyan(t): return _c("36", t)


def short(value, width: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def args_repr(tool_input: dict, width: int = 60) -> str:
    return short(", ".join(short(v, 40) for v in (tool_input or {}).values()), width)


def heading(text: str) -> None:
    print("\n" + bold(cyan(f"== {text} ")) + bold(cyan("=" * max(0, 76 - len(text)))))


def wrap(text: str, indent: str = "    ", width: int = 100) -> str:
    return "\n".join(textwrap.fill(line, width, initial_indent=indent, subsequent_indent=indent) for line in
                     str(text).strip().splitlines() if line.strip())


def _outcome_line(text: str) -> str:
    try:
        r = json.loads(text)
    except json.JSONDecodeError:
        return text
    detail = r.get("effect") or "; ".join(r.get("problems", [])) or r.get("reason") or r.get("error", "")
    return f"{r.get('status')}: {detail}"


QUIET_RESULTS = {"read_board", "search_logs", "check_metrics", "classify_alert", "get_service_dependencies"}


def narrate(agent, indent: str = "    ") -> None:
    """Print every tool call the agent makes, and the result of the calls that change something."""

    def before(event: BeforeToolCallEvent) -> None:
        use = event.tool_use
        print(dim(f"{indent}{agent.name} → ") + f"{use['name']}" + dim(f"({args_repr(use['input'])})"))

    def after(event: AfterToolCallEvent) -> None:
        name = event.tool_use["name"]
        text = "".join(c.get("text", "") for c in (event.result or {}).get("content", []))
        if name == "execute_remediation":
            text = _outcome_line(text)
        if event.result and event.result.get("status") == "error":
            print(red(f"{indent}  ✖ {name}: {short(text, 90)}"))
        elif "BLOCKED" in text or "NEEDS_APPROVAL" in text:
            print(red(f"{indent}  ⛔ {short(text, 110)}"))
        elif text.startswith("ERROR"):
            print(yellow(f"{indent}  ! {short(text, 110)}"))
        elif name == "execute_remediation":
            print(green(f"{indent}  ✓ {short(text, 110)}"))
        elif name not in QUIET_RESULTS and not hasattr(event.selected_tool, "agent"):
            print(dim(f"{indent}  ✓ {short(text, 90)}"))

    agent.add_hook(before, BeforeToolCallEvent)
    agent.add_hook(after, AfterToolCallEvent)


class StreamText:
    """Callback handler that streams the agent's own words and nothing else (tool calls come from narrate)."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self._fresh = True

    def __call__(self, **kwargs) -> None:
        data = kwargs.get("data", "")
        if data:
            if self._fresh:
                print(self.prefix, end="")
                self._fresh = False
            print(data.replace("\n", "\n" + self.prefix), end="", flush=True)
        if kwargs.get("event", {}).get("contentBlockStop") is not None and not self._fresh:
            print()
            self._fresh = True
