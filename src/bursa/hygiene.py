"""`bursa hygiene report` - a read-only decision list of data problems.

Nothing here writes to the database or touches a file other than the report
outputs. Every finding is a *proposal* for a human to say yes/no to:
deleting a document, merging two company rows, re-filing a subsidiary's
report. The checks:

* DUPLICATE_COMPANY - two company rows whose names normalise to the same
  thing ("SP Setia Berhad" / "S P Setia Berhad"). Which row holds the data
  and which stock code is the real Bursa code (present in the listing CSV)
  are reported separately, because they are often not the same row.
* JUNK_DOCUMENT - truncated test files, broker research notes,
  sustainability-only reports, key-statistics one-pagers, presentations,
  circulars, a subsidiary's own statements attached to the parent.
* DUPLICATE_DOCUMENT - the same report content stored more than once
  (different bytes, same text / same filename+page count).
* NAME_MISMATCH - the attached company's name never appears in the PDF, or
  its registration number disagrees with the company's other documents.
* NO_FACTS_COMPANY - companies with documents but zero facts, with a reason.
* ORPHAN_DOCUMENT - documents with no company.
* FY_GAP - companies with facts whose fiscal-year coverage has holes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from bursa.config import PROJECT_ROOT, get_settings
from bursa.db.enums import PeriodType
from bursa.db.models import Company, Document, ExtractionRun, Fact, Period

# --------------------------------------------------------------------------
# Report model
# --------------------------------------------------------------------------

CATEGORIES = (
    "DUPLICATE_COMPANY",
    "WRONG_CODE",
    "JUNK_DOCUMENT",
    "DUPLICATE_DOCUMENT",
    "NAME_MISMATCH",
    "NO_FACTS_COMPANY",
    "ORPHAN_DOCUMENT",
    "FY_GAP",
)

_SEVERITY_WEIGHT = {"HIGH": 100.0, "MEDIUM": 50.0, "LOW": 10.0}


@dataclass
class Issue:
    category: str
    severity: str  # HIGH / MEDIUM / LOW
    what: str
    why: str
    proposed_fix: str
    evidence: list[str] = field(default_factory=list)
    stock_codes: list[str] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    facts_affected: int = 0
    market_cap_b: float | None = None
    # False when the proposed fix rests on a guess a human must confirm.
    certain: bool = True
    impact: float = 0.0

    def score(self) -> float:
        cap = min(self.market_cap_b or 0.0, 50.0)
        self.impact = round(
            _SEVERITY_WEIGHT.get(self.severity, 10.0)
            + 10.0 * math.log10(self.facts_affected + 1)
            + 2.0 * cap,
            1,
        )
        return self.impact


@dataclass
class Report:
    generated_at: str
    database: str
    pdf_scan: bool
    counts: dict[str, int]
    issues: list[Issue]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


# --------------------------------------------------------------------------
# Name normalisation and duplicate-company matching
# --------------------------------------------------------------------------

_SUFFIXES = {"berhad", "bhd", "limited", "ltd", "plc", "inc"}
_PAREN_NOISE = re.compile(r"\((?:m|malaysia|malaya)\)", re.I)


def _fold(text: str) -> str:
    """Lowercase, '&'->'and', punctuation->space, join runs of single letters."""
    t = _PAREN_NOISE.sub(" ", text.lower())
    t = t.replace("&", " and ").replace("'", "").replace("\u2019", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    tokens = t.split()
    out: list[str] = []
    run: list[str] = []
    for tok in tokens + [""]:
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
            continue
        if run:
            out.append("".join(run))
            run = []
        if tok:
            out.append(tok)
    return " ".join(out)


def normalize_company_name(name: str) -> str:
    """Canonical comparison key: 'S P Setia Berhad' == 'SP Setia Bhd.'."""
    t = _fold(name)
    t = re.sub(r"\breal estate investment trust\b", "reit", t)
    t = re.sub(r"\bcorporation\b", "corp", t)
    t = re.sub(r"\bcompany\b", "co", t)
    tokens = [tok for tok in t.split() if tok not in _SUFFIXES]
    return " ".join(tokens) or t


@dataclass
class CompanyInfo:
    id: int
    stock_code: str
    name: str
    docs: int = 0
    facts: int = 0
    market_cap_b: float | None = None
    listed: bool = False  # code is in the Bursa listing CSV *under this name*
    listed_name: str | None = None  # the listing CSV's name for this code, if any

    @property
    def code_status(self) -> str:
        """OK / OTHER_ISSUER (code belongs to a different listed name) / UNLISTED."""
        if self.listed:
            return "OK"
        return "OTHER_ISSUER" if self.listed_name else "UNLISTED"

    def describe(self) -> str:
        s = f"{self.stock_code} '{self.name}': {self.docs} docs, {self.facts} facts, code "
        if self.code_status == "OK":
            s += "in listing CSV"
        elif self.code_status == "OTHER_ISSUER":
            s += f"belongs to '{self.listed_name}' in listing CSV"
        else:
            s += "not in listing CSV"
        return s + (f", mcap RM{self.market_cap_b:.1f}B" if self.market_cap_b else "")


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_company_name(a), normalize_company_name(b)
    if na == nb:
        return 1.0
    if na.replace(" ", "") == nb.replace(" ", ""):
        return 0.99
    return SequenceMatcher(None, na, nb).ratio()


def find_duplicate_companies(
    companies: list[CompanyInfo], *, threshold: float = 0.93
) -> list[tuple[CompanyInfo, CompanyInfo, float]]:
    """Pairs of company rows that look like the same issuer."""
    keyed = [(c, normalize_company_name(c.name)) for c in companies]
    pairs: list[tuple[CompanyInfo, CompanyInfo, float]] = []
    for i, (a, na) in enumerate(keyed):
        for b, nb in keyed[i + 1 :]:
            if a.stock_code == b.stock_code:
                continue
            if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
                pairs.append((a, b, 1.0))
                continue
            if abs(len(na) - len(nb)) > 4 or na[:3] != nb[:3]:
                continue
            sm = SequenceMatcher(None, na, nb)
            if sm.quick_ratio() < threshold:
                continue
            r = sm.ratio()
            if r >= threshold:
                pairs.append((a, b, round(r, 3)))
    return pairs


def duplicate_company_issue(a: CompanyInfo, b: CompanyInfo, sim: float) -> Issue:
    holder, other = (a, b) if (a.facts, a.docs) >= (b.facts, b.docs) else (b, a)
    listed = [c for c in (a, b) if c.listed]
    evidence = [c.describe() for c in (holder, other)]
    if sim < 1.0:
        evidence.append(f"name similarity {sim:.2f} (fuzzy - confirm same issuer)")
    certain = sim >= 0.99 and len(listed) == 1
    if len(listed) == 1:
        real = listed[0]
        fake = b if real is a else a
        if fake.docs == 0 and fake.facts == 0:
            fix = f"delete empty company row {fake.stock_code} (real code {real.stock_code} already holds the data)"
        else:
            fix = (
                f"merge {fake.stock_code} into {real.stock_code}: move documents/facts/periods/"
                f"synonyms/scrape attempts to {real.stock_code}, then delete row {fake.stock_code}"
            )
            if real.docs:
                fix += "; then dedupe any documents present on both"
            if fake.code_status == "OTHER_ISSUER":
                fix += f" (code {fake.stock_code} really belongs to '{fake.listed_name}')"
    elif len(listed) == 2:
        fix = "both codes are listed under this name - probably two different issuers; verify, likely no action"
        certain = False
    else:
        fix = (
            f"neither code is in the listing CSV - look up the real Bursa code, then merge "
            f"{other.stock_code} into {holder.stock_code} (or both into the real code)"
        )
        certain = False
    total_facts = a.facts + b.facts
    caps = [c.market_cap_b for c in listed if c.market_cap_b]
    severity = "HIGH" if (total_facts or a.docs + b.docs) else "LOW"
    if len(listed) == 2:
        severity = "LOW"
    return Issue(
        category="DUPLICATE_COMPANY",
        severity=severity,
        what=f"Duplicate company rows {holder.stock_code} / {other.stock_code} ({holder.name})",
        why="same issuer stored twice; facts, peers and valuation see a split or a wrong code",
        proposed_fix=fix,
        evidence=evidence,
        stock_codes=[holder.stock_code, other.stock_code],
        facts_affected=total_facts,
        market_cap_b=max(caps) if caps else None,
        certain=certain,
    )


# --------------------------------------------------------------------------
# Junk-document classifier
# --------------------------------------------------------------------------

_TEST_FILE = re.compile(r"(?:^|[_\-\s])test\d*\.pdf$", re.I)
_BROKER_NAME = re.compile(
    r"PB_Stock|PublicInvest|Bursa[\s_%20-]*Mid[sS]|(?:^|[_\s-])RHB(?:[_\s.-]|$)|Initiating[\s_-]*Coverage|"
    r"Results?[\s_-]*Review|MIDF[\s_-]*Research|Kenanga[\s_-]*Research|HLIB|CGS[\s_-]*CIMB|"
    r"Maybank[\s_-]*IB|AmInvest|TA[\s_-]*Research|UOBKH|Affin[\s_-]*Hwang",
    re.I,
)
_BROKER_TEXT = re.compile(
    r"\b(target price|price target|tp\s*:|(?:maintain|upgrade|downgrade)\s+(?:buy|sell|hold|outperform|"
    r"underperform|neutral)|research analyst|analyst certification|stock rating|recommendation\s*:|"
    r"bloomberg (?:code|ticker)|share price performance)\b",
    re.I,
)
_SUSTAIN = re.compile(r"sustainab|\besg\b", re.I)
_ANNUAL = re.compile(r"annual[\s_\-%20]*report|\bAR\s?20\d\d|\bAR\d{2}\b|_AR_|-AR-", re.I)
_KEY_STATS = re.compile(
    r"key[\s_\-]*statistic|fact[\s_\-]*sheet|financial[\s_\-]*highlights?\.pdf", re.I
)
_PRESENTATION = re.compile(
    r"presentation|briefing|investor[\s_\-]*deck|slides|analyst[\s_\-]*meet", re.I
)
_CIRCULAR = re.compile(
    r"circular|notice[\s_\-]*of[\s_\-]*(?:agm|egm)|\begm\b|prospectus|abridged", re.I
)
_SUBSIDIARY_HINT = re.compile(
    r"islamic|\bi[\s_\-]bank|investment[\s_\-]bank|takaful|insurance", re.I
)


@dataclass
class JunkVerdict:
    kind: str  # TEST_FILE, BROKER_NOTE, SUSTAINABILITY_ONLY, KEY_STATISTICS, PRESENTATION, CIRCULAR, SUBSIDIARY_REPORT
    reason: str
    certain: bool = True


def classify_junk(
    filename: str,
    page_count: int | None,
    company_name: str,
    head_text: str = "",
) -> list[JunkVerdict]:
    """Why a document should probably not be in the annual-report corpus.

    ``head_text`` is the text of the first few pages ('' if unscanned).
    Returns [] for an ordinary-looking report.
    """
    out: list[JunkVerdict] = []
    pages = page_count or 0
    fname = filename or ""
    head = head_text[:20000]
    if _TEST_FILE.search(fname):
        out.append(
            JunkVerdict("TEST_FILE", f"filename '{fname}' is a hand-made test cut ({pages} pages)")
        )
    broker_hits = len(_BROKER_TEXT.findall(head))
    if _BROKER_NAME.search(fname) and pages <= 30:
        out.append(JunkVerdict("BROKER_NOTE", f"broker-research filename '{fname}', {pages} pages"))
    elif pages and pages <= 12 and broker_hits >= 2:
        out.append(
            JunkVerdict(
                "BROKER_NOTE",
                f"{broker_hits} analyst-report phrases in a {pages}-page doc",
                certain=False,
            )
        )
    if _KEY_STATS.search(fname) or (pages and pages <= 2):
        out.append(JunkVerdict("KEY_STATISTICS", f"'{fname}' is a {pages}-page summary/factsheet"))
    if _SUSTAIN.search(fname) and not _ANNUAL.search(fname):
        out.append(
            JunkVerdict("SUSTAINABILITY_ONLY", f"filename '{fname}' names a sustainability report")
        )
    elif head and not _ANNUAL.search(fname):
        # Annual reports' contents pages also list a "Sustainability Statement",
        # so only flag text that never mentions financial statements / annual report.
        if re.search(r"sustainability\s+(?:report|statement)", head[:3000], re.I) and not re.search(
            r"annual\s+report|financial\s+statements|statements?\s+of\s+financial\s+position|"
            r"directors'?\s*report",
            head,
            re.I,
        ):
            out.append(
                JunkVerdict(
                    "SUSTAINABILITY_ONLY", "cover page reads 'Sustainability Report'", certain=False
                )
            )
    if _PRESENTATION.search(fname):
        out.append(JunkVerdict("PRESENTATION", f"filename '{fname}' is a presentation deck"))
    if _CIRCULAR.search(fname):
        out.append(JunkVerdict("CIRCULAR", f"filename '{fname}' is a circular/notice/prospectus"))
    if _SUBSIDIARY_HINT.search(fname) and not _SUBSIDIARY_HINT.search(company_name):
        out.append(
            JunkVerdict(
                "SUBSIDIARY_REPORT",
                f"filename '{fname}' names a subsidiary entity type not in '{company_name}'",
                certain=False,
            )
        )
    return out


# --------------------------------------------------------------------------
# PDF text checks: name presence, registration number, content fingerprint
# --------------------------------------------------------------------------

_REG_NEW = re.compile(r"(?<!\d)((?:19|20)\d{2}0[1-6]\d{6})(?!\d)")


@dataclass
class DocText:
    head: str  # text of the first N pages
    pages_read: int
    page_count: int
    full: str | None = None  # whole document, only fetched when needed


def extract_reg_no(text: str) -> str | None:
    """Most frequent 12-digit SSM registration number in the text."""
    found = Counter(_REG_NEW.findall(text))
    return found.most_common(1)[0][0] if found else None


_ENTITY = re.compile(
    r"((?:[A-Z0-9][\w&.'\-]*\s+){1,6}?)(?:\((?:M|Malaysia)\)\s+)?(?:BERHAD|Berhad|BHD\.?|Bhd\.?)(?![a-z])"
)
_NOT_ENTITY = {"bursa securities", "malaysia securities", "bursa malaysia", "securities exchange"}
_GENERIC_TOKENS = {
    "group",
    "holdings",
    "holding",
    "bank",
    "corp",
    "resources",
    "industries",
    "international",
    "capital",
    "technology",
    "technologies",
    "global",
    "properties",
    "development",
    "reit",
    "malaysia",
    "and",
    "co",
    "equity",
    "services",
    "solutions",
}


@dataclass
class Entity:
    key: str  # last two normalised name tokens, e.g. 'ambank islamic'
    weight: int  # mentions (x3 on the first ~3000 chars)
    reg_no: str | None  # 12-digit registration number printed next to it


def dominant_entity(text: str) -> Entity | None:
    """The 'X Berhad' named most often in the text (cover/corporate-info pages).

    Keyed on the last two tokens so table-of-contents words that run into the
    name ('Overview About Scientex Berhad') don't split the count. The
    registration number is taken only from right after the name, so the
    share registrar's or secretary's number elsewhere on the page is ignored.
    """
    t = re.sub(r"\s+", " ", text)
    weights: Counter = Counter()
    regs: dict[str, Counter] = defaultdict(Counter)
    for m in _ENTITY.finditer(t):
        keep: list[str] = []
        for w in reversed(m.group(1).split()):
            if w[0].isupper() or w[0].isdigit() or w == "&":
                keep.append(w)
            else:
                break
        toks = normalize_company_name(" ".join(reversed(keep))).split()
        if not toks or toks[-1] in ("sdn", "bhd") or toks[-1].isdigit():
            continue
        key = " ".join(toks[-2:])
        if key in _NOT_ENTITY:
            continue
        weights[key] += 3 if m.start() < 3000 else 1
        r = _REG_NEW.search(t, m.end(), m.end() + 120)
        if r:
            regs[key][r.group(1)] += 1
    if not weights:
        return None
    key, w = weights.most_common(1)[0]
    reg = regs[key].most_common(1)
    return Entity(key, w, reg[0][0] if reg else None)


def entity_matches(key: str, company_name: str) -> bool:
    """Whether an entity key plausibly names this company."""
    n = " " + normalize_company_name(company_name) + " "
    if f" {key} " in n or key.replace(" ", "") in n.replace(" ", ""):
        return True
    return any(len(t) >= 3 and t not in _GENERIC_TOKENS and f" {t} " in n for t in key.split())


def name_appears(company_name: str, text: str) -> bool:
    """Whether the company's name (normalised) appears in the text."""
    core = base = normalize_company_name(company_name)
    folded = " " + _fold(text) + " "
    if len(core.replace(" ", "")) < 4:
        core = _fold(company_name)  # 'I-Berhad' -> 'i berhad'
    if f" {core} " in folded:
        return True
    squashed_core = core.replace(" ", "")
    if len(squashed_core) >= 5 and squashed_core in folded.replace(" ", ""):
        return True
    # First two distinctive tokens are often enough ('Tropicana Corp' vs 'Tropicana Corporation').
    toks = [t for t in base.split() if len(t) >= 4]
    return bool(toks) and f" {toks[0]} " in folded and (len(toks) < 2 or f" {toks[1]} " in folded)


