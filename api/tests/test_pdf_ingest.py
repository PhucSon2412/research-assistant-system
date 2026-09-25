from __future__ import annotations

import io
import subprocess
import uuid
from datetime import timedelta

import pytest
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from sqlalchemy.orm import Session

from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.config import PdfBodyThreshold, Settings, get_settings
from app.ingest.pdf import Page, PdfError, classify, parse
from app.ingest.worker import _begin, _extract, _validate, claim_job, process_job, utcnow
from app.models import IngestionJob
from app.models import SourceFile
from tests.test_uploads import intent, send, upload_api


def upload_pdf(browser, original: bytes) -> dict:
    created = intent(browser, original, "paper.pdf")
    assert created.status_code == 201, created.text
    result = send(browser, created.json()["intent_token"], original)
    assert result.status_code == 201, result.text
    return result.json()


def pdf_bytes(pages: list[list[str]]) -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792), pageCompression=0)
    for lines in pages:
        y = 750
        for line in lines:
            pdf.drawString(35, y, line)
            y -= 15
        pdf.showPage()
    pdf.save()
    return output.getvalue()


BODY_LINE = "A measured observation with sufficient text for extraction and page coverage."


def full_pdf() -> bytes:
    return pdf_bytes([
        ["Synthetic paper", "Abstract", "This is a short abstract."],
        ["Introduction"] + [BODY_LINE] * 25,
        ["Methods"] + [BODY_LINE] * 25,
        ["References", "[1] Example Author. Example Work. 2020.", "[2] Second Author. Other Work. 2021."],
    ])


def partial_pdf() -> bytes:
    return pdf_bytes([
        ["Title", "Abstract", "Brief summary."],
        ["Introduction"] + [BODY_LINE] * 15,
        ["Methods"] + [BODY_LINE] * 15,
        ["Results"] + [BODY_LINE] * 15,
        ["Discussion", "Figure 1. Equation (2)."],
        ["References", "[1] Example citation."],
    ])


def two_column_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), pageCompression=0)
    document.drawString(35, 750, "Introduction")
    for row in range(25):
        y = 730 - row * 23
        document.drawString(35, y, f"Left column observation number {row}.")
        document.drawString(330, y, f"Right column observation number {row}.")
    document.save()
    return output.getvalue()


def visual_heavy_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), pageCompression=0)
    for heading in ("Introduction", "Methods", "Results"):
        document.drawString(35, 750, heading)
        for row in range(8):
            document.rect(40 + row * 20, 500 - row * 30, 100, 20)
            document.drawString(40, 450 - row * 25, f"E = m c^2; figure {row}")
        document.showPage()
    document.save()
    return output.getvalue()


def image_only_pdf() -> bytes:
    bitmap = Image.new("RGB", (200, 300), "white")
    drawing = ImageDraw.Draw(bitmap)
    for row in range(20):
        drawing.line((20, 20 + row * 12, 175, 20 + row * 12), fill="black", width=2)
    out = io.BytesIO()
    document = canvas.Canvas(out, pagesize=(612, 792), pageCompression=0)
    document.drawImage(ImageReader(bitmap), 35, 200, width=500, height=500)
    document.save()
    return out.getvalue()


