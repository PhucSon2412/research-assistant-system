"""M4c evaluation tooling: dataset schema, gold locators, metrics, review scoring and ACL."""
import json
import math
from unittest import mock

import httpx
import pytest
from pydantic import ValidationError

from app.config import EmbeddingPolicy, GenerationPolicy, get_settings
from app.evaluation import local_embedding
from app.evaluation.corpus import DEFAULT_ROOT, build_pdfs
from app.evaluation.locator import Hit, annotate, check_non_evidence, check_span, covers, load_source
from app.evaluation.metrics import Row, aggregate, failure, recall, side_ranks
from app.evaluation.review import answer_rows, machine_check, score as score_review
from app.evaluation.runner import (ORIGIN, Capture, Environment, check_viewer, eval_settings, evaluate,
                                   instrumented, score, seed)
from app.evaluation.schema import (GROUP_MINIMUMS, HOLDOUT_MINIMUM, NonEvidence, Question, QuestionSet, Span,
                                   check_manifest, check_questions, load)
from app.evaluation.validate import validate
from app.main import app
from app.rag import GenerationResult
from tests.test_uploads import upload_api  # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def dataset():
    return load(DEFAULT_ROOT)


def _question(qset, qid) -> Question:
    return next(q for q in qset.questions if q.id == qid)


def _replace(qset: QuestionSet, question: Question) -> QuestionSet:
    return qset.model_copy(update={"questions": [question if q.id == question.id else q for q in qset.questions]})


def test_committed_dataset_is_valid_and_pending_review(dataset):
    manifest, qset = dataset
    assert validate(DEFAULT_ROOT) == []
    assert len(qset.questions) >= 80
    assert sum(q.split == "holdout" for q in qset.questions) >= HOLDOUT_MINIMUM
    for group, minimum in GROUP_MINIMUMS.items():
        assert sum(q.group == group for q in qset.questions) >= minimum
    assert {q.label_status for q in qset.questions} == {"pending_human_review"}
    assert all(p.license.redistribution_in_repo and not p.contains_third_party_text for p in manifest.papers)
    assert manifest.representativeness.papers < manifest.representativeness.target_papers


def test_label_status_cannot_claim_a_review(dataset):
    _, qset = dataset
    data = json.loads(qset.model_dump_json())
    data["questions"][0]["label_status"] = "expert_reviewed"
    with pytest.raises(ValidationError):
        QuestionSet.model_validate(data)


def test_cross_checks_reject_mislabelled_questions(dataset):
    manifest, qset = dataset
    assert check_questions(manifest, qset) == []

    foreign = _question(qset, "SP-02")
    span = foreign.evidence[0].spans[0].model_copy(update={"item": "B/P21"})
    bad = _replace(qset, foreign.model_copy(update={"evidence": [foreign.evidence[0].model_copy(update={"spans": [span]})]}))
    assert any("outside the caller's allowed scope" in e for e in check_questions(manifest, bad))

    one_side = _question(qset, "CMP-01")
    bad = _replace(qset, one_side.model_copy(update={"evidence": one_side.evidence[:1]}))
    assert any("at least two papers" in e for e in check_questions(manifest, bad))

    idor = _question(qset, "ACL-06")
    bad = _replace(qset, idor.model_copy(update={"expected": idor.expected.model_copy(update={"status": "insufficient_evidence"})}))
    assert any("scope_not_found" in e for e in check_questions(manifest, bad))

    own_marker = _question(qset, "ACL-10")
    bad = _replace(qset, own_marker.model_copy(update={"forbidden_markers": ["ZQXA7731"]}))
    errors = check_questions(manifest, bad)
    assert any("appears in evidence" in e for e in errors) and any("belongs to the caller" in e for e in errors)

    repeated = _question(qset, "SP-03").model_copy(update={"question": _question(qset, "SP-04").question})
    assert any("repeated question text" in e for e in check_questions(manifest, _replace(qset, repeated)))

    no_holdout = qset.model_copy(update={"questions": [q.model_copy(update={"split": "dev"}) for q in qset.questions]})
    assert any("holdout" in e for e in check_questions(manifest, no_holdout))

    short = qset.model_copy(update={"questions": [q for q in qset.questions if q.group != "partial_pdf"][:-1]})
    errors = check_questions(manifest, short)
    assert any("group partial_pdf" in e for e in errors) and any("needs at least 80" in e for e in errors)


