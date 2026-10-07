"""One-call snapshot of pipeline progress for the dashboard."""

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import Integer, cast, func, inspect, select

from bursa.db.enums import Basis, PeriodType
from bursa.db.models import (
    BenchmarkResult,
    Company,
    Document,
    ExtractionRun,
    Fact,
    JobProgress,
    Period,
    ValidationResult,
)
from bursa.db.session import get_engine, session_scope

router = APIRouter()

REPO_ROOT = Path(__file__).resolve().parents[4]
README = REPO_ROOT / "README.md"
MANUAL_DOWNLOADS = REPO_ROOT / "manual_downloads.txt"
_MANUAL_ROW = re.compile(
    r"^\s+(LARGE|MID|SMALL|\?)\s+(\S+)\s+(.+?)\s{2,}(\S+)\s+"
    r"(unreachable|no PDF found|never tried|robots blocked)\s+(\S+)\s*$"
)
# Hosts the IR-URL discovery wrongly recorded as a company's IR site.
_BOGUS_URL_HOSTS = ("wa.me", "facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com")


def _manual_downloads(by_code: dict[str, dict]) -> list[dict]:
    if not MANUAL_DOWNLOADS.is_file():
        return []
    out = []
    for line in MANUAL_DOWNLOADS.read_text(encoding="utf-8").splitlines():
        m = _MANUAL_ROW.match(line)
        if not m:
            continue
        tier, code, name, mcap, reason, url = m.groups()
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
        row = by_code.get(code, {})
        out.append({
            "tier": tier if tier != "?" else "UNKNOWN",
            "stock_code": code,
            "name": row.get("name") or name.strip(),
            "market_cap_bn": float(mcap) if mcap.replace(".", "", 1).isdigit() else None,
            "reason": reason,
            "url": url,
            "url_suspect": any(host == h or host.endswith("." + h) for h in _BOGUS_URL_HOSTS),
            "documents": row.get("documents", 0),
            "facts": row.get("facts", 0),
        })
    return out
ACTIVE_WINDOW = timedelta(minutes=10)
_CHECKBOX = re.compile(r"^- \[( |x)\] (.+)$")


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _roadmap() -> list[dict]:
    if not README.is_file():
        return []
    sections: list[dict] = []
    in_roadmap = False
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_roadmap = line.strip() == "## Roadmap"
            continue
        if not in_roadmap:
            continue
        if line.startswith("### "):
            sections.append({"section": line[4:].strip(), "items": []})
        elif (m := _CHECKBOX.match(line.strip())) and sections:
            sections[-1]["items"].append({"done": m.group(1) == "x", "text": m.group(2)})
    return sections


def _jobs(s, now: datetime) -> list[dict]:  # type: ignore[no-untyped-def]
    if not inspect(get_engine()).has_table(JobProgress.__tablename__):
        return []
    out = []
    for j in s.scalars(select(JobProgress).order_by(JobProgress.id.desc()).limit(8)):
        started = _utc(j.started_at)
        end = _utc(j.finished_at) or now
        elapsed = max((end - started).total_seconds(), 0.0)
        rate = j.done / elapsed if elapsed > 0 and j.done else None
        remaining = (j.total - j.done) / rate if rate and j.status == "RUNNING" else None
        stale = j.status == "RUNNING" and now - _utc(j.updated_at) > timedelta(hours=1)
        out.append({
            "id": j.id, "kind": j.kind, "status": "STALLED" if stale else str(j.status),
            "done": j.done, "total": j.total, "current": j.current, "error": j.error,
            "started_at": started.isoformat(), "elapsed_s": round(elapsed),
            "eta_s": round(remaining) if remaining is not None else None,
        })
    return out


