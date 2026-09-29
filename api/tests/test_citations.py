"""M5b-1 exact arXiv citation links on PostgreSQL/MinIO; no arXiv/OpenRouter traffic."""
from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.citations import MATCHER_VERSION, normalize_arxiv_id, parse_citation, reconcile_pending, reconcile_references
from app.config import CitationPolicy, DiscoveryPolicy, Settings, get_settings
from app.discovery import _write_page, parse_page, run_sync
from app.ingest.embedding import EmbeddingResult
from app.ingest.worker import StepError, process_job
from app.main import app
from app.models import ReferenceEntry
from app.rag import GenerationResult
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import BODY_LINE, pdf_bytes, upload_pdf
from tests.test_private_index import fake_provider, settings_with_fake_key
from tests.test_search import indexed
from tests.test_uploads import upload_api  # noqa: F401  (fixture)

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def oai_page(records=(), deleted=()) -> bytes:
    parts = []
    for rid, title, versions, stamp in records:
        history = "".join(f'<version version="v{n}"><date>Mon, {20 + n} Sep 2026 10:00:00 GMT</date></version>'
                          for n in range(1, versions + 1))
        parts.append(f'<record><header><identifier>oai:arXiv.org:{rid}</identifier><datestamp>{stamp}</datestamp>'
                     f'</header><metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/"><id>{rid}</id>{history}'
                     f'<title>{title}</title><authors>Synthetic Author</authors><categories>cs.AI</categories>'
                     f'<abstract>ABSTRACTCANARY {rid}</abstract></arXivRaw></metadata></record>')
    for rid, stamp in deleted:
        parts.append(f'<record><header status="deleted"><identifier>oai:arXiv.org:{rid}</identifier>'
                     f'<datestamp>{stamp}</datestamp></header></record>')
    return (f'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>{"".join(parts)}'
            '<resumptionToken /></ListRecords></OAI-PMH>').encode()


def harvest(engine, page: bytes, now: datetime = NOW) -> dict:
    policy = DiscoveryPolicy(topics=["cs.AI"], max_pages_per_topic=1)
    result = run_sync(engine, policy, fetcher=lambda *_: page, now=now, force=True)
    assert result["busy"] is False and result["pages"] == 1, result
    return result


@pytest.fixture(autouse=True)
def _no_arxiv_clock(monkeypatch):
    monkeypatch.setattr("app.discovery._reserve_request", lambda db, policy: None)


SEED = [("2609.00011", "Exact Linking Study", 1, "2026-09-28"),
        ("2609.00012", "Versioned Work", 3, "2026-09-28"),
        ("2609.00013", "Second Merged Work", 1, "2026-09-28"),
        ("2609.00015", "Withdrawn Record", 1, "2026-09-28"),
        ("2609.00018", "DOI Form Target", 1, "2026-09-28"),
        ("2609.00091", "Near Identical Target", 1, "2026-09-28")]
LATE = ("2609.00014", "Late Arriving Metadata Paper", 1, "2026-09-28")

# (raw citation, status after seeding, arXiv ID, cited version, true target present in discovery)
MD_REFS = [
    ("A. Alpha. Exact Linking Study. arXiv:2609.00011, 2026.", "linked", "2609.00011", None, "2609.00011"),
    ("B. Beta. Versioned Work. arXiv:2609.00012v2 [cs.CL], 2026.", "linked", "2609.00012", "v2", "2609.00012"),
    ("C. Gamma. Journal Paper. doi:10.1145/3292500.3330701, 2019.", "no_arxiv_id", None, None, None),
    ("D. Delta. Merged entry. arXiv:2609.00011; E. Eps. arXiv:2609.00013.", "multiple_arxiv_ids", None, None, None),
    ("F. Phi. Near miss. arXiv:2609.000111, 2026.", "invalid_arxiv_id", None, None, None),
    ("G. Chi. Month thirteen. arXiv:2613.00001.", "invalid_arxiv_id", None, None, None),
    ("H. Psi. Bare number 2609.00011 without context.", "bare_identifier", None, None, "2609.00011"),
    ("I. Omega. Late Arriving Metadata Paper. 2026.", "no_arxiv_id", None, None, "2609.00014"),
    ("J. Kappa. Late Arriving Metadata Paper. https://arxiv.org/abs/2609.00014v1", "no_discovery_record",
     "2609.00014", "v1", "2609.00014"),
    ("K. Lambda. Withdrawn Record. arXiv:2609.00015.", "linked", "2609.00015", None, "2609.00015"),
    ("L. Mu. Conflicting. arXiv:2609.00016v1, https://arxiv.org/abs/2609.00016v2", "conflicting_versions",
     None, None, None),
    ("M. Nu. Broken extraction arXiv:2609.000\n17 split by the parser.", "invalid_arxiv_id", None, None, None),
    ("N. Xi. Old style. arXiv:hep-th/9901001.", "no_discovery_record", "hep-th/9901001", None, None),
    ("O. Omicron. DOI form 10.48550/arXiv.2609.00018.", "linked", "2609.00018", None, "2609.00018"),
    ("P. Pi. Near-identical ID arXiv:2609.00019.", "no_discovery_record", "2609.00019", None, None),
]
BODY = "## Methods\nSynthetic result is seven units.\n"


