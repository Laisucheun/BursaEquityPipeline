"""Record a long-running job's progress in `job_progress` for the dashboard.

Uses its own session, committed on every update. Call `step()` only between
units of work - after the work session has committed - so it never waits on
the work transaction's SQLite write lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from bursa.db.enums import RunStatus
from bursa.db.models import JobProgress
from bursa.db.session import get_engine, get_sessionmaker


class JobTracker:
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        # Per-item failures that didn't abort the job; saved on completion.
        self.errors: list[str] = []

    def _update(self, **fields: object) -> None:
        with get_sessionmaker()() as s:
            job = s.get(JobProgress, self.job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            s.commit()

    def step(self, done: int, current: str | None) -> None:
        self._update(done=done, current=current)


@contextmanager
def track_job(kind: str, total: int) -> Iterator[JobTracker]:
    JobProgress.__table__.create(get_engine(), checkfirst=True)
    with get_sessionmaker()() as s:
        job = JobProgress(kind=kind, total=total)
        s.add(job)
        s.commit()
        tracker = JobTracker(job.id)
    try:
        yield tracker
    except BaseException as exc:
        tracker._update(
            status=RunStatus.FAILED, error=repr(exc)[:2000], finished_at=datetime.now(UTC)
        )
        raise
    tracker._update(
        status=RunStatus.SUCCEEDED, done=total, current=None, finished_at=datetime.now(UTC),
        error="; ".join(tracker.errors)[:2000] or None,
    )