def is_scanned(dt: DocText) -> bool:
    return dt.pages_read > 0 and len(dt.head.strip()) < 80 * dt.pages_read


def text_fingerprint(dt: DocText) -> str | None:
    norm = re.sub(r"\s+", " ", dt.head).strip()
    if len(norm) < 500:
        return None
    return hashlib.sha1(f"{dt.page_count}|{norm[:20000]}".encode()).hexdigest()


TextProvider = Callable[[int, str, bool], "DocText | None"]


def pymupdf_text_provider(head_pages: int = 10, cache_path: Path | None = None) -> TextProvider:
    """Reads PDFs with PyMuPDF (read-only); optional JSON cache keyed by path."""
    import pymupdf

    cache: dict[str, dict] = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def provide(doc_id: int, path: str, full: bool) -> DocText | None:
        key = f"{path}|{'full' if full else 'head'}"
        if key in cache:
            return DocText(**cache[key])
        try:
            with pymupdf.open(path) as pdf:
                n = pdf.page_count
                upto = n if full else min(head_pages, n)
                text = "\n".join(pdf[i].get_text() for i in range(upto))
        except Exception:
            return None
        dt = DocText(head=text[:30000] if not full else text[:8000], pages_read=upto, page_count=n)
        if full:
            dt.full = text
        if not full:
            cache[key] = asdict(dt)
        return dt

    def save() -> None:
        if cache_path:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")

    provide.save = save  # type: ignore[attr-defined]
    return provide