def markdown(refs=MD_REFS, body=BODY) -> bytes:
    lines = "".join(f"[{i}] {raw}\n" for i, (raw, *_rest) in enumerate(refs, 1))
    return f"# Citation Fixture\n\n{body}\n## References\n{lines}".encode()


def refs_of(browser, item_id):
    response = browser.get(f"/library/items/{item_id}/references")
    assert response.status_code == 200, response.text
    return response.json()["references"]


def paper_id(api, arxiv_id):
    return str(api.scalar("SELECT id FROM discovery_papers WHERE remote_id=:r", r=arxiv_id))


def tally(refs, expected) -> dict:
    """Correct/wrong links and unmatched references against fixture labels."""
    correct = wrong = 0
    unmatched: dict[str, int] = {}
    for ref, (*_, truth) in zip(refs, expected):
        link = ref["citation_link"]
        if link["linked"]:
            if link["target"]["arxiv_id"] == truth:
                correct += 1
            else:
                wrong += 1
        else:
            unmatched[link["status"]] = unmatched.get(link["status"], 0) + 1
    return {"references": len(refs), "linked_correct": correct, "linked_wrong": wrong,
            "unmatched": sum(unmatched.values()), "unmatched_by_status": dict(sorted(unmatched.items())),
            "unmatched_with_true_target": sum(1 for ref, (*_, truth) in zip(refs, expected)
                                                      if not ref["citation_link"]["linked"] and truth)}


def two_users(api):
    api.bootstrap_admin()
    api.create_user("cite-a@example.org")
    api.create_user("cite-b@example.org")
    return api.user_browser("cite-a@example.org"), api.user_browser("cite-b@example.org"), api.admin_browser()


# ---------------------------------------------------------------- unit


@pytest.mark.parametrize("raw,status,arxiv_id,version,form", [
    ("Vaswani et al. arXiv:1706.03762v5 [cs.CL], 2017.", "exact", "1706.03762", "v5", "arxiv_prefix"),
    ("arXiv preprint arXiv:1810.04805, 2018.", "exact", "1810.04805", None, "arxiv_prefix"),
    ("ARXIV: 2005.14165.", "exact", "2005.14165", None, "arxiv_prefix"),
    ("https://arxiv.org/pdf/1706.03762v2.pdf", "exact", "1706.03762", "v2", "arxiv_url"),
    ("http://export.arxiv.org/abs/1706.03762?context=cs#top", "exact", "1706.03762", None, "arxiv_url"),
    ("See arXiv:1706.03762: an overview.", "exact", "1706.03762", None, "arxiv_prefix"),
    ("Available on arXiv: https://arxiv.org/abs/hep-th/9901001v1.", "exact", "hep-th/9901001", "v1", "arxiv_url"),
    ("arXiv:math.GT/0309136", "exact", "math/0309136", None, "arxiv_prefix"),
    ("doi:10.48550/arXiv.2005.14165", "exact", "2005.14165", None, "arxiv_doi"),
    ("CoRR, abs/1810.04805, 2018.", "exact", "1810.04805", None, "abs_path"),
    ("ArXiv, abs/2005.14165.", "exact", "2005.14165", None, "abs_path"),
    ("arXiv:0704.0001", "exact", "0704.0001", None, "arxiv_prefix"),
    ("arXiv:1706.03762 and https://arxiv.org/abs/1706.03762v3", "exact", "1706.03762", "v3", "arxiv_prefix"),
    ("Same ID bare 1706.03762 and arXiv:1706.03762", "exact", "1706.03762", None, "arxiv_prefix"),
    ("doi:10.1145/3292500.3330701", "no_arxiv_id", None, None, None),
    ("doi:10.18653/v1/2020.acl-main.463", "no_arxiv_id", None, None, None),
    ("Attention Is All You Need. NeurIPS 2017.", "no_arxiv_id", None, None, None),
    ("arXiv preprint, 2017.", "no_arxiv_id", None, None, None),
    ("see 1706.03762 for details", "bare_identifier", None, None, None),
    ("arXiv:1706.03762 and arXiv:1810.04805", "multiple_arxiv_ids", None, None, None),
    ("arXiv:1706.03762; also 1810.04805", "multiple_arxiv_ids", None, None, None),
    ("arXiv:1706.03762v1, https://arxiv.org/abs/1706.03762v2", "conflicting_versions", None, None, None),
    ("arXiv:1706.037621", "invalid_arxiv_id", None, None, None),
    ("arXiv:1706.0376", "invalid_arxiv_id", None, None, None),
    ("arXiv:1501.0001", "invalid_arxiv_id", None, None, None),
    ("arXiv:0703.1234", "invalid_arxiv_id", None, None, None),
    ("arXiv:1713.00001", "invalid_arxiv_id", None, None, None),
    ("arXiv:1706.03762v0", "invalid_arxiv_id", None, None, None),
    ("arXiv:17O6.03762", "invalid_arxiv_id", None, None, None),
    ("arXiv:1706.\n03762", "invalid_arxiv_id", None, None, None),
    ("arXiv:hep-th/0813001", "invalid_arxiv_id", None, None, None),
    ("arXiv:1706.03762 and arXiv:1706.037", "invalid_arxiv_id", None, None, None),
])
def test_parse_exact_arxiv_id(raw, status, arxiv_id, version, form):
    parsed = parse_citation(raw)
    assert (parsed.status, parsed.arxiv_id, parsed.cited_version, parsed.id_form) == (status, arxiv_id, version, form)
    if parsed.arxiv_id:  # the span locates one cited occurrence inside the raw text
        assert normalize_arxiv_id(raw[parsed.start:parsed.end])[0] == arxiv_id