def test_pdf_classification_threshold_denominator_and_uncertainty(app_env):
    settings = Settings(database_url="postgresql+psycopg://dummy:dummy@localhost/dummy")
    settings.pdf_body = PdfBodyThreshold(min_body_page_ratio=.7, min_chars_per_body_page=100,
                                          min_total_body_chars=2000)
    pages = [Page(1, "Title\nAbstract\nSummary\n", 612, 792),
             Page(2, "Introduction\n" + "a" * 1000, 612, 792),
             Page(3, "b" * 1000, 612, 792),
             Page(4, "c" * 99, 612, 792),
             Page(5, "References\n[1] Citation.\n", 612, 792)]
    result = classify(pages, settings)
    assert result.body_pages == [2, 3, 4]
    assert result.qualifying_pages == [2, 3]
    assert result.deficient_pages == [4]
    assert result.disposition == "fulltext_rejected"  # 2/3 is below 70%
    settings.pdf_body.min_body_page_ratio = 2 / 3
    assert classify(pages, settings).disposition == "awaiting_index"
    assert classify(pages[:1] + pages[-1:], settings).disposition == "needs_review"
    assert classify([Page(1, "", 612, 792)], settings).coverage == "none"
    partial = classify(pages[:3] + [Page(4, "", 612, 792, "page_extract_failed")], settings)
    assert partial.coverage == "partial"
    assert partial.failed_pages == [4]
    interrupted = classify([pages[1], Page(3, "", 612, 792, "page_extract_failed"),
                            Page(4, "Methods\n" + "x" * 1000, 612, 792)], settings)
    assert interrupted.body_pages == [2, 3, 4]
    assert interrupted.deficient_pages == [3]
    assert interrupted.coverage == "partial"


def test_scan_figures_margin_and_two_column_uncertainty(app_env):
    settings = Settings(database_url="postgresql+psycopg://dummy:dummy@localhost/dummy")
    scan = classify([Page(1, "", 612, 792), Page(2, "", 612, 792)], settings)
    assert scan.disposition == "needs_review" and scan.coverage == "none"
    sparse = classify([Page(1, "Introduction\nFigure 1\nEquation (1)\n", 612, 792)], settings)
    assert sparse.disposition == "fulltext_rejected"
    assert sparse.deficient_pages == [1]
    columns = classify([Page(1, "Introduction\nLeft column text  Right column text\n" + BODY_LINE * 30,
                             612, 792)], settings)
    assert columns.disposition == "awaiting_index"
    # This accepts extracted characters; it does not prove visual reading order.
    assert columns.segments[0].text.startswith("Introduction")
    margin = classify([Page(1, "Journal header\nIntroduction\n" + BODY_LINE * 30 + "\n1\n", 612, 792),
                       Page(2, "Journal header\nMethods\n" + BODY_LINE * 30 + "\n2\n", 612, 792)], settings)
    assert "margin_lines_excluded" in margin.warnings
    assert all("Journal header" not in s.text for s in margin.segments if s.kind == "body")


def test_parser_real_text_multpage_and_corruption(app_env, monkeypatch):
    original = full_pdf()
    settings = Settings(database_url="postgresql+psycopg://dummy:dummy@localhost/dummy")
    pages = parse(original, 4, settings)
    assert len(pages) == 4
    assert "Introduction" in pages[1].text
    assert "Methods" in pages[2].text
    assert pages[1].width == 612
    columns = parse(two_column_pdf(), 1, settings)
    assert columns[0].layout_mode == "columns_reordered"
    assert columns[0].text.index("Left column observation number 24") < columns[0].text.index("Right column observation number 0")
    visual_pages = parse(visual_heavy_pdf(), 3, settings)
    visual_result = classify(visual_pages, settings)
    assert visual_result.disposition == "fulltext_rejected"
    assert visual_result.total_body_chars < settings.pdf_body.min_total_body_chars
    scan_result = classify(parse(image_only_pdf(), 1, settings), settings)
    assert scan_result.disposition == "needs_review"
    assert scan_result.coverage == "none"
    assert "pages_without_text" in scan_result.warnings
    try:
        parse(original, 3, settings)
        assert False
    except PdfError as exc:
        assert exc.code == "pdf_page_mismatch"
    try:
        parse(b"%PDF-invalid", 1, settings)
        assert False
    except PdfError as exc:
        assert exc.code == "pdf_invalid"
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("parser", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    try:
        parse(original, 4, settings)
        assert False
    except PdfError as exc:
        assert exc.code == "pdf_parser_timeout" and exc.retryable
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], -9))
    try:
        parse(original, 4, settings)
        assert False
    except PdfError as exc:
        assert exc.code == "pdf_parser_crash" and exc.retryable