@router.get("")
def progress():
    now = datetime.now(UTC)
    with session_scope() as s:
        jobs = _jobs(s, now)
        companies = s.execute(select(Company.id, Company.stock_code, Company.name, Company.sector)).all()

        docs = dict(s.execute(
            select(Document.company_id, func.count(Document.id)).group_by(Document.company_id)
        ).all())
        doc_status = dict(s.execute(
            select(Document.status, func.count(Document.id)).group_by(Document.status)
        ).all())

        coverage: dict[int, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
        for company_id, fy, concept_key in s.execute(
            select(Fact.company_id, Period.fiscal_year, Fact.concept_key)
            .join(Period, Fact.period_id == Period.id)
            .where(
                Fact.basis == Basis.CONSOLIDATED,
                Period.period_type.in_((PeriodType.FY, PeriodType.INSTANT)),
            )
            .distinct()
        ):
            prefix = concept_key.split(".", 1)[0]
            if prefix in ("is", "bs", "cf"):
                coverage[company_id][prefix].add(fy)

        fact_counts = dict(s.execute(
            select(Fact.company_id, func.count(Fact.id)).group_by(Fact.company_id)
        ).all())
        last_activity = {
            cid: _utc(ts) for cid, ts in s.execute(
                select(Fact.company_id, func.max(Fact.updated_at)).group_by(Fact.company_id)
            )
        }

        validation: dict[int, tuple[int, int]] = {}
        for cid, total, passed in s.execute(
            select(Period.company_id, func.count(ValidationResult.id),
                   func.sum(cast(ValidationResult.passed, Integer)))
            .join(Period, ValidationResult.period_id == Period.id)
            .group_by(Period.company_id)
        ):
            validation[cid] = (total, passed or 0)

        bench: dict[int, dict[str, int]] = defaultdict(dict)
        for cid, cls, n in s.execute(
            select(BenchmarkResult.company_id, BenchmarkResult.classification,
                   func.count(BenchmarkResult.id))
            .group_by(BenchmarkResult.company_id, BenchmarkResult.classification)
        ):
            bench[cid][cls] = n

        recent_runs = s.execute(
            select(ExtractionRun.id, ExtractionRun.created_at, ExtractionRun.status,
                   Document.original_filename, Company.stock_code, Company.name)
            .join(Document, ExtractionRun.document_id == Document.id)
            .outerjoin(Company, Document.company_id == Company.id)
            .order_by(ExtractionRun.id.desc())
            .limit(15)
        ).all()
        run_facts = dict(s.execute(
            select(Fact.run_id, func.count(Fact.id))
            .where(Fact.run_id.in_([r.id for r in recent_runs]))
            .group_by(Fact.run_id)
        ).all()) if recent_runs else {}
        runs_recent_window = s.scalar(
            select(func.count(ExtractionRun.id)).where(
                ExtractionRun.created_at >= (now - ACTIVE_WINDOW).replace(tzinfo=None)
            )
        ) or 0

        rows = []
        for cid, code, name, sector in companies:
            cov = coverage.get(cid, {})
            total_v, passed_v = validation.get(cid, (0, 0))
            years = sorted(set().union(*cov.values())) if cov else []
            rows.append({
                "stock_code": code,
                "name": name,
                "sector": sector,
                "documents": docs.get(cid, 0),
                "facts": fact_counts.get(cid, 0),
                "years": years,
                "statements": {k: sorted(v) for k, v in cov.items()},
                "validation": {"run": total_v, "passed": passed_v},
                "benchmark": bench.get(cid, {}),
                "last_activity": last_activity.get(cid).isoformat() if last_activity.get(cid) else None,
            })

        bench_totals: dict[str, int] = defaultdict(int)
        for per in bench.values():
            for cls, n in per.items():
                bench_totals[cls] += n

        funnel = [
            {"stage": "Watchlist", "count": len(companies)},
            {"stage": "Has documents", "count": sum(1 for r in rows if r["documents"])},
            {"stage": "Has facts", "count": sum(1 for r in rows if r["facts"])},
            {"stage": "All 3 statements", "count": sum(
                1 for r in rows if all(r["statements"].get(k) for k in ("is", "bs", "cf")))},
            {"stage": "Validated", "count": sum(1 for r in rows if r["validation"]["run"])},
            {"stage": "Benchmarked", "count": sum(1 for r in rows if r["benchmark"])},
        ]

        last_run_at = _utc(recent_runs[0].created_at) if recent_runs else None
        return {
            "generated_at": now.isoformat(),
            "totals": {
                "companies": len(companies),
                "documents": sum(docs.values()),
                "facts": sum(fact_counts.values()),
                "periods": s.scalar(select(func.count(Period.id))) or 0,
            },
            "funnel": funnel,
            "jobs": jobs,
            "document_status": {str(k): v for k, v in doc_status.items()},
            "validation": {
                "run": sum(v[0] for v in validation.values()),
                "passed": sum(v[1] for v in validation.values()),
            },
            "benchmark": dict(bench_totals),
            "activity": {
                "active": last_run_at is not None and now - last_run_at < ACTIVE_WINDOW,
                "runs_last_10_min": runs_recent_window,
                "recent": [
                    {
                        "run_id": r.id,
                        "at": _utc(r.created_at).isoformat() if r.created_at else None,
                        "status": str(r.status),
                        "document": r.original_filename,
                        "stock_code": r.stock_code,
                        "company": r.name,
                        "facts": run_facts.get(r.id, 0),
                    }
                    for r in recent_runs
                ],
            },
            "roadmap": _roadmap(),
            "manual_downloads": _manual_downloads({r["stock_code"]: r for r in rows}),
            "companies": rows,
        }