# Look-alike contexts: "\b" matches after "-" and ".", so v1 accepted these (review PR #16).
LOOKALIKES = [
    "https://evil-arxiv.org/abs/1706.03762",
    "fake-arXiv:1706.03762",
    "fake-CoRR abs/1706.03762",
    "fake.arXiv:1706.03762",
    "fake/arXiv:1706.03762",
    "fake-ArXiv, abs/1706.03762",
    "evil-arxiv.org/pdf/1706.03762v2.pdf",
    "https://arxiv.org.evil.com/abs/1706.03762",
    "https://notarxiv.org/abs/1706.03762",
    "https://arxiv.org@evil.com/abs/1706.03762",
    "https://x@arxiv.org/abs/1706.03762",
    "https://arxiv.org:8443/abs/1706.03762",
    "https://evil.org/?u=https://arxiv.org/abs/1706.03762",
    "https://evil.org/arxiv.org/abs/1706.03762",
    "https://openreview.net/pdf/1706.03762",
    "evil-doi:10.48550/arXiv.1706.03762",
    "https://evil-doi.org/10.48550/arXiv.1706.03762",
    "fake-10.48550/arXiv.1706.03762",
]


@pytest.mark.parametrize("raw", LOOKALIKES)
def test_lookalike_arxiv_contexts_never_match_exactly(raw):
    parsed = parse_citation(raw)
    assert parsed.status == "bare_identifier" and parsed.arxiv_id is None, parsed


@pytest.mark.parametrize("raw,status,arxiv_id", [
    # A look-alike carrying a different ID makes a genuine one ambiguous.
    ("arXiv:1706.03762; mirror https://evil-arxiv.org/abs/1810.04805", "multiple_arxiv_ids", None),
    ("fake-arXiv:1810.04805 then arXiv:1706.03762", "multiple_arxiv_ids", None),
    # The same ID repeated in a look-alike adds no second candidate.
    ("arXiv:1706.03762, mirror https://evil-arxiv.org/abs/1706.03762", "exact", "1706.03762"),
    # Genuine contexts after explicit separators still match.
    ("URL:https://arxiv.org/abs/1706.03762", "exact", "1706.03762"),
    ("(https://www.arxiv.org/pdf/1706.03762v2.pdf)", "exact", "1706.03762"),
    ("<https://arxiv.org/abs/1706.03762>", "exact", "1706.03762"),
    ("https://doi.org/10.48550/arXiv.2005.14165", "exact", "2005.14165"),
    ("https://dx.doi.org/10.48550/arXiv.2005.14165", "exact", "2005.14165"),
    ("DOI 10.48550/arXiv.2005.14165", "exact", "2005.14165"),
    ("In: CoRR abs/1810.04805 (2018)", "exact", "1810.04805"),
    ("Vaswani et al., “arXiv:1706.03762”.", "exact", "1706.03762"),
    ("«arXiv:1706.03762»", "exact", "1706.03762"),
])
def test_genuine_and_mixed_contexts(raw, status, arxiv_id):
    parsed = parse_citation(raw)
    assert (parsed.status, parsed.arxiv_id) == (status, arxiv_id), parsed


def test_lookalike_references_create_no_exact_edge(upload_api):
    """End to end: the cited ID exists in discovery, yet no exact edge is created."""
    api, storage = upload_api
    a, _, _ = two_users(api)
    harvest(api.engine, oai_page([("1706.03762", "Look-alike Target", 5, "2026-09-28")]))
    refs = [(raw, "bare_identifier", None, None, "1706.03762") for raw in LOOKALIKES[:3]]
    refs.append(("Genuine control. arXiv:1706.03762v5.", "linked", "1706.03762", "v5", "1706.03762"))
    ids = upload(a, markdown(refs))
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    got = refs_of(a, ids["item_id"])
    assert [(r["citation_link"]["status"], r["citation_link"]["arxiv_id"]) for r in got] == [
        ("bare_identifier", None)] * 3 + [("linked", "1706.03762")]
    assert all(r["citation_link"]["target"] is None and r["citation_link"]["method"] is None for r in got[:3])
    assert api.scalar("SELECT count(*) FROM reference_citation_links WHERE arxiv_id IS NOT NULL") == 1
    citing = a.get(f"/discovery/papers/{paper_id(api, '1706.03762')}/citing-items").json()
    assert (citing["total"], citing["items"][0]["linked_references"]) == (1, 1)
    assert citing["relation"] == "cites"  # a bibliography relation, never content evidence


