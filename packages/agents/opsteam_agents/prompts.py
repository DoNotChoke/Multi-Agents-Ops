"""Agent prompts as markdown files with a small front-matter: version, agent, tools, output."""

from dataclasses import dataclass
from pathlib import Path

from opsteam_core.errors import OpsteamError

PROMPTS = Path(__file__).parent / "prompts"
_KEYS = {"prompt_version", "agent", "allowed_tools", "output_schema"}


class PromptError(OpsteamError):
    code = "agent.prompt_invalid"


@dataclass(frozen=True)
class Prompt:
    prompt_version: str
    agent: str
    allowed_tools: frozenset[str]
    output_schema: str
    body: str


def parse_prompt(text: str) -> Prompt:
    """Read `---`-fenced `key: value` lines (lists as `[a, b]`) followed by the prompt body."""
    head, sep, body = text.removeprefix("---\n").partition("\n---\n")
    if not text.startswith("---\n") or not sep:
        raise PromptError("prompt has no front-matter")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if not colon or key.strip() not in _KEYS:
            raise PromptError(f"unexpected front-matter line {line!r}")
        fields[key.strip()] = value.strip()
    if missing := _KEYS - fields.keys():
        raise PromptError(f"front-matter misses {sorted(missing)}")
    tools = fields["allowed_tools"].strip("[]")
    return Prompt(
        prompt_version=fields["prompt_version"],
        agent=fields["agent"],
        allowed_tools=frozenset(t.strip() for t in tools.split(",") if t.strip()),
        output_schema=fields["output_schema"],
        body=body.strip(),
    )


def load_prompt(agent: str) -> Prompt:
    """The prompt shipped in `prompts/<agent>.md`."""
    prompt = parse_prompt((PROMPTS / f"{agent}.md").read_text(encoding="utf-8"))
    if prompt.agent != agent:
        raise PromptError(f"{agent}.md declares agent {prompt.agent!r}")
    return prompt
