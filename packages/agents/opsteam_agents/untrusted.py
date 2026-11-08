"""Wrap data observed in customer systems so the model reads it as data, never as instructions."""

import re
from html import escape

MAX_CHARS = 8000
_CLOSING = re.compile(r"</\s*untrusted_data", re.IGNORECASE)
# Control characters except tab and newline: terminal escapes, NULs and the like.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def wrap_untrusted(
    source: str,
    content: str,
    *,
    tool_call_id: str | None = None,
    truncated: bool = False,
    max_chars: int = MAX_CHARS,
) -> str:
    """`content` inside an <untrusted_data> block it cannot close, cut to `max_chars`."""
    body = _CONTROL.sub("", content)
    if len(body) > max_chars:
        body, truncated = body[:max_chars], True
    body = _CLOSING.sub("&lt;/untrusted_data", body)
    attrs = f'source="{escape(source)}"'
    if tool_call_id is not None:
        attrs += f' tool_call_id="{escape(tool_call_id)}"'
    attrs += f' truncated="{str(truncated).lower()}"'
    return f"<untrusted_data {attrs}>\n{body}\n</untrusted_data>"