def test_citation_policy_config_and_env(monkeypatch):
    assert CitationPolicy() == CitationPolicy(enabled=True, reconcile_poll_seconds=300,
                                              reconcile_batch_size=500, reconcile_max_batches=20)
    for invalid in ({"reconcile_poll_seconds": 29}, {"reconcile_batch_size": 0}, {"reconcile_batch_size": 5001},
                    {"reconcile_max_batches": 0}):
        with pytest.raises(ValidationError):
            CitationPolicy(**invalid)
    monkeypatch.setenv("RAS_DATABASE_URL", "postgresql+psycopg://u:p@localhost/x")
    monkeypatch.setenv("RAS_CITATIONS__RECONCILE_BATCH_SIZE", "7")
    monkeypatch.setenv("RAS_CITATIONS__ENABLED", "false")
    settings = Settings()
    assert settings.citations.reconcile_batch_size == 7 and settings.citations.enabled is False


# ---------------------------------------------------------------- Markdown / PDF fixtures


def test_markdown_fixture_links_late_discovery_tombstone_and_counts(upload_api):
    api, storage = upload_api
    a, _, _ = two_users(api)
    harvest(api.engine, oai_page(SEED))
    ids = upload(a, markdown())
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    refs = refs_of(a, ids["item_id"])
    assert len(refs) == len(MD_REFS)
    original = markdown().decode()
    for ref, (raw, status, arxiv_id, version, _truth) in zip(refs, MD_REFS):
        link = ref["citation_link"]
        # Raw text, locator and version/file provenance are preserved as extracted.
        assert raw in ref["raw_citation"] and original[ref["start_offset"]:ref["end_offset"]] == ref["raw_citation"]
        assert ref["version_id"] == ids["version_id"] and ref["source_file_id"] == ids["file_id"]
        assert ref["evidence_kind"] == "bibliography_entry" and link["relation"] == "cites"
        assert (link["status"], link["arxiv_id"], link["cited_version"]) == (status, arxiv_id, version), raw
        assert link["matcher_version"] == MATCHER_VERSION
        if arxiv_id:
            assert link["method"] == "arxiv_id_exact"
            span = link["id_span"]
            assert arxiv_id.split("/")[-1] in ref["raw_citation"][span["start"]:span["end"]]
        if status == "linked":
            target = link["target"]
            assert target["arxiv_id"] == arxiv_id and target["label"] == "metadata-only"
            assert target["content_status"] == "metadata_only"
            assert target["original_url"] == f"https://arxiv.org/abs/{arxiv_id}"
        else:
            assert link["target"] is None
    versioned = refs[1]["citation_link"]
    # The cited version stays the citation's own; latest metadata version is separate.
    assert versioned["target"]["latest_metadata_version"] == "v3"
    assert versioned["target"]["cited_version_url"] == "https://arxiv.org/abs/2609.00012v2"
    assert refs[0]["citation_link"]["target"]["cited_version_url"] is None
    near = refs[14]["citation_link"]
    assert near["target"] is None and near["arxiv_id"] == "2609.00019"
    initial = tally(refs, MD_REFS)
    assert initial == {"references": 15, "linked_correct": 4, "linked_wrong": 0, "unmatched": 11,
                       "unmatched_by_status": {"bare_identifier": 1, "conflicting_versions": 1,
                                               "invalid_arxiv_id": 3, "multiple_arxiv_ids": 1,
                                               "no_arxiv_id": 2, "no_discovery_record": 3},
                       "unmatched_with_true_target": 3}

    # Discovery metadata that arrives after extraction links the waiting exact ID.
    harvest(api.engine, oai_page([LATE]), NOW + timedelta(hours=1))
    refs = refs_of(a, ids["item_id"])
    assert refs[8]["citation_link"]["status"] == "linked"
    assert refs[8]["citation_link"]["target"]["arxiv_id"] == "2609.00014"
    assert refs[7]["citation_link"]["status"] == "no_arxiv_id"  # same title, never linked by title
    late = tally(refs, MD_REFS)
    assert (late["linked_correct"], late["linked_wrong"], late["unmatched"]) == (5, 0, 10)

    # A persistent OAI tombstone is never shown as an available target.
    withdrawn = paper_id(api, "2609.00015")
    assert a.get(f"/discovery/papers/{withdrawn}/citing-items").json()["total"] == 1
    harvest(api.engine, oai_page(deleted=[("2609.00015", "2026-09-29")]), NOW + timedelta(hours=2))
    link = refs_of(a, ids["item_id"])[9]["citation_link"]
    assert (link["status"], link["linked"], link["target"]) == ("discovery_record_deleted", False, None)
    assert a.get(f"/discovery/papers/{withdrawn}/citing-items").status_code == 404
    # Newer metadata supersedes the tombstone, as in the M5a listing.
    harvest(api.engine, oai_page([("2609.00015", "Withdrawn Record", 2, "2026-09-30")]), NOW + timedelta(hours=3))
    assert refs_of(a, ids["item_id"])[9]["citation_link"]["status"] == "linked"
    if os.environ.get("RAS_M5B1_DEMO") == "1":
        print({"markdown_fixture_initial": initial, "markdown_fixture_after_late_discovery": late})