def test_manifest_checksum_and_rights_are_enforced(dataset, tmp_path):
    manifest, _ = dataset
    assert check_manifest(manifest, DEFAULT_ROOT) == []
    paper = manifest.papers[0]
    changed = manifest.model_copy(update={"papers": [paper.model_copy(update={"sha256": "0" * 64}), *manifest.papers[1:]]})
    assert any("checksum" in e for e in check_manifest(changed, DEFAULT_ROOT))
    unlicensed = paper.model_copy(update={"license": paper.license.model_copy(update={"redistribution_in_repo": False})})
    changed = manifest.model_copy(update={"papers": [unlicensed, *manifest.papers[1:]]})
    assert any("redistribution" in e for e in check_manifest(changed, DEFAULT_ROOT))


def test_markdown_gold_span_checks_offsets_lines_parser_and_bibliography(dataset):
    manifest, qset = dataset
    papers = {p.id: p for p in manifest.papers}
    source = load_source(papers["P06"], DEFAULT_ROOT)
    good = _question(qset, "RCI-03").evidence[0].spans[0]
    assert check_span(good, source) == []
    shifted = good.model_copy(update={"start_offset": good.start_offset + 1, "end_offset": good.end_offset + 1})
    assert any("offsets" in e for e in check_span(shifted, source))
    assert any("line numbers" in e for e in check_span(good.model_copy(update={"start_line": 1}), source))
    assert any("parser" in e for e in check_span(good.model_copy(update={"parser": "markdown-lines-v1"}), source))
    entry = "[4] R. Castell. Quantum Annealing for Expert Placement in Sparse Models. 2025."
    as_evidence = annotate(Span(item="A/P06", locator_kind="markdown", quote=entry), source)
    assert any("references region" in e for e in check_span(as_evidence, source))
    assert check_non_evidence(NonEvidence(item="A/P06", kind="bibliography_entry", quote=entry, why="x"), source) == []
    body = "This survey covers 41 papers"
    assert any("not inside a references section" in e for e in check_non_evidence(
        NonEvidence(item="A/P06", kind="bibliography_entry", quote=body, why="x"), source))


def test_pdf_gold_spans_match_extraction_and_unextracted_pages(dataset):
    manifest, qset = dataset
    papers = {p.id: p for p in manifest.papers}
    source = load_source(papers["P08"], DEFAULT_ROOT)
    span = _question(qset, "PP-03").evidence[0].spans[0]
    assert span.page == 5 and check_span(span, source) == []
    assert any("offsets" in e for e in check_span(span.model_copy(update={"page": 3}), source))
    region = NonEvidence(item="A/P08", kind="not_extracted_region", page=4, why="figure")
    assert check_non_evidence(region, source) == []
    assert check_non_evidence(region.model_copy(update={"page": 3}), source)
    assert 4 in source.deficient_pages and 3 not in source.deficient_pages


def test_pdf_sources_render_to_the_committed_bytes(dataset):
    manifest, _ = dataset
    expected = {p.file: p.sha256 for p in manifest.papers if p.format == "pdf"}
    assert build_pdfs(DEFAULT_ROOT, write=False) == expected