# --------------------------------------------------------------------------
# Hint files
# --------------------------------------------------------------------------


def load_listing(path: Path) -> dict[str, str]:
    """stock_code -> company_name from the Bursa main-market CSV."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {r["stock_code"].strip(): r["company_name"].strip() for r in csv.DictReader(f)}


def load_market_caps(report_json: Path, manual_txt: Path) -> dict[str, float]:
    """stock_code -> market cap in RM billions (yfinance report, then manual list)."""
    caps: dict[str, float] = {}
    if manual_txt.exists():
        for line in manual_txt.read_text(encoding="utf-8").splitlines():
            m = re.match(
                r"\s*(LARGE|MID|SMALL|MICRO|\w+)\s+(\d{4}[A-Z]?)\s+.+?\s+(\d+\.\d+)\s+", line
            )
            if m:
                caps[m.group(2)] = float(m.group(3))
    if report_json.exists():
        for row in json.loads(report_json.read_text(encoding="utf-8")):
            if row.get("market_cap"):
                caps[row["stock_code"]] = row["market_cap"] / 1e9
    return caps


# --------------------------------------------------------------------------
# Report builder
# --------------------------------------------------------------------------


@dataclass
class _Doc:
    id: int
    company_id: int | None
    filename: str
    page_count: int | None
    path: str
    facts: int
    has_run: bool
    doc_type: str
    fy: int | None = None  # latest FY the document reports facts for


def _load(session: Session) -> tuple[dict[int, CompanyInfo], list[_Doc], dict[int, set[int]]]:
    companies = {
        cid: CompanyInfo(id=cid, stock_code=code, name=name)
        for cid, code, name in session.execute(select(Company.id, Company.stock_code, Company.name))
    }
    fact_by_doc = dict(
        session.execute(
            select(Fact.reported_in_document_id, func.count()).group_by(
                Fact.reported_in_document_id
            )
        ).all()
    )
    fact_by_company = dict(
        session.execute(select(Fact.company_id, func.count()).group_by(Fact.company_id)).all()
    )
    run_docs = set(session.scalars(select(ExtractionRun.document_id).distinct()))
    docs = [
        _Doc(d_id, cid, fn, pc, sp, fact_by_doc.get(d_id, 0), d_id in run_docs, str(dt))
        for d_id, cid, fn, pc, sp, dt in session.execute(
            select(
                Document.id,
                Document.company_id,
                Document.original_filename,
                Document.page_count,
                Document.storage_path,
                Document.doc_type,
            ).order_by(Document.id)
        )
    ]
    for d in docs:
        if d.company_id in companies:
            companies[d.company_id].docs += 1
    for cid, n in fact_by_company.items():
        if cid in companies:
            companies[cid].facts = n
    fy_years: dict[int, set[int]] = defaultdict(set)
    for cid, fy in session.execute(
        select(Period.company_id, Period.fiscal_year)
        .join(Fact, Fact.period_id == Period.id)
        .where(Period.period_type == PeriodType.FY)
        .distinct()
    ):
        fy_years[cid].add(fy)
    doc_fy = dict(
        session.execute(
            select(Fact.reported_in_document_id, func.max(Period.fiscal_year))
            .join(Period, Fact.period_id == Period.id)
            .where(Period.period_type == PeriodType.FY)
            .group_by(Fact.reported_in_document_id)
        ).all()
    )
    for d in docs:
        d.fy = doc_fy.get(d.id)
    return companies, docs, fy_years


def apply_listing(
    companies: Iterable[CompanyInfo], listing: dict[str, str], market_caps: dict[str, float]
) -> None:
    """Mark each company's code OK / OTHER_ISSUER / UNLISTED against the listing CSV.

    The market cap is only trusted for a code that is listed under this
    company's own name - otherwise yfinance's figure belongs to someone else.
    """
    for c in companies:
        c.listed_name = listing.get(c.stock_code)
        c.listed = c.listed_name is not None and name_similarity(c.name, c.listed_name) >= 0.85
        c.market_cap_b = market_caps.get(c.stock_code) if (c.listed or not listing) else None


def build_report(
    session: Session,
    *,
    listing: dict[str, str] | None = None,
    market_caps: dict[str, float] | None = None,
    text_provider: TextProvider | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Report:
    listing = listing or {}
    market_caps = market_caps or {}
    companies, docs, fy_years = _load(session)
    apply_listing(companies.values(), listing, market_caps)
    issues: list[Issue] = []

    def cap(cid: int | None) -> float | None:
        return companies[cid].market_cap_b if cid in companies else None

    def code(cid: int | None) -> str:
        return companies[cid].stock_code if cid in companies else "-"

    # 1. Duplicate companies -------------------------------------------------
    in_pair: set[int] = set()
    pair_issue: dict[frozenset[int], Issue] = {}
    for a, b, sim in find_duplicate_companies(list(companies.values())):
        issue = duplicate_company_issue(a, b, sim)
        issues.append(issue)
        in_pair |= {a.id, b.id}
        pair_issue[frozenset((a.id, b.id))] = issue

    # 1b. Codes that are not this company's Bursa code -----------------------
    if listing:
        for c in companies.values():
            if c.code_status == "OK" or c.id in in_pair:
                continue
            has_data = bool(c.docs or c.facts)
            ours = normalize_company_name(c.name).split()
            theirs = normalize_company_name(c.listed_name or "").split()
            if ours and theirs and ours[0] == theirs[0] and len(ours[0]) >= 4:
                issues.append(
                    Issue(
                        category="WRONG_CODE",
                        severity="LOW",
                        what=f"{c.stock_code} '{c.name}' vs listing name '{c.listed_name}'",
                        why="same leading name - probably a rename or name variant, code likely right",
                        proposed_fix=f"rename row {c.stock_code} to '{c.listed_name}'",
                        evidence=[c.describe()],
                        stock_codes=[c.stock_code],
                        facts_affected=c.facts,
                        certain=False,
                    )
                )
                continue
            issues.append(
                Issue(
                    category="WRONG_CODE",
                    severity="HIGH" if c.facts else ("MEDIUM" if has_data else "LOW"),
                    what=f"{c.stock_code} '{c.name}' is not this company's Bursa code",
                    why=(
                        f"listing CSV has {c.stock_code} = '{c.listed_name}'"
                        if c.code_status == "OTHER_ISSUER"
                        else f"{c.stock_code} is not in the listing CSV"
                    )
                    + "; yfinance/peers/benchmarks will look up the wrong ticker",
                    proposed_fix=(
                        f"look up the real code for '{c.name}' and re-key row {c.stock_code} to it"
                        if has_data
                        else f"delete empty row {c.stock_code} (or re-key it to the real code)"
                    ),
                    evidence=[c.describe()],
                    stock_codes=[c.stock_code],
                    facts_affected=c.facts,
                    certain=False,
                )
            )

    # 2. Orphans ---------------------------------------------------------------
    for d in docs:
        if d.company_id is None:
            issues.append(
                Issue(
                    category="ORPHAN_DOCUMENT",
                    severity="MEDIUM",
                    what=f"Document {d.id} '{d.filename}' has no company",
                    why="unattached documents are never extracted into any company's facts",
                    proposed_fix=f"attach document {d.id} to the right company (or delete it)",
                    document_ids=[d.id],
                    facts_affected=d.facts,
                )
            )

    # 3. Per-document scans ---------------------------------------------------
    texts: dict[int, DocText | None] = {}
    if text_provider:
        for i, d in enumerate(docs):
            texts[d.id] = text_provider(d.id, d.path, False)
            if progress:
                progress(i + 1, len(docs))

    doc_reasons: dict[int, list[str]] = defaultdict(list)
    junk_doc_ids: set[int] = set()
    for d in docs:
        c = companies.get(d.company_id) if d.company_id is not None else None
        dt = texts.get(d.id)
        verdicts = classify_junk(
            d.filename, d.page_count, c.name if c else "", dt.head if dt else ""
        )
        if verdicts:
            junk_doc_ids.add(d.id)
            kinds = ", ".join(v.kind for v in verdicts)
            doc_reasons[d.id].append(kinds)
            evidence = [v.reason for v in verdicts] + [
                f"{d.page_count} pages, {d.facts} facts" + (f", latest FY {d.fy}" if d.fy else "")
            ]
            if "SUBSIDIARY_REPORT" in kinds:
                fix = f"delete document {d.id} or re-attach to the subsidiary; keep out of parent facts"
            elif kinds == "TEST_FILE" and d.facts:
                same_fy = [
                    o
                    for o in docs
                    if o.company_id == d.company_id
                    and o.id != d.id
                    and o.fy == d.fy
                    and o.facts
                    and not _TEST_FILE.search(o.filename)
                ]
                if same_fy:
                    fix = f"delete document {d.id} (FY{d.fy} also covered by document {same_fy[0].id})"
                    evidence.append(
                        f"doc {same_fy[0].id} '{same_fy[0].filename}' reports the same FY"
                    )
                else:
                    fix = (
                        f"replace document {d.id} with the official full PDF, then delete it "
                        f"(only source of FY{d.fy} for this company)"
                    )
            else:
                fix = f"delete document {d.id} (and its {d.facts} facts)"
            issues.append(
                Issue(
                    category="JUNK_DOCUMENT",
                    severity="HIGH" if d.facts else "MEDIUM",
                    what=f"Doc {d.id} '{d.filename}' on {code(d.company_id)} is {kinds}",
                    why="not an original full report of this company; its facts (if any) "
                    "duplicate or contaminate the real report's",
                    proposed_fix=fix,
                    evidence=evidence,
                    stock_codes=[code(d.company_id)],
                    document_ids=[d.id],
                    facts_affected=d.facts,
                    market_cap_b=cap(d.company_id),
                    certain=all(v.certain for v in verdicts),
                )
            )

    # 3b. Duplicate content -----------------------------------------------------
    groups: dict[str, list[_Doc]] = defaultdict(list)
    for d in docs:
        dt = texts.get(d.id)
        fp = text_fingerprint(dt) if dt else None
        key = fp or f"name:{d.filename.lower()}|{d.page_count}"
        groups[key].append(d)
    for key, members in groups.items():
        if len(members) < 2:
            continue
        if key.startswith("name:") and texts:
            continue  # both scanned/no-text: filename match alone is weak once text exists

        def keep_rank(m: _Doc) -> tuple:
            c = companies.get(m.company_id) if m.company_id is not None else None
            dt = texts.get(m.id)
            named = bool(c and dt and name_appears(c.name, dt.head))
            return (
                not named,
                not (c and c.listed),
                bool(_TEST_FILE.search(m.filename)),
                -m.facts,
                m.id,
            )

        ranked = sorted(members, key=keep_rank)
        keep, drop = ranked[0], ranked[1:]
        member_cids = frozenset(m.company_id for m in members)
        cross = len(member_cids) > 1
        if member_cids in pair_issue:
            # Same issuer under two rows: fold the document cleanup into the merge.
            pi = pair_issue[member_cids]
            pi.proposed_fix += (
                "; after merging delete duplicate "
                + ", ".join(f"document {m.id}" for m in drop)
                + f" (same content as {keep.id})"
            )
            pi.document_ids += [m.id for m in members]
            continue
        issues.append(
            Issue(
                category="DUPLICATE_DOCUMENT",
                severity="HIGH" if cross else "MEDIUM",
                what=(
                    f"Same report stored {len(members)}x: docs "
                    + ", ".join(f"{m.id}({code(m.company_id)})" for m in members)
                ),
                why="duplicate content double-counts facts"
                + ("; attached to different company rows" if cross else ""),
                proposed_fix=f"keep document {keep.id} on {code(keep.company_id)}; delete "
                + ", ".join(f"document {m.id}" for m in drop),
                evidence=[
                    f"doc {m.id} '{m.filename}' on {code(m.company_id)}: {m.page_count} pages, {m.facts} facts"
                    for m in members
                ]
                + [
                    "identical text of first pages"
                    if not key.startswith("name:")
                    else "same filename+page count"
                ],
                stock_codes=sorted({code(m.company_id) for m in members}),
                document_ids=[m.id for m in members],
                facts_affected=sum(m.facts for m in drop),
                market_cap_b=max((cap(m.company_id) or 0) for m in members) or None,
            )
        )

    # 3c. Name / dominant-entity mismatch ------------------------------------
    if text_provider:
        flagged: dict[int, list[tuple[_Doc, Entity | None, bool]]] = defaultdict(list)
        own_regs: dict[int, Counter] = defaultdict(Counter)
        for d in docs:
            dt = texts.get(d.id)
            c = companies.get(d.company_id) if d.company_id is not None else None
            if not dt or not c or is_scanned(dt) or d.id in junk_doc_ids:
                continue
            ent = dominant_entity(dt.head)
            ent_ok = ent is None or ent.weight < 12 or entity_matches(ent.key, c.name)
            if ent and ent.reg_no and entity_matches(ent.key, c.name):
                own_regs[c.id][ent.reg_no] += 1
            named = name_appears(c.name, dt.head)
            if not named:
                full = text_provider(d.id, d.path, True)
                named = bool(full and full.full and name_appears(c.name, full.full))
            if named and ent_ok:
                continue
            flagged[c.id].append((d, ent, named))
        for cid, items in flagged.items():
            c = companies[cid]
            issues.append(_name_mismatch_issue(c, items, companies))

        # One registration number as the own entity of two company rows.
        dup_pairs = {frozenset(i.stock_codes) for i in issues if i.category == "DUPLICATE_COMPANY"}
        reg_owner: dict[str, set[int]] = defaultdict(set)
        for cid, cnt in own_regs.items():
            reg_owner[cnt.most_common(1)[0][0]].add(cid)
        for reg, cids in reg_owner.items():
            codes = sorted(code(x) for x in cids)
            if len(cids) < 2 or frozenset(codes) in dup_pairs:
                continue
            members = [companies[x] for x in cids]
            issues.append(
                Issue(
                    category="DUPLICATE_COMPANY",
                    severity="HIGH",
                    what=f"Registration no. {reg} is the own entity of {', '.join(codes)}",
                    why="one legal entity stored under several company rows (names differ)",
                    proposed_fix="confirm the real Bursa code (listing CSV), then merge the other rows into it",
                    evidence=[m.describe() for m in members],
                    stock_codes=codes,
                    facts_affected=sum(m.facts for m in members),
                    market_cap_b=max((m.market_cap_b or 0) for m in members) or None,
                    certain=False,
                )
            )

    # 4. Companies with documents but no facts -------------------------------
    for c in companies.values():
        if c.docs == 0 or c.facts:
            continue
        reasons: Counter = Counter()
        cdocs = [d for d in docs if d.company_id == c.id]
        for d in cdocs:
            reasons[_no_fact_reason(d, texts.get(d.id), doc_reasons.get(d.id))] += 1
        reason_txt = ", ".join(f"{r} x{n}" for r, n in reasons.most_common())
        issues.append(
            Issue(
                category="NO_FACTS_COMPANY",
                severity="MEDIUM" if (c.market_cap_b or 0) >= 1 else "LOW",
                what=f"{c.stock_code} {c.name}: {c.docs} docs, 0 facts",
                why=f"reason: {reason_txt}",
                proposed_fix=_no_fact_fix(reasons, cdocs),
                evidence=[f"doc {d.id} '{d.filename}' {d.page_count}p" for d in cdocs[:5]],
                stock_codes=[c.stock_code],
                document_ids=[d.id for d in cdocs],
                market_cap_b=c.market_cap_b,
                certain=False,
            )
        )

    # 5. FY coverage gaps ------------------------------------------------------
    for cid, years in fy_years.items():
        if cid not in companies or len(years) < 2:
            continue
        missing = sorted(set(range(min(years), max(years) + 1)) - years)
        if not missing:
            continue
        c = companies[cid]
        issues.append(
            Issue(
                category="FY_GAP",
                severity="LOW",
                what=f"{c.stock_code} {c.name}: FY facts missing for {', '.join(map(str, missing))}",
                why=f"coverage {min(years)}-{max(years)} has holes; trends/CAGR span a gap",
                proposed_fix="download the missing years' annual reports (or check why they produced no FY facts)",
                evidence=[f"FY years with facts: {sorted(years)}"],
                stock_codes=[c.stock_code],
                facts_affected=c.facts,
                market_cap_b=c.market_cap_b,
            )
        )

    if text_provider and hasattr(text_provider, "save"):
        text_provider.save()  # type: ignore[attr-defined]
    for i in issues:
        i.score()
    issues.sort(key=lambda i: (-i.impact, i.category, i.what))
    counts = {cat: sum(1 for i in issues if i.category == cat) for cat in CATEGORIES}
    return Report(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        database=str(session.get_bind().url),
        pdf_scan=text_provider is not None,
        counts=counts,
        issues=issues,
    )


def _name_mismatch_issue(
    c: CompanyInfo,
    items: list[tuple[_Doc, Entity | None, bool]],
    companies: dict[int, CompanyInfo],
) -> Issue:
    keys = Counter(e.key for _, e, _ in items if e)
    top = keys.most_common(1)[0][0] if keys else None
    unnamed = [d for d, _, named in items if not named]
    ids = [d.id for d, _, _ in items]
    id_txt = ", ".join(map(str, ids[:8])) + (" ..." if len(ids) > 8 else "")
    all_docs = len(items) == c.docs
    certain = False
    others: list[CompanyInfo] = []
    if top:
        others = [
            o
            for o in companies.values()
            if o.id != c.id and f" {top} " in f" {normalize_company_name(o.name)} "
        ]
    if top and c.listed_name and not c.listed and entity_matches(top, c.listed_name):
        fix = f"rename row {c.stock_code} to '{c.listed_name}' (the listing CSV name; its reports are by it)"
        certain = all_docs
    elif top and len(others) == 1 and unnamed:
        o = others[0]
        fix = f"re-attach document(s) {id_txt} to {o.stock_code} ('{o.name}')"
    elif top and all_docs:
        fix = (
            f"row name '{c.name}' does not match any of its reports ('{top}'): "
            "check for a rename or a wrong code, then rename/re-key the row"
        )
    else:
        fix = f"inspect document(s) {id_txt}; delete or re-attach to the right company"
    evidence = [
        f"doc {d.id} '{d.filename[:50]}' {d.page_count}p {d.facts} facts: "
        + ("name absent" if not named else "name present")
        + (
            f", mostly about '{e.key}' ({e.weight}x"
            + (f", reg {e.reg_no}" if e.reg_no else "")
            + ")"
            if e
            else ""
        )
        for d, e, named in items[:6]
    ]
    facts = sum(d.facts for d, _, _ in items)
    if unnamed:
        severity = "HIGH" if any(d.facts for d in unnamed) else "MEDIUM"
        what = (
            f"{c.stock_code} '{c.name}': {len(unnamed)} of {c.docs} docs never mention the company"
        )
    else:
        severity = "LOW"
        what = f"{c.stock_code} '{c.name}': {len(items)} docs are mostly about another entity"
    if top:
        what += f" (they name '{top}')"
    return Issue(
        category="NAME_MISMATCH",
        severity=severity,
        what=what,
        why="another issuer's or a subsidiary's report attached here, or the row's name/code is wrong",
        proposed_fix=fix,
        evidence=evidence,
        stock_codes=[c.stock_code] + [o.stock_code for o in others[:1]],
        document_ids=ids,
        facts_affected=facts,
        market_cap_b=c.market_cap_b,
        certain=certain,
    )


def _no_fact_reason(d: _Doc, dt: DocText | None, junk: list[str] | None) -> str:
    if junk:
        return "WRONG_DOC_TYPE(" + junk[0] + ")"
    if not d.has_run:
        return "NOT_YET_EXTRACTED"
    if dt is not None and is_scanned(dt):
        return "SCANNED"
    if (d.page_count or 0) < 20:
        return "SHORT_DOC"
    if re.search(r"part[\s_\-]*(?:1|one|i)(?![0-9a-z])", d.filename, re.I):
        return "PART_WITHOUT_STATEMENTS"
    if dt is None and d.page_count is None:
        return "UNREADABLE"
    return "NO_STATEMENT_PAGE"


def _no_fact_fix(reasons: Counter, cdocs: list[_Doc]) -> str:
    top = reasons.most_common(1)[0][0]
    if top.startswith("WRONG_DOC_TYPE"):
        return "delete the junk documents and download the real annual report"
    if top == "NOT_YET_EXTRACTED":
        return "run normalize facts for these documents"
    if top == "SCANNED":
        return "re-run extraction with OCR, or source a text-layer PDF"
    if top == "PART_WITHOUT_STATEMENTS":
        return "download the financial-statements part of the report (only the narrative part is stored)"
    if top == "SHORT_DOC":
        return "replace with the full annual report (current docs are excerpts)"
    return "inspect page selection (extractor found no statement page) or source the right report"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def to_markdown(report: Report, top: int = 30) -> str:
    lines = [
        "# Data hygiene report",
        "",
        f"Generated {report.generated_at} from `{report.database}` (read-only). "
        f"PDF text scan: {'yes' if report.pdf_scan else 'no'}.",
        "",
        "Every row is a proposal - nothing has been changed. `certain=no` means the fix rests on a guess.",
        "",
        "## Counts",
        "",
        "| Category | Issues |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in report.counts.items()]
    lines += [
        "",
        f"## Top {top} actions",
        "",
        "| # | Impact | Category | What | Proposed fix | Evidence | Certain |",
        "|---|---|---|---|---|---|---|",
    ]
    for n, i in enumerate(report.issues[:top], 1):
        lines.append(
            f"| {n} | {i.impact} | {i.category} | {_md_cell(i.what)} | {_md_cell(i.proposed_fix)} | "
            f"{_md_cell('; '.join(i.evidence[:4]))} | {'yes' if i.certain else 'no'} |"
        )
    for cat in CATEGORIES:
        rows = [i for i in report.issues if i.category == cat]
        if not rows:
            continue
        lines += [
            "",
            f"## {cat} ({len(rows)})",
            "",
            "| Impact | What | Why | Proposed fix | Evidence | Certain |",
            "|---|---|---|---|---|---|",
        ]
        for i in rows:
            lines.append(
                f"| {i.impact} | {_md_cell(i.what)} | {_md_cell(i.why)} | {_md_cell(i.proposed_fix)} | "
                f"{_md_cell('; '.join(i.evidence))} | {'yes' if i.certain else 'no'} |"
            )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

app = typer.Typer(help="Read-only data-hygiene decision list.", no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Read-only data-hygiene decision list."""