PDF_REFS = [
    ("[1] A. Alpha. Exact Linking Study. arXiv:2609.00011v1, 2026.", "linked", "2609.00011", "v1", "2609.00011"),
    ("[2] C. Gamma. Journal Paper. doi:10.1145/3292500.3330701.", "no_arxiv_id", None, None, None),
    ("[3] D. Delta. Merged. arXiv:2609.00011; arXiv:2609.00013.", "multiple_arxiv_ids", None, None, None),
    ("[4] F. Phi. Near miss arXiv:2609.000111.", "invalid_arxiv_id", None, None, None),
    ("[5] G. Beta. https://arxiv.org/abs/2609.00012v2", "linked", "2609.00012", "v2", "2609.00012"),
    ("[6] H. Late. Late Arriving Metadata Paper. arXiv:2609.00014", "no_discovery_record", "2609.00014",
     None, "2609.00014"),
]


def test_pdf_fixture_links_by_page_locator_and_late_discovery(upload_api):
    api, storage = upload_api
    a, b, admin = two_users(api)
    harvest(api.engine, oai_page(SEED))
    original = pdf_bytes([["Citation PDF", "Abstract", "Short abstract."],
                          ["Introduction"] + [BODY_LINE] * 25,
                          ["Methods"] + [BODY_LINE] * 25,
                          ["References"] + [raw for raw, *_ in PDF_REFS]])
    ids = upload_pdf(a, original)
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    refs = refs_of(a, ids["item_id"])
    assert len(refs) == len(PDF_REFS)
    for ref, (raw, status, arxiv_id, version, _truth) in zip(refs, PDF_REFS):
        assert ref["locator_kind"] == "pdf" and ref["page"] == 4
        assert raw.split("] ", 1)[1].split(".")[0] in ref["raw_citation"]
        assert ref["source_file_id"] == ids["file_id"]
        link = ref["citation_link"]
        assert (link["status"], link["arxiv_id"], link["cited_version"]) == (status, arxiv_id, version), ref
    summary = tally(refs, PDF_REFS)
    assert (summary["linked_correct"], summary["linked_wrong"], summary["unmatched"]) == (2, 0, 4)
    harvest(api.engine, oai_page([LATE]), NOW + timedelta(hours=1))
    assert refs_of(a, ids["item_id"])[5]["citation_link"]["status"] == "linked"
    for other in (b, admin):
        assert other.get(f"/library/items/{ids['item_id']}/references").status_code == 404
    if os.environ.get("RAS_M5B1_DEMO") == "1":
        print({"pdf_fixture": summary})


# ---------------------------------------------------------------- ACL


