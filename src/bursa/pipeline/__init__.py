"""Pipeline stages.

Every stage is a plain function taking a ``Session``. Prefect flows in
``bursa.flows`` only orchestrate them, so the whole pipeline is runnable and
testable without an orchestrator installed.
"""

from bursa.pipeline.ingest import ingest_file, parse_filename, scan_inbox

__all__ = ["ingest_file", "parse_filename", "scan_inbox"]