def test_cover_rule_and_recall_formulas():
    span = Span(item="A/P01", locator_kind="markdown", quote="x" * 10, start_offset=100, end_offset=110,
                start_line=3, end_line=3, parser="p")
    assert covers(span, Hit("A/P01", "markdown", None, 0, 200))
    assert covers(span, Hit("A/P01", "markdown", None, 105, 300))       # exactly half
    assert not covers(span, Hit("A/P01", "markdown", None, 106, 300))
    assert not covers(span, Hit("B/P01", "markdown", None, 0, 200))
    pdf_span = span.model_copy(update={"locator_kind": "pdf", "page": 2})
    assert covers(pdf_span, Hit("A/P01", "pdf", 2, 90, 120)) and not covers(pdf_span, Hit("A/P01", "pdf", 3, 90, 120))

    question = Question.model_validate({
        "id": "CMP-99", "split": "dev", "group": "comparison", "question": "q", "identity": "user_a",
        "scope": {"kind": "all"}, "expected": {"status": "answered"}, "supported_claim": "c", "rationale": "r",
        "label_status": "pending_human_review", "label_author": "coding_agent",
        "evidence": [{"label": "one", "spans": [span.model_dump()]},
                     {"label": "two", "spans": [span.model_copy(update={"item": "A/P02"}).model_dump()]}]})
    hits = [Hit("A/P03", "markdown", None, 0, 5)] * 6 + [Hit("A/P02", "markdown", None, 100, 110)]
    hits.insert(1, Hit("A/P01", "markdown", None, 100, 110))
    ranks = side_ranks(question, hits)
    assert ranks == [2, 8]
    assert recall(ranks) == 0.5 and recall(ranks, k=8) == 1.0
    assert failure(ranks, [True, True], acl_violation=False) == "rank"
    assert failure([2, None], [True, True], acl_violation=False) == "retrieval"
    assert failure([2, None], [True, False], acl_violation=False) == "extraction"
    assert failure([1, 1], [True, True], acl_violation=True) == "acl"
    assert failure([1, 3], [True, True], acl_violation=False) == "ok"
    rows = [Row("CMP-99", "comparison", "dev", "all", "vector", (2, 8), "rank"),
            Row("SP-99", "single_paper", "dev", "all", "vector", (1,), "ok")]
    [entry] = aggregate(rows, ["stage"])
    assert entry["questions"] == 2 and entry["recall_at_k"] == 0.75
    assert (entry["sides_hit"], entry["sides_total"]) == (2, 3)
    assert (entry["all_sides_hit"], entry["multi_side_questions"]) == (0, 1)
    with pytest.raises(ValueError):
        recall([])


def test_runner_attributes_pack_losses_separately(dataset):
    manifest, qset = dataset
    question = _question(qset, "SP-20")
    span = question.evidence[0].spans[0]
    hit = [span.item, "markdown", None, span.start_offset, span.end_offset]
    index = {span.item: [Hit(*hit)]}
    base = {"question_id": "SP-20", "mode": "hybrid", "status_code": 200, "acl_violations": []}
    rows = score(manifest, qset, [
        base | {"stage": "rag_pack", "hits": [], "search_pool": [hit]},
        base | {"stage": "rag_pack", "hits": [], "search_pool": []},
        base | {"stage": "search", "hits": [["A/P01", "markdown", None, 0, 5]] * 5 + [hit]},
    ], index)
    assert [row.outcome for row in rows] == ["evidence_pack", "retrieval", "rank"]
    assert score(manifest, qset, [base | {"stage": "search", "hits": [hit]}], {})[0].outcome == "extraction"


def test_local_embedding_is_deterministic_unit_and_token_free(api):
    first, second = local_embedding.vector("GSR reached 27.9 BLEU"), local_embedding.vector("GSR reached 27.9 BLEU")
    assert first == second and len(first) == local_embedding.DIMENSIONS
    assert math.isclose(sum(v * v for v in first), 1.0)
    assert math.isclose(sum(v * v for v in local_embedding.vector("the of and")), 1.0)
    settings = eval_settings(get_settings())
    result = local_embedding.embed("text", settings, input_type="search_query")
    assert result.input_tokens == 0 and result.model_id == local_embedding.MODEL_ID