def test_acl_matrix_same_citation_and_hash_in_two_private_libraries(upload_api):
    api, storage = upload_api
    a, b, admin = two_users(api)
    harvest(api.engine, oai_page(SEED))
    target = paper_id(api, "2609.00011")
    body = markdown([MD_REFS[0]], "## Methods\nPrivate A canary AONLYTITLE.\n")
    ids_a = upload(a, body)
    assert process_job(uuid.UUID(ids_a["job_id"]), storage=storage) == "awaiting_index"
    assert a.patch(f"/library/items/{ids_a['item_id']}", {"title": "AONLYTITLE"}).status_code == 200
    # Before B has its own citing paper, B's count reveals nothing about A.
    for browser in (b, admin):
        empty = browser.get(f"/discovery/papers/{target}/citing-items")
        assert empty.status_code == 200 and empty.json()["total"] == 0 and empty.json()["items"] == []
    ids_b = upload(b, body)  # same bytes and hash, separate private library
    assert process_job(uuid.UUID(ids_b["job_id"]), storage=storage) == "awaiting_index"
    assert api.scalar("SELECT count(*) FROM reference_citation_links WHERE match_status='linked'") == 2

    a_view = a.get(f"/discovery/papers/{target}/citing-items").json()
    b_view = b.get(f"/discovery/papers/{target}/citing-items").json()
    admin_view = admin.get(f"/discovery/papers/{target}/citing-items").json()
    assert (a_view["total"], [i["id"] for i in a_view["items"]]) == (1, [ids_a["item_id"]])
    assert (b_view["total"], [i["id"] for i in b_view["items"]]) == (1, [ids_b["item_id"]])
    assert (admin_view["total"], admin_view["items"]) == (0, [])
    assert a_view["items"][0]["linked_references"] == 1 and a_view["relation"] == "cites"
    # Pagination is applied after the ACL filter, so page two is empty rather than B's item.
    page_two = a.get(f"/discovery/papers/{target}/citing-items?limit=1&offset=1").json()
    assert (page_two["total"], page_two["items"]) == (1, [])
    for view in (b_view, admin_view):
        dumped = json.dumps(view)
        assert ids_a["item_id"] not in dumped and "AONLYTITLE" not in dumped
        assert ids_a["file_id"] not in dumped and ids_a["version_id"] not in dumped
    a_refs = refs_of(a, ids_a["item_id"])
    assert a_refs[0]["citation_link"]["target"]["discovery_paper_id"] == target
    for other in (b, admin):
        for suffix in ("", "/references", "/chunks"):
            denied = other.get(f"/library/items/{ids_a['item_id']}{suffix}")
            assert denied.status_code == 404 and denied.json() == {"error": "not_found"}
        listed = other.get("/library/items").json()
        assert ids_a["item_id"] not in json.dumps(listed)
        assert other.get("/library/items/count").json()["total"] == (1 if other is b else 0)
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        assert a.get(f"/discovery/papers/{bad}/citing-items").status_code == 404
    guest = api.browser()
    assert guest.get(f"/discovery/papers/{target}/citing-items").status_code == 401
    # The discovery listing itself carries no per-user citation counts.
    listing = json.dumps(b.get("/discovery/papers").json())
    assert ids_a["item_id"] not in listing and "citing" not in listing


# ---------------------------------------------------------------- not evidence, not indexed


