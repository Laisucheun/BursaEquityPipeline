"""Generate one day's brief with the Anthropic API (web search + web fetch).

The same instructions file drives the scheduled Claude Code task, so both
generators produce the same brief. Needs Anthropic credentials (an
``ANTHROPIC_API_KEY`` or an ``ant auth login`` profile).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from bursa.briefs.schema import BriefContent, NewsBrief

INSTRUCTIONS = Path(__file__).with_name("instructions.md")
MAX_CONTINUATIONS = 5
# A real brief took 17 searches and 12 fetches; headroom without letting a
# run wander.
MAX_SEARCHES = 20
MAX_FETCHES = 15


class BriefGenerationError(RuntimeError):
    pass


@dataclass
class BriefUsage:
    """Summed over every request (continuations included)."""

    requests: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0

    def add(self, usage) -> None:  # type: ignore[no-untyped-def]
        self.requests += 1
        self.input_tokens += usage.input_tokens
        self.cache_write_tokens += usage.cache_creation_input_tokens or 0
        self.cache_read_tokens += usage.cache_read_input_tokens or 0
        self.output_tokens += usage.output_tokens
        if usage.server_tool_use:
            self.web_searches += usage.server_tool_use.web_search_requests
            self.web_fetches += usage.server_tool_use.web_fetch_requests


def generate_brief(
    day: date, *, model: str, effort: str = "medium", api_key: str | None = None,
    usage: BriefUsage | None = None,
) -> NewsBrief:
    import anthropic  # deferred: only this command needs the SDK

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    # Haiku takes only the basic web tools and has no server-side refusal fallback.
    haiku = "haiku" in model
    tools = [
        {"type": "web_search_20250305" if haiku else "web_search_20260209",
         "name": "web_search", "max_uses": MAX_SEARCHES,
         "user_location": {"type": "approximate", "country": "MY", "city": "Kuala Lumpur",
                           "timezone": "Asia/Kuala_Lumpur"}},
        {"type": "web_fetch_20250910" if haiku else "web_fetch_20260209",
         "name": "web_fetch", "max_uses": MAX_FETCHES},
    ]
    fallback = {} if haiku else {"betas": ["server-side-fallback-2026-07-01"],
                                 "fallbacks": "default"}
    messages: list[dict] = [{
        "role": "user",
        "content": f"Write the Bursa Morning Brief for {day:%A, %d %B %Y} (Malaysia time). "
                   "Research it now with web search.",
    }]

    for _ in range(MAX_CONTINUATIONS + 1):
        with client.beta.messages.stream(
            model=model,
            max_tokens=64000,
            system=INSTRUCTIONS.read_text(encoding="utf-8"),
            messages=messages,
            tools=tools,
            output_config={"effort": effort},
            output_format=BriefContent,
            # Each search step re-reads the growing context; cache it.
            cache_control={"type": "ephemeral"},
            **fallback,
        ) as stream:
            message = stream.get_final_message()
        if usage is not None:
            usage.add(message.usage)

        if message.stop_reason == "pause_turn":
            # Long server-tool turn: send it back and the server resumes it.
            messages.append({"role": "assistant", "content": message.content})
            continue
        if message.stop_reason == "refusal":
            raise BriefGenerationError(f"model declined: {message.stop_details}")
        if message.stop_reason == "max_tokens":
            raise BriefGenerationError("brief was cut off at max_tokens")
        content = message.parsed_output
        if content is None:
            raise BriefGenerationError(f"no structured brief returned ({message.stop_reason})")
        return NewsBrief(
            **content.model_dump(), date=day,
            generated_by=f"api:{message.model}", generated_at=datetime.now().astimezone(),
        )
    raise BriefGenerationError(f"turn still paused after {MAX_CONTINUATIONS} continuations")
