"""Read-only data-hygiene report: duplicate-company matcher and junk-document classifier."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from bursa.db.enums import DocSource, PeriodType, Statement
from bursa.db.models import Company, Concept, Document, ExtractionRun, Fact, Period
from bursa.hygiene import (
    CompanyInfo,
    DocText,
    apply_listing,
    build_report,
    classify_junk,
    dominant_entity,
    entity_matches,
    extract_reg_no,
    find_duplicate_companies,
    name_appears,
    normalize_company_name,
    to_markdown,
)

# --- name normalisation / duplicate matcher --------------------------------


def test_normalize_folds_berhad_punctuation_and_initials() -> None:
    assert normalize_company_name("S P Setia Berhad") == normalize_company_name("SP Setia Bhd.")
    assert normalize_company_name("TIME dotCom Berhad") == normalize_company_name(
        "Time Dotcom Berhad"
    )
    assert normalize_company_name("Top Glove Corporation Bhd.") == normalize_company_name(
        "Top Glove Corporation Berhad"
    )
    assert normalize_company_name("Ajinomoto (Malaysia) Berhad") == "ajinomoto"
    assert normalize_company_name("Hong Leong Bank (M) Berhad") == "hong leong bank"
    assert normalize_company_name("IGB Real Estate Investment Trust") == "igb reit"


def test_find_duplicates_pairs_same_issuer_only() -> None:
    cos = [
        CompanyInfo(1, "3704", "SP Setia Berhad"),
        CompanyInfo(2, "8664", "S P Setia Berhad"),
        CompanyInfo(3, "4008", "Time Dotcom Berhad"),
        CompanyInfo(4, "5031", "TIME dotCom Berhad"),
        CompanyInfo(5, "1155", "Malayan Banking Berhad"),
        CompanyInfo(6, "3794", "Malayan Cement Berhad"),
        CompanyInfo(7, "5211", "Sunway Berhad"),
        CompanyInfo(8, "5263", "Sunway Construction Group Berhad"),
    ]
    pairs = {frozenset((a.stock_code, b.stock_code)) for a, b, _ in find_duplicate_companies(cos)}
    assert pairs == {frozenset({"3704", "8664"}), frozenset({"4008", "5031"})}


def test_apply_listing_marks_wrong_code_and_drops_its_mcap() -> None:
    cos = [
        CompanyInfo(1, "5100", "Hibiscus Petroleum Berhad"),
        CompanyInfo(2, "5199", "Hibiscus Petroleum Bhd"),
    ]
    apply_listing(
        cos,
        {"5100": "BP Plastics Holding Bhd.", "5199": "Hibiscus Petroleum Berhad"},
        {"5100": 0.2, "5199": 1.6},
    )
    assert cos[0].code_status == "OTHER_ISSUER" and cos[0].market_cap_b is None
    assert cos[1].code_status == "OK" and cos[1].market_cap_b == 1.6


# --- junk classifier ----------------------------------------------------------


def _kinds(*args, **kw) -> set[str]:  # type: ignore[no-untyped-def]
    return {v.kind for v in classify_junk(*args, **kw)}


def test_classify_junk_cases() -> None:
    assert _kinds("Heineken_part2_test.pdf", 151, "Heineken Malaysia Berhad") == {"TEST_FILE"}
    assert _kinds("Yinson_test1.pdf", 32, "Yinson Holdings Berhad") == {"TEST_FILE"}
    assert "BROKER_NOTE" in _kinds("PB_Stock-IBHD_EN_20210226.pdf", 4, "I-Berhad")
    assert "BROKER_NOTE" in _kinds("RHB Bursa-MidS 2023.pdf", 9, "I-Berhad")
    assert "BROKER_NOTE" in _kinds("2022-05-31_PublicInvest-I-BERHAD-Slow Start.pdf", 4, "I-Berhad")
    assert "BROKER_NOTE" in _kinds(
        "15-IBHD_Bursa-MidS-Initiating-Coverage_20171215_RHB.pdf", 16, "I-Berhad"
    )
    assert "BROKER_NOTE" in _kinds("23-PB_Stock-IBHD_EN_20150313.pdf", 27, "I-Berhad")
    assert _kinds("Harn-Len-Key-Statistic-2024-FINAL-1.pdf", 1, "Harn Len Corporation Bhd") == {
        "KEY_STATISTICS"
    }
    assert _kinds("SUSTAINABILITY-STATEMENT-2024.pdf", 48, "X Berhad") == {"SUSTAINABILITY_ONLY"}
    assert "SUBSIDIARY_REPORT" in _kinds(
        "amb-islamic-interim-financial-statements-as-at-31-dec-2025.pdf", 46, "AMMB Holdings Berhad"
    )
    assert "PRESENTATION" in _kinds("FGV-13th-AGM_Presentation-Deck.pdf", 30, "FGV Holdings Berhad")
    assert "CIRCULAR" in _kinds("KJTS-Group-Berhad-Prospectus.pdf", 300, "KJTS Group Berhad")


def test_classify_junk_leaves_real_reports_alone() -> None:
    assert classify_junk("ENG-KAH-AR2023-Bursa-01.pdf", 108, "Eng Kah Corporation Berhad") == []
    assert classify_junk("Sustainability-and-Annual-Report-2024.pdf", 250, "X Berhad") == []
    assert classify_junk("BIMB-AR2024.pdf", 300, "Bank Islam Malaysia Berhad") == []
    # A short document with analyst-report wording is a broker note even without a telltale name.
    head = "Maintain BUY with a target price of RM1.20. Research analyst certification ..."
    assert {"BROKER_NOTE"} <= _kinds("report.pdf", 6, "I-Berhad", head)


def test_text_helpers() -> None:
    assert name_appears("S P Setia Berhad", "ANNUAL REPORT 2024  S P SETIA BERHAD (19740100...)")
    assert name_appears("I-Berhad", "Welcome to I-Berhad annual report")
    assert not name_appears("Tropicana Corporation Berhad", "Sunway Berhad annual report 2024")
    assert (
        extract_reg_no("Registration No. 197401002663 (19698-X) ... 197401002663") == "197401002663"
    )
    assert extract_reg_no("no number here 12345") is None
    text = (
        "AMBANK ISLAMIC BERHAD Registration No. 199401009897 (295576-U) Interim Financial Statements. "
        "Share registrar: Boardroom Share Registrars Sdn Bhd 199601006647. "
        "Holding company AMMB Holdings Berhad. AmBank Islamic Berhad notes. AmBank Islamic Berhad."
    )
    ent = dominant_entity(text)
    assert ent is not None and ent.key == "ambank islamic" and ent.reg_no == "199401009897"
    assert not entity_matches(ent.key, "AMMB Holdings Berhad")
    assert entity_matches("sp setia", "S P Setia Berhad")
    assert entity_matches("ioi corp", "IOI Corporation Berhad")


# --- end to end on a synthetic DB --------------------------------------------


def _doc(session: Session, company: Company, name: str, pages: int, sha: str) -> Document:
    d = Document(
        company_id=company.id,
        source=DocSource.UPLOAD,
        original_filename=name,
        file_sha256=sha * 64,
        file_size=1,
        storage_path=f"/x/{sha}.pdf",
        page_count=pages,
    )
    session.add(d)
    session.flush()
    return d


def test_build_report_end_to_end(session: Session) -> None:
    session.add(
        Concept(concept_key="is.revenue", statement=Statement.INCOME_STATEMENT, label="Revenue")
    )
    fake = Company(stock_code="3704", name="SP Setia Berhad")
    real = Company(stock_code="8664", name="S P Setia Berhad")
    ib = Company(stock_code="4251", name="I-Berhad")
    session.add_all([fake, real, ib])
    session.flush()
    ar = _doc(session, fake, "setia-ar2024.pdf", 200, "a")
    note = _doc(session, ib, "PB_Stock-IBHD_EN_20210226.pdf", 4, "b")
    wrong = _doc(session, ib, "ar2023.pdf", 150, "c")
    for d in (ar, note, wrong):
        session.add(ExtractionRun(document_id=d.id))
    session.flush()
    run = session.query(ExtractionRun).filter_by(document_id=ar.id).one()
    for fy in (2021, 2023):
        p = Period(
            company_id=fake.id,
            period_start=date(fy, 1, 1),
            period_end=date(fy, 12, 31),
            period_type=PeriodType.FY,
            fiscal_year=fy,
        )
        session.add(p)
        session.flush()
        session.add(
            Fact(
                company_id=fake.id,
                concept_key="is.revenue",
                period_id=p.id,
                value=Decimal(1),
                reported_in_document_id=ar.id,
                run_id=run.id,
            )
        )
    session.flush()

    texts = {
        ar.id: "S P SETIA BERHAD annual report 2024 Registration No. 197401002663 " * 20,
        note.id: "I-Berhad target price research analyst " * 20,
        wrong.id: "SP SETIA BERHAD annual report 2023 Registration No. 197401002663 " * 20,
    }

    def provider(doc_id: int, path: str, full: bool) -> DocText:
        t = texts[doc_id]
        return DocText(head=t, pages_read=10, page_count=100, full=t if full else None)

    report = build_report(
        session,
        listing={"8664": "S P Setia Berhad", "4251": "I-Berhad"},
        market_caps={"8664": 3.6},
        text_provider=provider,
    )
    by_cat: dict[str, list] = {}
    for i in report.issues:
        by_cat.setdefault(i.category, []).append(i)

    dup = by_cat["DUPLICATE_COMPANY"][0]
    assert "merge 3704 into 8664" in dup.proposed_fix and dup.certain
    assert dup.market_cap_b == 3.6
    assert any(i.document_ids == [note.id] for i in by_cat["JUNK_DOCUMENT"])
    mism = by_cat["NAME_MISMATCH"]
    assert [i.document_ids for i in mism] == [[wrong.id]]
    assert "sp setia" in mism[0].what  # names the entity the report is actually about
    assert mism[0].severity == "MEDIUM"
    assert by_cat["FY_GAP"][0].what.endswith("2022")
    assert {i.stock_codes[0] for i in by_cat["NO_FACTS_COMPANY"]} == {"4251"}
    assert "WRONG_CODE" not in by_cat  # 3704 is covered by the duplicate pair
    md = to_markdown(report)
    assert "## Top" in md and "DUPLICATE_COMPANY" in md