def test_links_and_bibliography_stay_out_of_search_rag_and_provider_payloads(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, _ = two_users(api)
    harvest(api.engine, oai_page([("2609.00011", "Canary Discovery Title Zeta", 1, "2026-09-28")]))
    settings = settings_with_fake_key()
    settings.generation.pricing_verified = True
    embedded = fake_provider(monkeypatch)
    body = (b"# Evidence Fixture\n\n## Methods\nSynthetic result is seven units.\n\n## References\n"
            b"[1] REFCANARY Author. Canary Discovery Title Zeta. arXiv:2609.00011v1, 2026.\n")
    ids = indexed(api, storage, a, body, settings)
    app.dependency_overrides[get_settings] = lambda: settings
    queries = []

    def fake_query(value, cfg, input_type):
        queries.append(value)
        return EmbeddingResult([0.1] * 3, cfg.embedding.model_id, 2)

    monkeypatch.setattr("app.search.embed", fake_query)
    link = refs_of(a, ids["item_id"])[0]["citation_link"]
    assert link["status"] == "linked" and link["target"]["title"] == "Canary Discovery Title Zeta"
    canaries = ("REFCANARY", "2609.00011", "Canary Discovery Title", "Zeta", "ABSTRACTCANARY")
    assert embedded and not any(c in value for value in embedded for c in canaries)
    assert api.scalar("SELECT count(*) FROM index_entries WHERE text ~ 'REFCANARY|2609|Zeta|ABSTRACTCANARY'") == 0
    for query in ("REFCANARY", "2609.00011", "Zeta", "ABSTRACTCANARY", "Canary Discovery Title"):
        keyword = a.post("/search", {"query": query, "mode": "keyword", "rerank": False})
        assert keyword.status_code == 200 and keyword.json()["count"] == 0, query
        hybrid = a.post("/search", {"query": query, "mode": "hybrid", "rerank": False}).json()
        assert not any(c in json.dumps(hybrid["hits"]) for c in canaries)
    sent = []

    def capture(messages, generation_settings):
        sent.append(json.dumps(messages))
        payload = json.loads(messages[1]["content"])
        # Fake vectors tie, so cite whichever evidence chunk holds the quote.
        first = next(row for row in payload["evidence"] if "seven units" in row["text"])
        return GenerationResult(json.dumps({"claims": [{"text": "The result is seven units.",
            "citations": [{"chunk_id": first["chunk_id"], "quote": "seven units"}]}]}),
            100, 20, generation_settings.generation.model_id, "SyntheticProvider")

    monkeypatch.setattr("app.rag.generate", capture)
    for request in ({"item_id": ids["item_id"], "question": "Synthetic result", "mode": "keyword"},
                    {"item_id": ids["item_id"], "question": "Synthetic result", "mode": "hybrid"},
                    {"scope": "all", "question": "Synthetic result", "mode": "keyword"}):
        answer = a.post("/rag/ask", request)
        assert answer.status_code == 200, answer.text
        data = answer.json()
        assert data["status"] == "answered", data
        for claim in data["claims"]:
            for citation in claim["citations"]:
                opened = a.get(citation["url"].replace("/sources/", "/search/chunks/")).json()
                assert "REFCANARY" not in opened["text"]
        assert not any(c in json.dumps(data) for c in canaries)
    assert len(sent) == 3 and not any(c in payload for payload in sent for c in canaries)
    # Query embeddings carry only the caller's own query text, never bibliography or link data.
    assert queries and set(queries) <= {"REFCANARY", "2609.00011", "Zeta", "ABSTRACTCANARY",
                                        "Canary Discovery Title", "Synthetic result"}


# ---------------------------------------------------------------- idempotency, retry, concurrency


def test_existing_references_backfill_retry_and_stale_source_changes(upload_api, monkeypatch):
    api, storage = upload_api
    a, _, _ = two_users(api)
    harvest(api.engine, oai_page(SEED))
    import app.ingest.worker as worker
    actual = worker.reconcile_references
    calls = {"n": 0}

    def fail_first(*args, **kwargs):
        calls["n"] += 1
        result = actual(*args, **kwargs)
        if calls["n"] == 1:
            raise StepError("storage_unavailable", retryable=True)
        return result

    monkeypatch.setattr(worker, "reconcile_references", fail_first)
    ids = upload(a, markdown())
    job = uuid.UUID(ids["job_id"])
    assert process_job(job, storage=storage) != "awaiting_index"
    assert api.scalar("SELECT count(*) FROM reference_entries") == 0
    assert api.scalar("SELECT count(*) FROM reference_citation_links") == 0
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET next_attempt_at=now()-interval '1 second' WHERE id=:i"), {"i": job})
    assert process_job(job, storage=storage) == "awaiting_index"
    counts = "SELECT (SELECT count(*) FROM reference_entries), (SELECT count(*) FROM reference_citation_links)"
    with api.engine.connect() as conn:
        assert tuple(conn.execute(text(counts)).one()) == (15, 15)

    # References that predate the link table (or lost their rows) are backfilled.
    with api.engine.begin() as conn:
        conn.execute(text("DELETE FROM reference_citation_links"))
    assert {r["citation_link"]["status"] for r in refs_of(a, ids["item_id"])} == {"not_reconciled"}
    policy = CitationPolicy(reconcile_batch_size=4)
    assert reconcile_pending(api.engine, policy) == {"batches": 4, "references": 15}
    assert tally(refs_of(a, ids["item_id"]), MD_REFS)["linked_correct"] == 4
    stamp = api.scalar("SELECT max(reconciled_at) FROM reference_citation_links")
    assert reconcile_pending(api.engine, policy) == {"batches": 0, "references": 0}
    with Session(api.engine) as db:  # an explicit rerun changes nothing
        assert reconcile_references(db, source_file_id=uuid.UUID(ids["file_id"])) == 15
        db.commit()
    assert api.scalar("SELECT max(reconciled_at) FROM reference_citation_links") == stamp
    with api.engine.connect() as conn:
        assert tuple(conn.execute(text(counts)).one()) == (15, 15)

    # If the source text changes, the old edge is fenced at read time and in SQL.
    target = paper_id(api, "2609.00011")
    assert a.get(f"/discovery/papers/{target}/citing-items").json()["total"] == 1
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE reference_entries SET raw_citation='A. Alpha. Exact Linking Study. 2026.'"
                          " WHERE item_id=:i AND ordinal=0"), {"i": uuid.UUID(ids["item_id"])})
    first = refs_of(a, ids["item_id"])[0]["citation_link"]
    assert (first["status"], first["target"]) == ("not_reconciled", None)
    assert a.get(f"/discovery/papers/{target}/citing-items").json()["total"] == 0
    assert reconcile_pending(api.engine, policy)["references"] == 1
    assert refs_of(a, ids["item_id"])[0]["citation_link"]["status"] == "no_arxiv_id"

    # An out-of-band tombstone is honoured on read even before re-targeting runs.
    with api.engine.begin() as conn:
        conn.execute(text("INSERT INTO discovery_deleted_records (source, remote_id, oai_datestamp)"
                          " VALUES ('arxiv', '2609.00018', DATE '2026-09-29')"))
    assert api.scalar("SELECT match_status FROM reference_citation_links l JOIN reference_entries r"
                      " ON r.id=l.reference_id WHERE r.ordinal=13") == "linked"
    doi_ref = refs_of(a, ids["item_id"])[13]["citation_link"]
    assert (doi_ref["status"], doi_ref["target"]) == ("discovery_record_deleted", None)
    assert a.get(f"/discovery/papers/{paper_id(api, '2609.00018')}/citing-items").status_code == 404

    # A matcher version bump marks every row stale until reconciled again.
    monkeypatch.setattr("app.citations.MATCHER_VERSION", "arxiv-exact-test-next")
    assert reconcile_pending(api.engine, CitationPolicy())["references"] == 15
    assert api.scalar("SELECT count(DISTINCT matcher_version) FROM reference_citation_links") == 1