def test_pdf_upload_worker_locator_references_coverage_and_acl(upload_api):
    api, storage = upload_api
    api.bootstrap_admin()
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    original = full_pdf()
    ids = upload_pdf(a, original)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    item = a.get(f"/library/items/{ids['item_id']}").json()["item"]
    assert item["content_status"] == "metadata_only"
    assert item["versions"][0]["coverage"] == "complete"
    assert item["jobs"][0]["extraction_finished_at"] is not None
    assert item["jobs"][0]["finished_at"] is None
    coverage = a.get(f"/library/items/{ids['item_id']}/coverage").json()["pages"]
    assert [p["page"] for p in coverage if p["body_chars"]] == [2, 3]
    assert all(p["qualifies"] for p in coverage if p["body_chars"])
    refs = a.get(f"/library/items/{ids['item_id']}/references").json()["references"]
    assert len(refs) == 2
    assert all(r["evidence_kind"] == "bibliography_entry" for r in refs)
    chunks = a.get(f"/library/items/{ids['item_id']}/chunks?limit=100").json()["chunks"]
    assert chunks
    assert {chunk["section_kind"] for chunk in chunks} == {"abstract", "body", "references"}
    pages = parse(original, 4, get_settings())
    for chunk in chunks:
        detail = chunk["locator_detail"]
        assert chunk["text"] == pages[chunk["start_page"] - 1].text[detail["start_offset"]:detail["end_offset"]]
        assert chunk["source_file_id"] == ids["file_id"]
        assert detail["geometry"] is None
    for ref in refs:
        assert ref["raw_citation"] == pages[ref["page"] - 1].text[ref["start_offset"]:ref["end_offset"]]
    assert a.get(f"/files/{ids['file_id']}/original").content == original
    for other in (b, admin):
        for suffix in ("", "/coverage", "/references", "/chunks"):
            assert other.get(f"/library/items/{ids['item_id']}{suffix}").status_code == 404


