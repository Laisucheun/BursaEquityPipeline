"""Use Claude, grounded in real web search, to propose company IR homepages.

This exists because guessing is a demonstrated failure mode: during this
project's own recon, two URLs recalled from plain model memory were tried
live, and one was simply wrong. Every call here forces a real
``web_search_20260209`` tool round-trip rather than answering from training
data alone, and every result still carries a confidence label and reasoning
for a human (or a follow-up automated check) to weigh before it's trusted -
this module never writes to the database itself. See
``bursa scrape discover-ir-urls`` in the CLI, which reports results to a file
and requires a separate ``--commit`` pass to actually call
``bursa company set-ir-url``.

Real money note: each call makes up to a handful of live web searches
against Anthropic's server-side tool. This is deliberately not run as part of
any test or automated pipeline stage - it's an interactively-triggered,
human-reviewed bootstrapping tool.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from bursa.config import get_settings

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
_FINAL_LINE = re.compile(r"FINAL_ANSWER:\s*(.+)", re.IGNORECASE)

Confidence = Literal["high", "medium", "low", "none"]


class CompanyCandidate(BaseModel):
    stock_code: str
    name: str
    sector: str | None = None


class IrUrlCandidate(BaseModel):
    stock_code: str
    company_name: str
    ir_homepage_url: str | None = Field(
        default=None, description="Best candidate URL, or None if not found"
    )
    confidence: Confidence = "none"
    reasoning: str = ""


def get_client() -> Any:
    """An Anthropic client. Raises with a clear message if nothing can auth."""
    import anthropic

    settings = get_settings()
    if settings.anthropic_api_key:
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # A bare constructor also picks up an `ant auth login` profile if one
    # exists; if neither is configured, the first real call raises
    # anthropic.AuthenticationError, which callers should let surface plainly
    # rather than swallow - there is nothing useful to fall back to.
    return anthropic.Anthropic()


def _web_search_tool(max_uses: int) -> dict:
    return {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}


def _response_text(response: Any) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def _check_refusal(response: Any) -> None:
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError(f"model declined the request: {response.stop_details}")


# --------------------------------------------------------------------------
# Company discovery
# --------------------------------------------------------------------------

_DISCOVER_SYSTEM = """\
You research Bursa Malaysia (Malaysia's stock exchange) listed companies.
Use web search - verify each stock code and company name against a real
source (Bursa Malaysia's own site, a reputable financial data provider, or a
news article naming both), rather than answering from memory alone. A wrong
stock code is worse than a shorter list.

Prefer well-established, large-cap Main Market companies with an easily
findable corporate website, since the goal is a working seed list for
further automated lookup, not maximum coverage.

End your answer with exactly one fenced code block containing a JSON array
and nothing else after it, in this exact shape:
```json
[{"stock_code": "5347", "name": "Tenaga Nasional Berhad", "sector": "Utilities"}, ...]
```
"""


def discover_watchlist_companies(
    count: int = 20,
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
) -> list[CompanyCandidate]:
    """Ask Claude, grounded in web search, for a seed list of real companies.

    Still not auto-trusted: the caller is expected to spot-check a sample
    before treating these stock codes as fact, same as the IR URLs below.
    """
    client = client or get_client()
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        system=_DISCOVER_SYSTEM,
        thinking={"type": "adaptive"},
        tools=[_web_search_tool(max_uses=8)],
        messages=[
            {
                "role": "user",
                "content": (
                    f"List {count} real, currently-listed Bursa Malaysia Main "
                    "Market companies spanning a range of sectors (banking, "
                    "utilities, plantations, telco, industrials, healthcare, "
                    "consumer). Verify each stock code via search."
                ),
            }
        ],
    )
    _check_refusal(response)
    text = _response_text(response)

    match = _JSON_FENCE.search(text)
    if not match:
        raise RuntimeError(f"model did not emit the expected JSON block; tail: {text[-300:]!r}")

    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model's JSON block did not parse: {exc}") from exc

    return [CompanyCandidate.model_validate(item) for item in raw]


# --------------------------------------------------------------------------
# IR homepage discovery
# --------------------------------------------------------------------------

_IR_URL_SYSTEM = """\
You find the official investor-relations (IR) homepage for a named Malaysian
public-listed company (Bursa Malaysia). Use web search - never answer from
memory alone, since a plausible-looking but wrong URL is worse than
admitting you could not find one.

Prefer the company's own corporate domain over a Bursa Malaysia page, a
stock-screener/aggregator site, or a news article. The page does not need to
be the exact annual-reports page - a general investor-relations landing page
is fine, since a separate crawler finds the annual report from there.

End your answer with exactly one line in this form, and nothing after it:
FINAL_ANSWER: <url or NONE> | <confidence: high|medium|low> | \
<one-line reasoning citing what you found>
"""


def find_ir_url(
    stock_code: str,
    company_name: str,
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
) -> IrUrlCandidate:
    client = client or get_client()
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=_IR_URL_SYSTEM,
        thinking={"type": "adaptive"},
        tools=[_web_search_tool(max_uses=5)],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Company: {company_name} (Bursa Malaysia stock code {stock_code}). "
                    "Find its official investor-relations homepage."
                ),
            }
        ],
    )
    _check_refusal(response)
    text = _response_text(response)

    match = _FINAL_LINE.search(text)
    if not match:
        return IrUrlCandidate(
            stock_code=stock_code,
            company_name=company_name,
            reasoning=f"model did not emit FINAL_ANSWER; tail: {text[-300:]!r}",
        )

    parts = [p.strip() for p in match.group(1).split("|")]
    url = parts[0] if parts and parts[0].upper() != "NONE" else None
    confidence: Confidence = "low"
    if len(parts) > 1 and parts[1].lower() in ("high", "medium", "low", "none"):
        confidence = parts[1].lower()  # type: ignore[assignment]
    reasoning = parts[2] if len(parts) > 2 else ""

    return IrUrlCandidate(
        stock_code=stock_code,
        company_name=company_name,
        ir_homepage_url=url,
        confidence=confidence if url else "none",
        reasoning=reasoning,
    )


def find_ir_urls(
    companies: list[CompanyCandidate], model: str = DEFAULT_MODEL
) -> list[IrUrlCandidate]:
    """One call per company. Serial and deliberately not batched/parallel -
    this is a low-volume, human-reviewed tool, not a production hot path."""
    client = get_client()
    results = []
    for company in companies:
        log.info("looking up IR homepage for %s (%s)", company.name, company.stock_code)
        result = find_ir_url(company.stock_code, company.name, model=model, client=client)
        results.append(result)
        log.info("  -> %s (%s)", result.ir_homepage_url or "NOT FOUND", result.confidence)
    return results


# --------------------------------------------------------------------------
# Batched IR homepage discovery (multiple companies per API call)
# --------------------------------------------------------------------------

_IR_BATCH_SYSTEM = """\
You find the official investor-relations (IR) homepages for Malaysian
public-listed companies (Bursa Malaysia). Use web search for each company -
never answer from memory alone.

Prefer each company's own corporate domain over a Bursa Malaysia page, a
stock-screener/aggregator site, or a news article. The page does not need to
be the exact annual-reports page - a general investor-relations landing page
is fine.

For EACH company, output exactly one line in this format:
RESULT: <stock_code> | <url or NONE> | <confidence: high|medium|low> | <one-line reasoning>

Output all RESULT lines at the end, one per company, in the same order given.
"""

_BATCH_RESULT_LINE = re.compile(
    r"RESULT:\s*(\S+)\s*\|\s*(.+?)\s*\|\s*(high|medium|low|none)\s*\|\s*(.+)",
    re.IGNORECASE,
)


def _find_ir_urls_batch(
    companies: list[CompanyCandidate],
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
) -> list[IrUrlCandidate]:
    """Look up IR URLs for a batch of companies in one API call."""
    client = client or get_client()
    lines = [
        f"- {c.stock_code}: {c.name}" for c in companies
    ]
    prompt = (
        "Find the official investor-relations homepage for each of these "
        f"Bursa Malaysia companies:\n" + "\n".join(lines)
    )
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        system=_IR_BATCH_SYSTEM,
        thinking={"type": "adaptive"},
        tools=[_web_search_tool(max_uses=min(len(companies) * 3, 25))],
        messages=[{"role": "user", "content": prompt}],
    )
    _check_refusal(response)
    text = _response_text(response)

    by_code = {c.stock_code: c for c in companies}
    found: dict[str, IrUrlCandidate] = {}
    for m in _BATCH_RESULT_LINE.finditer(text):
        code, url_str, conf, reasoning = m.group(1), m.group(2).strip(), m.group(3).lower(), m.group(4).strip()
        url = url_str if url_str.upper() != "NONE" else None
        if code in by_code:
            found[code] = IrUrlCandidate(
                stock_code=code,
                company_name=by_code[code].name,
                ir_homepage_url=url,
                confidence=conf if url else "none",  # type: ignore[arg-type]
                reasoning=reasoning,
            )

    results = []
    for c in companies:
        if c.stock_code in found:
            results.append(found[c.stock_code])
        else:
            results.append(IrUrlCandidate(
                stock_code=c.stock_code,
                company_name=c.name,
                reasoning="not returned in batch response",
            ))
    return results


def find_ir_urls_batched(
    companies: list[CompanyCandidate],
    batch_size: int = 10,
    model: str = DEFAULT_MODEL,
) -> list[IrUrlCandidate]:
    """Look up IR URLs in batches of `batch_size` companies per API call."""
    client = get_client()
    all_results: list[IrUrlCandidate] = []
    for i in range(0, len(companies), batch_size):
        chunk = companies[i : i + batch_size]
        log.info(
            "batch %d-%d of %d: looking up %d companies",
            i + 1, i + len(chunk), len(companies), len(chunk),
        )
        results = _find_ir_urls_batch(chunk, model=model, client=client)
        for r in results:
            log.info("  %s -> %s (%s)", r.stock_code, r.ir_homepage_url or "NOT FOUND", r.confidence)
        all_results.extend(results)
    return all_results