def _race_reference(db: Session, ids: dict, label: str, raw: str) -> None:
    db.add(ReferenceEntry(item_id=uuid.UUID(ids["item_id"]), version_id=uuid.UUID(ids["version_id"]),
                          source_file_id=uuid.UUID(ids["file_id"]), ordinal=0, raw_citation=raw, fields={},
                          confidence="raw_only", provenance=label, locator_kind="markdown", page_number=None,
                          start_offset=0, end_offset=len(raw), pipeline_version=label))
    db.flush()


def _status(api, label):
    return api.scalar("SELECT l.match_status FROM reference_citation_links l JOIN reference_entries r"
                      " ON r.id=l.reference_id WHERE r.pipeline_version=:p", p=label)


def test_reference_and_discovery_arriving_concurrently_never_both_miss(upload_api):
    api, storage = upload_api
    a, _, _ = two_users(api)
    harvest(api.engine, oai_page(SEED))
    ids = upload(a, markdown([MD_REFS[0]]))
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    errors = []

    def run(fn):
        def wrapped():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        thread = threading.Thread(target=wrapped)
        thread.start()
        return thread

    # 1. Ingest writes its link first; the discovery page waits, then re-targets it.
    with Session(api.engine) as w:
        _race_reference(w, ids, "race-ingest-first", "[1] Race. arXiv:2609.00031.")
        reconcile_references(w, source_file_id=uuid.UUID(ids["file_id"]), pipeline_version="race-ingest-first")
        page = oai_page([("2609.00031", "Race Target One", 1, "2026-09-28")])
        thread = run(lambda: harvest(api.engine, page, NOW + timedelta(minutes=1)))
        thread.join(1.0)
        assert thread.is_alive(), "discovery must wait for the ingest transaction"
        w.commit()
    thread.join(20)
    assert not thread.is_alive() and not errors
    assert _status(api, "race-ingest-first") == "linked"

    # 2. Discovery writes the paper first; ingest waits, then sees committed metadata.
    with Session(api.engine) as d:
        _write_page(d, "cs.AI", parse_page(oai_page([("2609.00032", "Race Target Two", 1, "2026-09-28")]), 10**6),
                    NOW + timedelta(minutes=2))

        def ingest():
            with Session(api.engine) as w:
                _race_reference(w, ids, "race-discovery-first", "[1] Race. arXiv:2609.00032.")
                reconcile_references(w, source_file_id=uuid.UUID(ids["file_id"]),
                                     pipeline_version="race-discovery-first")
                w.commit()
        thread = run(ingest)
        thread.join(1.0)
        assert thread.is_alive(), "ingest must wait for the discovery transaction"
        d.commit()
    thread.join(20)
    assert not thread.is_alive() and not errors
    assert _status(api, "race-discovery-first") == "linked"


def test_without_the_link_lock_the_race_leaves_a_stale_row(upload_api, monkeypatch):
    """Control: the advisory lock is what prevents case 1 above from missing."""
    api, storage = upload_api
    a, _, _ = two_users(api)
    harvest(api.engine, oai_page(SEED))
    ids = upload(a, markdown([MD_REFS[0]]))
    assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    monkeypatch.setattr("app.citations._lock", lambda db, exclusive: None)
    with Session(api.engine) as w:
        _race_reference(w, ids, "race-unlocked", "[1] Race. arXiv:2609.00033.")
        reconcile_references(w, source_file_id=uuid.UUID(ids["file_id"]), pipeline_version="race-unlocked")
        harvest(api.engine, oai_page([("2609.00033", "Race Target Three", 1, "2026-09-28")]), NOW + timedelta(minutes=1))
        w.commit()
    assert _status(api, "race-unlocked") == "no_discovery_record"


def test_concurrent_backfills_write_one_row_per_reference(upload_api):
    api, storage = upload_api
    a, b, _ = two_users(api)
    harvest(api.engine, oai_page(SEED))
    for browser in (a, b):
        ids = upload(browser, markdown())
        assert process_job(uuid.UUID(ids["job_id"]), storage=storage) == "awaiting_index"
    with api.engine.begin() as conn:
        conn.execute(text("DELETE FROM reference_citation_links"))
    barrier = Barrier(4)

    def backfill(_):
        barrier.wait()
        return reconcile_pending(api.engine, CitationPolicy(reconcile_batch_size=5))

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(backfill, range(4)))
    assert sum(r["references"] for r in results) >= 30
    with api.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM reference_citation_links")) == 30
        assert conn.scalar(text("SELECT count(*) FROM reference_citation_links WHERE match_status='linked'")) == 8
        assert conn.scalar(text("SELECT count(*) FROM (SELECT reference_id FROM reference_citation_links"
                                " GROUP BY reference_id HAVING count(*) > 1) d")) == 0