def _readonly_session() -> Session:
    url = get_settings().database_url
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[1]
        p = Path(db_path)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        engine = create_engine(f"sqlite:///file:{p.as_posix()}?mode=ro&uri=true", future=True)
    else:
        engine = create_engine(url, future=True)
    return sessionmaker(bind=engine, future=True)()


@app.command("report")
def report_cmd(
    out: Annotated[
        Path, typer.Option("--out", help="Directory for hygiene.json / hygiene.md.")
    ] = Path("."),
    scan_pdfs: Annotated[
        bool, typer.Option("--scan-pdfs/--no-scan-pdfs", help="Read PDF text (slow).")
    ] = True,
    cache: Annotated[
        Path | None, typer.Option("--cache", help="JSON cache of PDF head text.")
    ] = None,
    top: Annotated[int, typer.Option("--top")] = 30,
) -> None:
    """Write hygiene.json + hygiene.md. Opens the database read-only."""
    listing = load_listing(PROJECT_ROOT / "bursa_main_market.csv")
    caps = load_market_caps(
        PROJECT_ROOT / "market_cap_report.json", PROJECT_ROOT / "manual_downloads.txt"
    )
    provider = pymupdf_text_provider(cache_path=cache) if scan_pdfs else None

    def progress(i: int, n: int) -> None:
        if i % 200 == 0 or i == n:
            typer.echo(f"scanned {i}/{n} PDFs", err=True)

    session = _readonly_session()
    try:
        report = build_report(
            session, listing=listing, market_caps=caps, text_provider=provider, progress=progress
        )
    finally:
        session.close()
    out.mkdir(parents=True, exist_ok=True)
    (out / "hygiene.json").write_text(report.to_json(), encoding="utf-8")
    (out / "hygiene.md").write_text(to_markdown(report, top=top), encoding="utf-8")
    for k, v in report.counts.items():
        typer.echo(f"{k:20s} {v}")
    typer.echo(f"wrote {out / 'hygiene.md'}")