def test_rejected_pdf_keeps_original_and_no_chunks(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload_pdf(a, pdf_bytes([["Cover", "Abstract", "Summary"],
                               ["References", "[1] Source."]]))
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "needs_review"
    item = a.get(f"/library/items/{ids['item_id']}").json()["item"]
    assert item["content_status"] == "metadata_only"
    assert item["jobs"][0]["finished_at"] is not None
    assert a.get(f"/library/items/{ids['item_id']}/chunks").json()["chunks"] == []
    assert len(a.get(f"/library/items/{ids['item_id']}/references").json()["references"]) == 1


def test_partial_pdf_passes_with_explicit_deficient_body_page(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload_pdf(a, partial_pdf())
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    item = a.get(f"/library/items/{ids['item_id']}").json()["item"]
    assert item["versions"][0]["coverage"] == "partial"
    assert "body_pages_below_min_chars" in item["jobs"][0]["warnings"]
    coverage = a.get(f"/library/items/{ids['item_id']}/coverage").json()["summary"][0]
    assert coverage["body_pages"] == 4 and coverage["qualifying_pages"] == 3
    assert coverage["deficient_pages"] == 1


@pytest.mark.parametrize("revoke_kind", ["epoch", "cancel"])
def test_pdf_epoch_or_cancel_during_parser_discards_derivatives(upload_api, monkeypatch, revoke_kind):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload_pdf(a, full_pdf())
    from app.ingest import pdf as parser
    actual = parser.parse

    def revoke(*args):
        pages = actual(*args)
        with api.engine.begin() as conn:
            if revoke_kind == "epoch":
                conn.execute(text("UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i"),
                             {"i": uuid.UUID(ids["item_id"])})
            else:
                conn.execute(text("UPDATE ingestion_jobs SET cancel_requested=true, cancel_requested_at=now() WHERE id=:i"),
                             {"i": uuid.UUID(ids["job_id"])})
        return pages

    monkeypatch.setattr(parser, "parse", revoke)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "cancelled"
    assert api.scalar("SELECT count(*) FROM pdf_pages WHERE item_id=:i", i=uuid.UUID(ids["item_id"])) == 0


def test_pdf_timeout_retry_and_owner_scoped_duplicate(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b = api.user_browser("a@example.org"), api.user_browser("b@example.org")
    original = full_pdf()
    first, same, other = upload_pdf(a, original), upload_pdf(a, original), upload_pdf(b, original)
    from app.ingest import pdf as parser
    actual = parser.parse

    def timed_out(*_args):
        raise PdfError("pdf_parser_timeout", retryable=True)

    monkeypatch.setattr(parser, "parse", timed_out)
    assert process_job(uuid.UUID(first["job_id"]), storage=storage) == "pdf_parser_timeout"
    assert api.scalar("SELECT state FROM ingestion_jobs WHERE id=:i", i=uuid.UUID(first["job_id"])) == "retryable_failed"
    monkeypatch.setattr(parser, "parse", actual)
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET next_attempt_at=now()-interval '1 second' WHERE id=:i"),
                     {"i": uuid.UUID(first["job_id"])})
    assert process_job(uuid.UUID(first["job_id"]), storage=storage) == "awaiting_index"
    assert process_job(uuid.UUID(same["job_id"]), storage=storage) == "duplicate"
    assert process_job(uuid.UUID(other["job_id"]), storage=storage) == "awaiting_index"
    assert api.scalar("SELECT count(*) FROM reference_entries WHERE item_id=:i", i=uuid.UUID(first["item_id"])) == 2
    assert api.scalar("SELECT count(*) FROM reference_entries WHERE item_id=:i", i=uuid.UUID(same["item_id"])) == 0


def test_pdf_original_checksum_is_rechecked_by_worker(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    original = full_pdf()
    ids = upload_pdf(a, original)
    with Session(api.engine) as db:
        key = db.get(SourceFile, uuid.UUID(ids["file_id"])).storage_key
    altered = original[:-1] + (b"X" if original[-1:] != b"X" else b"Y")
    storage.put(key, io.BytesIO(altered), "application/pdf")
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "checksum_mismatch"
    assert api.scalar("SELECT state FROM ingestion_jobs WHERE id=:i", i=uuid.UUID(ids["job_id"])) == "terminal_failed"
    assert api.scalar("SELECT count(*) FROM pdf_pages WHERE item_id=:i", i=uuid.UUID(ids["item_id"])) == 0


@pytest.mark.parametrize("stage", ["extract", "chunk"])
def test_pdf_worker_lost_before_step_commit_replays_once(upload_api, stage):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    ids = upload_pdf(a, full_pdf())
    job_id = uuid.UUID(ids["job_id"])
    settings = get_settings()
    with Session(api.engine) as db:
        token = claim_job(db, job_id, settings)
        source = _begin(db, job_id, token, "validate")
        _validate(db, job_id, token, source, storage, settings)
        _begin(db, job_id, token, "extract")
        if stage == "chunk":
            _extract(db, job_id, token, source, storage, settings)
            _begin(db, job_id, token, "chunk")
        job = db.get(IngestionJob, job_id)
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_job(job_id, storage=storage) == "awaiting_index"
    assert api.scalar("SELECT count(*) FROM pdf_pages WHERE item_id=:i", i=uuid.UUID(ids["item_id"])) == 4
    assert api.scalar("SELECT count(*) FROM reference_entries WHERE item_id=:i", i=uuid.UUID(ids["item_id"])) == 2
    assert api.scalar("SELECT count(*) FROM ingest_step_attempts WHERE job_id=:i AND error_code='worker_lost'", i=job_id) == 1