def test_eval_settings_leave_pricing_gates_off(api):
    base = get_settings()
    settings = eval_settings(base)
    assert settings.embedding.pricing_verified and settings.generation.pricing_verified
    assert settings.rerank.pricing_verified is False
    assert base.embedding.pricing_verified is False and base.generation.pricing_verified is False
    assert EmbeddingPolicy().pricing_verified is False and GenerationPolicy().pricing_verified is False
    assert settings.embedding.model_id == local_embedding.MODEL_ID and settings.generation.model_id != base.generation.model_id


def test_instrumented_run_blocks_real_outbound_http():
    capture = Capture()
    original = httpx.HTTPTransport.handle_request
    with instrumented(capture):
        with pytest.raises(httpx.ConnectError):
            httpx.Client().get("https://openrouter.ai/api/v1/models")
    assert capture.blocked_hosts == ["openrouter.ai"]
    assert httpx.HTTPTransport.handle_request is original


def test_review_scoring_uses_only_human_columns(dataset):
    manifest, qset = dataset
    question = _question(qset, "SP-20")
    span = question.evidence[0].spans[0]
    chunk = {"text": "## Abstract\n\n" + span.quote}
    citation = {"chunk_id": "c1", "item_id": "i1", "quote": span.quote, "locator_kind": "markdown",
                "start_line": span.start_line, "end_line": span.end_line, "start_offset": span.start_offset - 13,
                "end_offset": span.end_offset, "start_page": None, "locator_detail": {}}
    assert machine_check(citation, {"c1": chunk}, question, "A/P06") == {
        "machine_id_in_evidence": "yes", "machine_quote_verbatim": "yes", "machine_locator_overlaps_gold": "yes"}
    assert machine_check(citation | {"chunk_id": "c2"}, {"c1": chunk}, question, "A/P06")["machine_id_in_evidence"] == "no"
    assert machine_check(citation | {"quote": "invented"}, {"c1": chunk}, question, "A/P06")["machine_quote_verbatim"] == "no"
    response = {"status": "answered", "claims": [{"text": "41 papers", "citations": [citation, citation]}]}
    rows = answer_rows(question, "hybrid", response, {"c1": chunk}, lambda _: "A/P06")
    assert len(rows) == 2 and rows[0]["machine_locator_overlaps_gold"] == "yes"
    # Machine checks alone never count as a reviewed judgement.
    assert score_review(rows)["grounded_answer_rate"]["denominator"] == 0
    rows[0] |= {"human_claim_supported": "yes", "human_citation_correct": "yes", "reviewer": "r1"}
    rows[1] |= {"human_claim_supported": "partial", "human_citation_correct": "no", "reviewer": "r1"}
    abstain = answer_rows(_question(qset, "NA-01"), "hybrid", {"status": "insufficient_evidence", "claims": []}, {}, str)
    abstain[0] |= {"human_abstention_appropriate": "yes", "reviewer": "r1"}
    wrong = answer_rows(_question(qset, "NA-02"), "hybrid", {"status": "insufficient_evidence", "claims": []}, {}, str)
    wrong[0] |= {"human_abstention_appropriate": "no", "reviewer": "r2"}
    metrics = score_review(rows + abstain + wrong)
    assert metrics["grounded_answer_rate"] == {"numerator": 0, "denominator": 1, "value": 0.0}
    assert metrics["citation_accuracy"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert metrics["abstention_appropriateness"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    with pytest.raises(ValueError):
        score_review([rows[0] | {"human_claim_supported": "probably"}])
    with pytest.raises(ValueError):
        score_review([rows[0] | {"reviewer": ""}])


ACL_ITEMS = {"A/P01", "A/P10", "A/P11", "B/P11", "B/P20", "B/P21", "C/C01"}


def test_acl_same_doi_hash_matrix_through_runner(upload_api, monkeypatch, dataset):
    """A/B/Admin with one DOI, byte-identical uploads and per-copy canaries."""
    api, storage = upload_api
    manifest, qset = dataset
    monkeypatch.setenv("RAS_HTTP__ALLOWED_ORIGINS", json.dumps([ORIGIN]))
    get_settings.cache_clear()
    settings = eval_settings(get_settings())
    app.dependency_overrides[get_settings] = lambda: settings
    subset = manifest.model_copy(update={"items": [i for i in manifest.items if i.key in ACL_ITEMS],
                                         "collections": [c for c in manifest.collections if c.key == "B-speech"]})
    questions = [q for q in qset.questions if q.group == "permission" or q.id in ("CMP-14", "RCI-10", "NA-14")]
    subset_q = qset.model_copy(update={"questions": questions})
    capture = Capture()
    with instrumented(capture):
        seeded = seed(Environment(api.engine, storage, settings, {}), subset, DEFAULT_ROOT)
        assert seeded.ingest["A/P11"]["job_state"] == seeded.ingest["B/P11"]["job_state"] == "ready"
        records = evaluate(Environment(api.engine, storage, settings, {}), subset, subset_q, seeded, capture)
        viewer = check_viewer(seeded, records)
        a, b, admin = (seeded.identities[k] for k in ("user_a", "user_b", "admin"))

        # A model that tries to cite User B's chunk is rejected before anything is returned.
        b_hit = next(r for r in records if r["question_id"] == "ACL-02" and r["stage"] == "search"
                     and r["mode"] == "hybrid")
        b_chunk = b_hit["chunk_ids"][0]
        b_text = b.client.get(f"/search/chunks/{b_chunk}").json()["text"]

        def cite_b(messages, cfg):
            capture.payloads.append({"user": json.loads(messages[1]["content"])})
            return GenerationResult(json.dumps({"claims": [{"text": "WER is 7.9%.", "citations": [
                {"chunk_id": b_chunk, "quote": "7.9%"}]}]}), 0, 0, cfg.generation.model_id, "local-capture")

        with mock.patch("app.rag.generate", cite_b):
            capture.reset()
            forged = a.post("/rag/ask", {"question": _question(qset, "ACL-01").question, "mode": "hybrid",
                                         "scope": "all"})
        assert forged.status_code == 200 and forged.json()["status"] == "insufficient_evidence"
        assert b_chunk not in forged.text and "ZQXB5582" not in forged.text
        assert capture.payloads and b_text not in json.dumps(capture.payloads)
        for other in (a, admin):
            assert other.client.get(f"/search/chunks/{b_chunk}").status_code == 404

    assert capture.blocked_hosts == []
    assert [r for r in records if r["acl_violations"]] == []
    assert viewer["violations"] == [] and viewer["requests"] == 3 * viewer["chunks"] > 0
    by = {(r["question_id"], r["stage"], r["mode"]): r for r in records}
    # Same DOI and title: each user retrieves only their own copy; Admin retrieves nothing.
    own = {"ACL-03": "A/P10", "ACL-04": "B/P20", "ACL-09": "B/P11", "ACL-02": "B/P21"}
    for qid, item in own.items():
        hits = by[(qid, "search", "hybrid")]["hits"]
        assert hits and {h[0] for h in hits} <= {k for k in seeded.items if k.startswith(item[:2])}
        assert hits[0][0] == item
    for stage in ("search", "rag_pack"):
        assert by[("ACL-05", stage, "hybrid")]["hits"] == []
    assert by[("ACL-05", "rag_pack", "hybrid")]["generation_called"] is False
    for qid in ("ACL-06", "ACL-07", "ACL-08"):
        assert by[(qid, "search", "keyword")]["status_code"] == 404
        assert by[(qid, "rag_pack", "hybrid")]["status_code"] == 404
    a_payloads = [r for r in records if r["stage"] == "rag_pack" and r["generation_called"]
                  and next(q for q in questions if q.id == r["question_id"]).identity == "user_a"]
    assert a_payloads and all(h[0].startswith("A/") for r in a_payloads for h in r["hits"])
    assert all(not r.get("gold_label_in_payload") for r in records)
    assert {k for r in records for k in r.get("payload_request_keys", [])} == {"evidence", "question"}
