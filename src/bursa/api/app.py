"""FastAPI application for the Bursa Equity Pipeline."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from bursa.api.routes import (
    analysis,
    benchmark,
    companies,
    dividends,
    facts,
    fiveyear,
    peers,
    progress,
    upload,
    validation,
    valuation,
)

app = FastAPI(
    title="Bursa Equity Pipeline",
    description="Financial data extraction and analysis for Bursa Malaysia listed companies.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(companies.router, prefix="/api/companies", tags=["companies"])
app.include_router(facts.router, prefix="/api/facts", tags=["facts"])
app.include_router(upload.router, prefix="/api/upload", tags=["upload"])
app.include_router(validation.router, prefix="/api/validation", tags=["validation"])
app.include_router(benchmark.router, prefix="/api/benchmark", tags=["benchmark"])
app.include_router(valuation.router, prefix="/api/valuation", tags=["valuation"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["analysis"])
app.include_router(progress.router, prefix="/api/progress", tags=["progress"])
app.include_router(peers.router, prefix="/api/peers", tags=["peers"])
app.include_router(dividends.router, prefix="/api/dividends", tags=["dividends"])
app.include_router(fiveyear.router, prefix="/api/fiveyear", tags=["fiveyear"])

DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(DASHBOARD)


@app.get("/api/status")
def pipeline_status():
    """Pipeline health at a glance."""
    from sqlalchemy import func, select

    from bursa.db.models import Company, Concept, ConceptSynonym, Document
    from bursa.db.session import session_scope

    with session_scope() as session:
        return {
            "concepts": session.scalar(select(func.count(Concept.concept_key))) or 0,
            "synonyms": session.scalar(select(func.count(ConceptSynonym.id))) or 0,
            "companies": session.scalar(select(func.count(Company.id))) or 0,
            "documents": session.scalar(select(func.count(Document.id))) or 0,
        }
