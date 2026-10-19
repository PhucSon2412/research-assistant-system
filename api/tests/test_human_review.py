"""M7b human review bundle, sheet import and report (offline: no DB, provider or network).

Reviewer names here are fixtures written only under ``tmp_path``; none is a real verdict.
"""
import json
import re
import shutil
from datetime import date, datetime, timezone

import pytest

from app.evaluation import human, human_bundle, review
from app.evaluation.__main__ import main
from app.evaluation.corpus import DEFAULT_ROOT
from app.evaluation.schema import load
from app.evaluation.validate import load_sources

TODAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
FIXTURE_REVIEWERS = ("fixture-reviewer-a", "fixture-reviewer-b")
LUMEN = {"title": "lumen-bench", "locator_kind": "markdown", "start_page": None, "start_line": 5,
         "quote": "measured a drift of 0.4 percent over twelve hours", "chunk_id": "c-lumen",
         "version_id": "v-lumen", "source_file_id": "f-lumen"}
HARBOR = {"title": "harbor-sensor", "locator_kind": "pdf", "start_page": 1, "start_line": None,
          "quote": "Over thirty synthetic days the Harbor tide sensor reported a mean error of 1.2 centimeters",
          "chunk_id": "c-harbor", "version_id": "v-harbor", "source_file_id": "f-harbor"}


def answer(label, question, status, claims):
    return {"label": label, "question": question, "http": 200, "status": status, "reason": None,
            "claims": claims, "provider_calls": 1, "error": None}


def snapshot(answers=None, *, chrome="Trạng thái: Có câu trả lời.", corpus_ok=True) -> dict:
    files = human.m7a_files()
    snap = human.empty_snapshot("test-run")
    snap["answers"] = answers if answers is not None else [
        answer("single", "How much drift?", "answered", [{"text": "Drift was 0.4 percent.", "citations": [LUMEN]}]),
        answer("compare", "Compare drift and error.", "answered",
               [{"text": "0.4 percent drift versus 1.2 cm error.", "citations": [LUMEN, HARBOR]}]),
        answer("insufficient", "What was the price?", "insufficient_evidence", []),
    ]
    snap["chrome_text"] = chrome
    snap["targets"] = {"harbor": {"item_id": "i-harbor", "version_id": "v-harbor", "file_id": "f-harbor"}}
    snap["corpus"] = {"lumen-md_sha256": human.sha256_bytes(files["lumen-bench.md"]),
                      "harbor-pdf_sha256": human.sha256_bytes(files["harbor-sensor.pdf"]) if corpus_ok else "0" * 64}
    snap["generation_calls"] = [{"seq": n, "route": f"runtime:rag-{label}", "status": "completed",
                                 "generation_id": f"gen-test-{n}", "model": "test/model", "provider": "TestProvider",
                                 "selected": [{"model": "test/model-1", "provider": "TestProvider"}]}
                                for n, label in enumerate(("single", "compare", "insufficient"), 1)]
    return snap


@pytest.fixture(scope="module")
def m4c_parts():
    return human_bundle.m4c_outputs(DEFAULT_ROOT)


@pytest.fixture(scope="module")
def built(m4c_parts):
    return human_bundle.build(DEFAULT_ROOT, human.dump_json(snapshot()), m4c_parts=m4c_parts)


@pytest.fixture
def bundle(tmp_path, built):
    path = tmp_path / "bundle"
    human_bundle.write(path, built)
    return path


def rows_of(bundle, kind):
    columns = human.LABEL_COLUMNS if kind == "labels" else human.ANSWER_COLUMNS
    name = human.LABEL_SHEET if kind == "labels" else human.ANSWER_SHEET
    rows, errors = human.read_sheet(bundle / name, columns)
    assert errors == []
    return [row for _, row in rows]


def write_sheet(path, kind, rows):
    columns = human.LABEL_COLUMNS if kind == "labels" else human.ANSWER_COLUMNS
    path.write_bytes(human.sheet_bytes(columns, rows))
    return path


def fill(bundle, tmp_path, kind, updates, name="sheet.csv"):
    key = "question_id" if kind == "labels" else "row_key"
    rows = [row | updates.get(row[key], {}) for row in rows_of(bundle, kind)]
    return write_sheet(tmp_path / name, kind, rows)


def accept(reviewer="fixture-reviewer-a", verdict="accepted", notes=""):
    return {"verdict": verdict, "reviewer": reviewer, "review_date": "2026-10-19", "notes": notes}


def grade(row_key, supported="yes", correct="yes", reviewer="fixture-reviewer-a", abstention=""):
    values = {"reviewer": reviewer, "review_date": "2026-10-19"}
    if "#answer" in row_key:
        return values | {"human_abstention_appropriate": abstention or "yes"}
    return values | {"human_claim_supported": supported, "human_citation_correct": correct,
                     "human_abstention_appropriate": abstention}


def import_labels(sheet, bundle, records, **kwargs):
    return human.import_labels(sheet, bundle=bundle, records=records, today=TODAY, now=NOW, **kwargs)


def import_answers(sheet, bundle, records, **kwargs):
    return human.import_answers(sheet, bundle=bundle, records=records, today=TODAY, now=NOW, **kwargs)


def report(bundle, records, root=DEFAULT_ROOT):
    return human.build_report(bundle=bundle, records=records, root=root, today=TODAY)


SINGLE, COMPARE1, COMPARE2, INSUFFICIENT = ("test-run/01-single#c1.1", "test-run/02-compare#c1.1",
                                            "test-run/02-compare#c1.2", "test-run/03-insufficient#answer")


# --- bundle -------------------------------------------------------------------------------

def test_bundle_covers_every_label_and_keeps_m7a_apart(built):
    _, qset = load(DEFAULT_ROOT)
    manifest = json.loads(built["manifest.json"])
    labels, _ = human.parse_sheet(built[human.LABEL_SHEET], human.LABEL_COLUMNS)
    labels = [row for _, row in labels]
    assert len(labels) == len(qset.questions) == 80 == len(manifest["m4c"]["labels"])
    assert sum(row["split"] == "holdout" for row in labels) == manifest["m4c"]["holdout"] == 16
    assert {g: c["dev"] + c["holdout"] for g, c in manifest["m4c"]["groups"].items()} == {
        "single_paper": 20, "comparison": 15, "no_answer": 15, "partial_pdf": 10, "permission": 10,
        "references_conflict_injection": 10}
    assert all(f"labels/{q.id}.html" in built for q in qset.questions)
    assert all(not any(row[c] for c in human.LABEL_HUMAN) for row in labels)  # nothing pre-filled
    answers, _ = human.parse_sheet(built[human.ANSWER_SHEET], human.ANSWER_COLUMNS)
    answers = [row for _, row in answers]
    assert [row["row_key"] for row in answers] == [SINGLE, COMPARE1, COMPARE2, INSUFFICIENT]
    assert {row["split"] for row in answers} == {"m7a"} and not {r["question_id"] for r in answers} & {
        q.id for q in qset.questions}
    assert all(not any(row[c] for c in human.ANSWER_HUMAN) for row in answers)
    assert manifest["m7a"]["available"] and manifest["m7a"]["corpus_matches_run"] is True
    chrome = next(r for r in manifest["m7a"]["responses"] if r["label"] == "chrome")
    assert not chrome["scoreable"] and chrome["not_scoreable_reason"] == "screen_text_only" and chrome["rows"] == {}
    assert manifest["files"] == {p: human.sha256_bytes(d) for p, d in built.items()
                                 if p not in ("manifest.json", human.LABEL_SHEET, human.ANSWER_SHEET)}
    # Contexts come from the production parser, with the gold quote marked and line/page links.
    page = built["labels/SP-01.html"].decode()
    assert "<mark>" in page and 'href="../papers/P01.html#L19"' in page
    assert 'href="../files/pdf/' in built["labels/PP-01.html"].decode() and "#page=" in built["labels/PP-01.html"].decode()


def test_pages_are_static_offline_html(built):
    for path, data in built.items():
        if not path.endswith(".html"):
            continue
        text = data.decode("utf-8")
        assert "Content-Security-Policy" in text and "default-src 'none'" in text, path
        assert "<script" not in text.lower() and "<iframe" not in text.lower(), path
        for href in re.findall(r'(?:href|src)="([^"]*)"', text):
            assert not re.match(r"^[a-z][a-z0-9+.-]*:|^//", href, re.I), (path, href)


def test_html_and_sheet_escape_corpus_and_answer_text(m4c_parts):
    manifest, qset = load(DEFAULT_ROOT)
    sources = load_sources(manifest, DEFAULT_ROOT)
    evil = '<script>alert(1)</script><img src="https://example.invalid/x" onerror="alert(2)">'
    question = qset.questions[0].model_copy(update={"question": "=HYPERLINK(\"https://example.invalid\") " + evil,
                                                    "rationale": evil})
    page = human_bundle.label_page(question, manifest, sources, None, None).decode()
    assert "<script>alert" not in page and "<img src=" not in page and "&lt;script&gt;" in page
    snap = snapshot([answer("single", evil, "answered",
                            [{"text": evil, "citations": [LUMEN | {"quote": "=1+1", "title": evil}]}])], chrome=evil)
    outputs = human_bundle.build(DEFAULT_ROOT, human.dump_json(snap), m4c_parts=m4c_parts)
    for path in ("answers/01-single.html", "answers/chrome.html", "index.html"):
        text = outputs[path].decode()
        assert "<script>alert" not in text and '<img src="https' not in text, path
    sheet = outputs[human.ANSWER_SHEET].decode("utf-8-sig")
    assert ",'=1+1," in sheet  # a spreadsheet must not run the quote as a formula
    rows, errors = human.parse_sheet(outputs[human.ANSWER_SHEET], human.ANSWER_COLUMNS)
    assert errors == [] and rows[0][1]["citation_quote"] == "=1+1" and rows[0][1]["claim_text"] == evil
    for value in ("=x", "+1", "-1", "@a", "'quoted", "\tTab", "plain"):
        assert human.cell_in(human.cell_out(value)) == value


def test_bundle_is_reproducible_and_check_finds_drift(tmp_path, built, m4c_parts, bundle):
    again = human_bundle.build(DEFAULT_ROOT, human.dump_json(snapshot()), m4c_parts=human_bundle.m4c_outputs(DEFAULT_ROOT))
    assert again == built
    assert human_bundle.check(bundle, built) == []
    fill(bundle, bundle, "labels", {"SP-01": accept()}, name=human.LABEL_SHEET)
    assert human_bundle.check(bundle, built) == []  # filled review cells are not drift
    rows = rows_of(bundle, "labels")
    rows[0]["question"] = "edited"
    write_sheet(bundle / human.LABEL_SHEET, "labels", rows)
    (bundle / "labels/SP-02.html").write_text("tampered", encoding="utf-8")
    (bundle / "extra.txt").write_text("x", encoding="utf-8")
    assert human_bundle.check(bundle, built) == [
        "unexpected: extra.txt", f"differs: {human.LABEL_SHEET} (non-fill columns or rows)", "differs: labels/SP-02.html"]
    with pytest.raises(human.ReviewError, match="already has filled"):
        human_bundle.write(bundle, built)
    human_bundle.write(bundle, built, force=True)
    assert human_bundle.check(bundle, built) == []


def m7a_corpus_copy(tmp_path, newline: bytes):
    corpus = tmp_path / f"m7a-{len(newline)}"
    shutil.copytree(human.M7A_CORPUS, corpus)
    for path in corpus.iterdir():
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline))
    return corpus


def test_m7a_corpus_bytes_follow_the_run_not_the_checkout(tmp_path):
    lf, crlf = m7a_corpus_copy(tmp_path, b"\n"), m7a_corpus_copy(tmp_path, b"\r\n")
    lf_bytes = human.m7a_files(lf)["lumen-bench.md"]
    crlf_bytes = lf_bytes.replace(b"\n", b"\r\n")
    for corpus in (lf, crlf):
        assert human.m7a_files(corpus, human.sha256_bytes(crlf_bytes))["lumen-bench.md"] == crlf_bytes
        assert human.m7a_files(corpus, human.sha256_bytes(lf_bytes))["lumen-bench.md"] == lf_bytes
        assert human.m7a_files(corpus, "0" * 64)["lumen-bench.md"] == lf_bytes  # no match: never guessed
        assert human.m7a_files(corpus)["harbor-sensor.pdf"] == human.m7a_files(lf)["harbor-sensor.pdf"]


def test_committed_bundle_is_current_and_holds_no_fixture_verdicts(tmp_path, m4c_parts):
    if not (human.DEFAULT_BUNDLE / "manifest.json").is_file():
        pytest.skip("no committed bundle")
    snap = (human.DEFAULT_BUNDLE / human.SNAPSHOT_FILE).read_bytes()
    assert human_bundle.check(human.DEFAULT_BUNDLE, human_bundle.build(DEFAULT_ROOT, snap)) == []
    # Same bundle whether git checked the M7a corpus out with LF or CRLF.
    for newline in (b"\n", b"\r\n"):
        corpus = m7a_corpus_copy(tmp_path, newline)
        outputs = human_bundle.build(DEFAULT_ROOT, snap, corpus, m4c_parts=m4c_parts)
        assert human_bundle.check(human.DEFAULT_BUNDLE, outputs) == [], newline
    assert json.loads(snap)["corpus"] and json.loads((human.DEFAULT_BUNDLE / "manifest.json").read_text(
        encoding="utf-8"))["m7a"]["corpus_matches_run"] is True
    for path in human.DEFAULT_RECORDS.glob("*.json") if human.DEFAULT_RECORDS.is_dir() else []:
        reviewers = {r["reviewer"] for r in json.loads(path.read_text(encoding="utf-8"))["records"]}
        assert not reviewers & set(FIXTURE_REVIEWERS), path


# --- label import -------------------------------------------------------------------------

@pytest.mark.parametrize("cells, message", [
    ({"verdict": "accepted", "review_date": "2026-10-19"}, "incomplete review"),
    ({"verdict": "accepted", "reviewer": "fixture-reviewer-a"}, "incomplete review"),
    ({"reviewer": "fixture-reviewer-a", "review_date": "2026-10-19"}, "incomplete review"),
    ({"notes": "looks fine"}, "incomplete review"),
    (accept() | {"verdict": "approved"}, "is not one of"),
    (accept() | {"review_date": "19/10/2026"}, "not YYYY-MM-DD"),
    (accept() | {"review_date": "2026-10-20"}, "in the future"),
    (accept(verdict="changes_requested"), "needs notes"),
    (accept(verdict="rejected"), "needs notes"),
    (accept() | {"reviewer": "two\nlines"}, "single-line"),
])
def test_label_rows_need_verdict_reviewer_and_date(tmp_path, bundle, cells, message):
    records = tmp_path / "records"
    result = import_labels(fill(bundle, tmp_path, "labels", {"SP-01": cells, "SP-02": accept()}), bundle, records)
    assert any(message in e for e in result["errors"]), result["errors"]
    assert not result["written"] and not records.exists()


def test_label_sheet_structure_must_match_the_bundle(tmp_path, bundle):
    records = tmp_path / "records"
    good = import_labels(fill(bundle, tmp_path, "labels", {"SP-01": accept()}), bundle, records)
    assert good["errors"] == [] and good["written"] and good["pending"] == [r["question_id"] for r in rows_of(bundle, "labels")][1:]
    before = human.records_path(records, "labels").read_bytes()
    rows = rows_of(bundle, "labels")
    cases = {
        "unknown": [rows[0] | {"question_id": "SP-99"}] + rows[1:],
        "duplicate": rows + [rows[3] | accept()],
        "missing": rows[:-1],
        "edited question": [rows[0] | {"question": rows[0]["question"] + " (edited)"}] + rows[1:],
        "edited expectation": [rows[0] | {"expected_status": "insufficient_evidence"}] + rows[1:],
    }
    expected = {"unknown": "unknown question_id 'SP-99'", "duplicate": "duplicate question_id",
                "missing": "row missing from the sheet", "edited question": "question differ from the bundle",
                "edited expectation": "expected_status differ from the bundle"}
    for name, sheet_rows in cases.items():
        result = import_labels(write_sheet(tmp_path / f"{name}.csv", "labels", sheet_rows), bundle, records)
        assert any(expected[name] in e for e in result["errors"]), (name, result["errors"])
    columns = tmp_path / "columns.csv"
    columns.write_bytes(human.sheet_bytes(human.LABEL_COLUMNS[:-1], rows))
    assert "columns differ" in import_labels(columns, bundle, records)["errors"][0]
    latin = tmp_path / "latin.csv"
    latin.write_bytes((bundle / human.LABEL_SHEET).read_bytes() + "SP-01,café\n".encode("cp1252"))
    assert "not UTF-8" in import_labels(latin, bundle, records)["errors"][0]
    assert human.records_path(records, "labels").read_bytes() == before


def test_label_import_is_all_or_nothing_and_idempotent(tmp_path, bundle):
    records = tmp_path / "records"
    mixed = fill(bundle, tmp_path, "labels", {"SP-01": accept(), "SP-02": accept() | {"verdict": "maybe"}})
    assert import_labels(mixed, bundle, records)["errors"] and not records.exists()
    sheet = fill(bundle, tmp_path, "labels", {"SP-01": accept(), "SP-02": accept()})
    assert import_labels(sheet, bundle, records, dry_run=True)["errors"] == [] and not records.exists()
    first = import_labels(sheet, bundle, records)
    assert first["counts"] == {"added": 2} and first["written"]
    data = human.records_path(records, "labels").read_bytes()
    assert import_labels(sheet, bundle, records)["counts"] == {"unchanged": 2}
    assert human.records_path(records, "labels").read_bytes() == data
    stored = json.loads(data)
    assert stored["attribution"] == human.ATTRIBUTION and {r["attribution"] for r in stored["records"]} == {"self_reported"}
    _, qset = load(DEFAULT_ROOT)
    sp01 = next(q for q in qset.questions if q.id == "SP-01")
    binding = stored["records"][0]["binding"]
    assert binding["label_sha256"] == human.label_hash(sp01) and binding["version"] == human.BINDING_VERSION
    assert stored["records"][0]["binding_sha256"] == human.digest(binding)


def copy_root(tmp_path):
    root = tmp_path / "rag"
    shutil.copytree(DEFAULT_ROOT, root)
    return root


def edit_question(root, qid, **changes):
    path = root / "questions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    next(q for q in data["questions"] if q["id"] == qid).update(changes)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def test_stale_label_corpus_or_parser_blocks_import_and_voids_records(tmp_path, bundle, monkeypatch):
    records = tmp_path / "records"
    sheet = fill(bundle, tmp_path, "labels", {"SP-01": accept(), "SP-02": accept(verdict="changes_requested",
                                                                               notes="quote too short")})
    root = copy_root(tmp_path)
    assert import_labels(sheet, bundle, records, root=root)["written"]
    assert report(bundle, records, root)["m4c_labels"]["counts"]["changes_requested"] == 1
    # The agent "fixes" SP-02 after review: the old verdict no longer applies, it never becomes accepted.
    edit_question(root, "SP-02", rationale="rewritten after review")
    result = import_labels(sheet, bundle, records, root=root)
    assert any("SP-02 label changed" in e for e in result["errors"])
    data = report(bundle, records, root)["m4c_labels"]
    assert data["counts"]["changes_requested"] == 0 and data["counts"]["accepted"] == 1
    assert data["stale_records"] == 1 and data["bundle_current"] is False and data["status"] == "provisional"
    corpus = root / "corpus" / "markdown" / "p01-gated-sparse-routing.md"
    corpus.write_bytes(corpus.read_bytes() + b"\nextra\n")
    assert any(e.startswith("corpus: P01") for e in import_labels(sheet, bundle, records, root=root)["errors"])
    monkeypatch.setattr(human.markdown, "PIPELINE_VERSION", "markdown-lines-v99")
    errors = import_labels(sheet, bundle, records)["errors"]
    assert any("parsers changed" in e for e in errors)
    assert report(bundle, records)["m4c_labels"]["counts"]["unreviewed"] == 80


def full_labels(bundle, tmp_path):
    return fill(bundle, tmp_path, "labels", {r["question_id"]: accept() for r in rows_of(bundle, "labels")}, "all.csv")


def full_answers(bundle, tmp_path):
    return fill(bundle, tmp_path, "answers", {k: grade(k) for k in (SINGLE, COMPARE1, COMPARE2, INSUFFICIENT)},
                "all-answers.csv")


def copy_bundle_sources(tmp_path, bundle):
    root, corpus = copy_root(tmp_path), tmp_path / "m7a"
    shutil.copytree(human.M7A_CORPUS, corpus)
    return root, corpus


def test_control_full_review_of_unchanged_sources_is_reviewed(tmp_path, bundle):
    records = tmp_path / "records"
    assert import_labels(full_labels(bundle, tmp_path), bundle, records)["counts"] == {"added": 80}
    assert import_answers(full_answers(bundle, tmp_path), bundle, records)["counts"] == {"added": 4}
    data = report(bundle, records)
    assert data["status"] == "reviewed" and data["m4c_labels"]["status"] == "accepted_all"
    assert data["m7a_answers"]["metrics"]["grounded_answer_rate"] == {"numerator": 2, "denominator": 2, "value": 1.0}
    assert data["m7a_answers"]["metrics"]["abstention_appropriateness"]["denominator"] == 1


def test_changed_corpus_bytes_void_every_label_verdict(tmp_path, bundle):
    records = tmp_path / "records"
    root, _ = copy_bundle_sources(tmp_path, bundle)
    assert import_labels(full_labels(bundle, tmp_path), bundle, records, root=root)["written"]
    paper = root / "corpus" / "markdown" / "p01-gated-sparse-routing.md"
    paper.write_bytes(paper.read_bytes() + b"\nchanged after review\n")  # manifest still declares the old hash
    data = report(bundle, records, root)
    labels = data["m4c_labels"]
    assert labels["status"] == "source_mismatch" and data["status"] == "source_mismatch"
    assert labels["counts"]["accepted"] == 0 and labels["counts"]["unreviewed"] == 80
    assert labels["stale_records"] == 80 and labels["bundle_current"] is False and labels["corpus_errors"]
    assert "Lỗi nguồn" in human.render_report(data) and "source_mismatch" in human.render_report(data)
    paper.write_bytes(paper.read_bytes().replace(b"\nchanged after review\n", b""))
    assert report(bundle, records, root)["m4c_labels"]["status"] == "accepted_all"  # exact bytes back


def test_parser_change_voids_answer_verdicts_before_and_after_rebuild(tmp_path, bundle, monkeypatch):
    records = tmp_path / "records"
    assert import_answers(full_answers(bundle, tmp_path), bundle, records)["written"]
    changed = human.parsers() | {"markdown": "markdown-lines-v99"}
    monkeypatch.setattr(human, "parsers", lambda: changed)
    data = report(bundle, records)["m7a_answers"]
    assert data["status"] == "source_mismatch" and data["live_records"] == 0 and data["stale_records"] == 4
    for name in ("grounded_answer_rate", "citation_accuracy", "abstention_appropriateness"):
        assert data["metrics"][name] == {"numerator": 0, "denominator": 0, "value": None}, name
    fresh = tmp_path / "fresh"
    human_bundle.write(fresh, human_bundle.build(DEFAULT_ROOT, (bundle / human.SNAPSHOT_FILE).read_bytes()))
    data = report(fresh, records)["m7a_answers"]
    assert data["bundle_current"] is True and data["status"] == "awaiting_human_review"
    assert data["live_records"] == 0 and data["metrics"]["abstention_appropriateness"]["denominator"] == 0


def test_m7a_corpus_change_voids_abstention_and_blocks_import(tmp_path, bundle):
    records = tmp_path / "records"
    _, corpus = copy_bundle_sources(tmp_path, bundle)
    assert import_answers(full_answers(bundle, tmp_path), bundle, records, corpus=corpus)["written"]
    before = human.records_path(records, "answers").read_bytes()
    paper = corpus / "lumen-bench.md"
    paper.write_bytes(paper.read_bytes() + b"\nThe purchase price was 42 dollars.\n")
    data = human.build_report(bundle=bundle, records=records, corpus=corpus, today=TODAY)["m7a_answers"]
    assert data["corpus_matches_run"] is False and data["status"] == "source_mismatch" and data["live_records"] == 0
    assert data["metrics"]["abstention_appropriateness"] == {"numerator": 0, "denominator": 0, "value": None}
    errors = import_answers(full_answers(bundle, tmp_path), bundle, records, corpus=corpus)["errors"]
    assert any("does not reproduce the run" in e for e in errors)
    assert human.records_path(records, "answers").read_bytes() == before


def test_records_without_current_binding_never_count(tmp_path, bundle):
    records = tmp_path / "records"
    import_labels(fill(bundle, tmp_path, "labels", {"SP-01": accept()}), bundle, records)
    import_answers(fill(bundle, tmp_path, "answers", {INSUFFICIENT: grade(INSUFFICIENT)}), bundle, records)
    for kind in ("labels", "answers"):
        path = human.records_path(records, kind)
        data = json.loads(path.read_text(encoding="utf-8"))
        legacy = {k: v for k, v in data["records"][0].items() if k not in ("binding", "binding_sha256")}
        forged = data["records"][0] | {"binding_sha256": "0" * 64}
        data["records"] = [legacy, forged]
        path.write_text(json.dumps(data), encoding="utf-8")
    data = report(bundle, records)
    assert data["m4c_labels"]["counts"]["accepted"] == 0 and data["m4c_labels"]["stale_records"] == 2
    assert data["m7a_answers"]["live_records"] == 0 and data["m7a_answers"]["stale_records"] == 2
    assert data["status"] == "awaiting_human_review"


@pytest.mark.parametrize("kind, path, change", [
    ("labels", "labels/SP-01.html", "edit"),
    ("labels", "papers/P01.html", "delete"),
    ("labels", "files/markdown/p01-gated-sparse-routing.md", "edit"),
    ("labels", "manifest.json", "edit"),
    ("answers", "answers/01-single.html", "edit"),
    ("answers", "papers/m7a-lumen-bench.html", "delete"),
    ("answers", "files/m7a/harbor-sensor.pdf", "edit"),
])
def test_import_refuses_a_bundle_whose_displayed_content_drifted(tmp_path, bundle, kind, path, change):
    records = tmp_path / "records"
    sheet = fill(bundle, tmp_path, kind, {"SP-01": accept()} if kind == "labels" else {SINGLE: grade(SINGLE)})
    # Earlier valid import: a refused import must leave these bytes untouched.
    seed = fill(bundle, tmp_path, kind, {"SP-02": accept()} if kind == "labels" else {COMPARE1: grade(COMPARE1)},
                "seed.csv")
    assert (import_labels if kind == "labels" else import_answers)(seed, bundle, records)["written"]
    before = human.records_path(records, kind).read_bytes()
    target = bundle / path
    if change == "delete":
        target.unlink()
    elif path.endswith(".html"):
        target.write_bytes(target.read_bytes().replace(b"<mark>", b"<mark>WRONG ", 1))
    else:
        target.write_bytes(target.read_bytes() + b" ")
    for dry_run in (True, False):
        result = (import_labels if kind == "labels" else import_answers)(sheet, bundle, records, dry_run=dry_run)
        assert any(e.startswith("bundle drift:") and path in e for e in result["errors"]), result["errors"]
        assert not result["written"]
    assert human.records_path(records, kind).read_bytes() == before


def test_filled_bundle_sheet_and_extra_files_are_not_drift(tmp_path, bundle):
    records = tmp_path / "records"
    in_place = fill(bundle, bundle, "labels", {"SP-01": accept()}, name=human.LABEL_SHEET)
    (bundle / "~$label-review.csv").write_bytes(b"spreadsheet lock file")
    result = import_labels(in_place, bundle, records)
    assert result["errors"] == [] and result["counts"] == {"added": 1} and len(result["pending"]) == 79


def test_reviewer_conflicts_and_explicit_replace(tmp_path, bundle):
    records = tmp_path / "records"
    assert import_labels(fill(bundle, tmp_path, "labels", {"SP-01": accept()}), bundle, records)["written"]
    other = fill(bundle, tmp_path, "labels", {"SP-01": accept("fixture-reviewer-b", "rejected", "wrong side")}, "b.csv")
    assert import_labels(other, bundle, records)["written"]
    data = report(bundle, records)["m4c_labels"]
    assert data["counts"]["conflicting"] == 1 and data["not_accepted"] == {"SP-01": "conflicting"}
    changed = fill(bundle, tmp_path, "labels", {"SP-01": accept(verdict="rejected", notes="changed mind")}, "c.csv")
    assert any("--replace" in e for e in import_labels(changed, bundle, records)["errors"])
    assert import_labels(changed, bundle, records, replace=True)["counts"] == {"replaced": 1}
    assert report(bundle, records)["m4c_labels"]["counts"]["rejected"] == 1


def test_label_report_separates_states_splits_and_groups(tmp_path, bundle):
    records = tmp_path / "records"
    empty = report(bundle, records)
    assert empty["status"] == "awaiting_human_review" and empty["m4c_labels"]["status"] == "awaiting_human_review"
    assert empty["m4c_labels"]["counts"] == {"accepted": 0, "changes_requested": 0, "rejected": 0,
                                             "conflicting": 0, "unreviewed": 80}
    assert empty["m4c_answers"]["status"] == "not_measured"
    holdout = next(r["question_id"] for r in rows_of(bundle, "labels") if r["split"] == "holdout")
    import_labels(fill(bundle, tmp_path, "labels", {"SP-01": accept(), holdout: accept()}), bundle, records)
    data = report(bundle, records)["m4c_labels"]
    assert data["status"] == "provisional" and data["counts"]["accepted"] == 2 and data["counts"]["unreviewed"] == 78
    assert data["by_split"]["holdout"]["accepted"] == 1 and data["by_split"]["dev"]["accepted"] == 1
    assert data["by_group"]["single_paper"]["accepted"] == 2 - (not holdout.startswith("SP"))
    assert "provisional" in data["effect"]


# --- answer import and scoring ------------------------------------------------------------

def test_answer_states_unreviewed_partial_and_complete(tmp_path, bundle):
    records = tmp_path / "records"
    data = report(bundle, records)["m7a_answers"]
    assert data["status"] == "awaiting_human_review"
    assert data["metrics"]["grounded_answer_rate"] == {"numerator": 0, "denominator": 0, "value": None}
    assert data["metrics"]["abstention_appropriateness"]["value"] is None
    partial = fill(bundle, tmp_path, "answers", {COMPARE1: grade(COMPARE1)})
    assert import_answers(partial, bundle, records)["written"]
    data = report(bundle, records)["m7a_answers"]
    assert data["status"] == "provisional"
    assert data["metrics"]["answered_responses"]["incomplete_ids"] == ["test-run/02-compare"]
    assert data["metrics"]["grounded_answer_rate"]["denominator"] == 0
    full = fill(bundle, tmp_path, "answers", {key: grade(key) for key in (SINGLE, COMPARE1, INSUFFICIENT)}
                | {COMPARE2: grade(COMPARE2, supported="yes", correct="no")}, "full.csv")
    assert import_answers(full, bundle, records)["counts"] == {"added": 3, "unchanged": 1}
    data = report(bundle, records)
    m = data["m7a_answers"]["metrics"]
    assert m["grounded_answer_rate"] == {"numerator": 2, "denominator": 2, "value": 1.0}
    assert m["citation_accuracy"] == {"numerator": 2, "denominator": 3, "value": 0.6667}
    assert m["abstention_appropriateness"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert data["m7a_answers"]["status"] == "reviewed"
    assert data["m7a_answers"]["by_group"]["comparison"]["citation_accuracy"]["denominator"] == 2
    assert data["m4c_labels"]["status"] == "awaiting_human_review" and data["status"] == "provisional"
    assert data["m7a_answers"]["not_scoreable"] == [{"response_id": "test-run/chrome", "reason": "screen_text_only"}]


def test_answer_sheet_structure_and_content_must_match(tmp_path, bundle):
    records = tmp_path / "records"
    rows = [row | grade(row["row_key"]) for row in rows_of(bundle, "answers")]
    cases = {
        "missing citation row": ([r for r in rows if r["row_key"] != COMPARE2], "row_key test-run/02-compare#c1.2: row missing"),
        "missing response": ([r for r in rows if "single" not in r["row_key"]], "row_key test-run/01-single#c1.1: row missing"),
        "duplicate": (rows + [rows[1]], "duplicate row_key"),
        "edited claim": ([rows[0] | {"claim_text": "Drift was 0.5 percent."}] + rows[1:], "claim_text differ"),
        "edited quote": ([rows[0] | {"citation_quote": "a drift"}] + rows[1:], "citation_quote differ"),
        "edited provenance": ([rows[0] | {"citation_item": rows[0]["citation_item"].replace("v-lumen", "v-other")}]
                              + rows[1:], "citation_item differ"),
        "edited response": ([rows[0] | {"row_key": "test-run/09-single#c1.1"}] + rows[1:], "unknown row_key"),
        "edited count": ([rows[0] | {"response_claims": "2"}] + rows[1:], "response_claims differ"),
    }
    for name, (sheet_rows, message) in cases.items():
        result = import_answers(write_sheet(tmp_path / "x.csv", "answers", sheet_rows), bundle, records)
        assert any(message in e for e in result["errors"]), (name, result["errors"])
        assert not records.exists()


@pytest.mark.parametrize("key, cells, message", [
    (SINGLE, {"human_claim_supported": "yes", "human_citation_correct": "yes"}, "need reviewer and review_date"),
    (SINGLE, {"human_claim_supported": "yes", "human_citation_correct": "yes", "reviewer": "fixture-reviewer-a"},
     "need reviewer and review_date"),
    (SINGLE, {"reviewer": "fixture-reviewer-a", "review_date": "2026-10-19", "human_claim_supported": "yes"},
     "missing human_citation_correct"),
    (SINGLE, grade(SINGLE) | {"human_claim_supported": "probably"}, "is not one of"),
    (SINGLE, grade(SINGLE, abstention="yes"), "applies only to questions expected not to be answered"),
    (INSUFFICIENT, {"reviewer": "fixture-reviewer-a", "review_date": "2026-10-19", "notes": "x"},
     "missing human_abstention_appropriate"),
    (INSUFFICIENT, grade(INSUFFICIENT) | {"human_claim_supported": "yes"}, "no claim/citation to grade"),
])
def test_answer_rows_need_complete_judgements(tmp_path, bundle, key, cells, message):
    records = tmp_path / "records"
    result = import_answers(fill(bundle, tmp_path, "answers", {key: cells}), bundle, records)
    assert any(message in e for e in result["errors"]), result["errors"]
    assert not records.exists()


def test_contradictory_answer_judgements_are_rejected_or_reported(tmp_path, bundle):
    records = tmp_path / "records"
    split = fill(bundle, tmp_path, "answers", {COMPARE1: grade(COMPARE1, supported="yes"),
                                               COMPARE2: grade(COMPARE2, supported="no")})
    assert any("conflicting human_claim_supported" in e for e in import_answers(split, bundle, records)["errors"])
    wrong = fill(bundle, tmp_path, "answers", {SINGLE: grade(SINGLE, supported="yes", correct="no")})
    assert any("every citation is marked incorrect" in e for e in import_answers(wrong, bundle, records)["errors"])
    assert not records.exists()
    # Two reviewers who disagree are both kept; the response is reported conflicting, not scored.
    assert import_answers(fill(bundle, tmp_path, "answers", {SINGLE: grade(SINGLE)}), bundle, records)["written"]
    other = fill(bundle, tmp_path, "answers", {SINGLE: grade(SINGLE, "no", "no", "fixture-reviewer-b")}, "b.csv")
    assert import_answers(other, bundle, records)["written"]
    m = report(bundle, records)["m7a_answers"]["metrics"]
    assert m["answered_responses"]["conflicting_ids"] == ["test-run/01-single"]
    assert m["grounded_answer_rate"]["denominator"] == 0 and m["citation_accuracy"]["denominator"] == 0


def test_two_runs_of_one_question_and_mode_stay_separate(tmp_path, m4c_parts):
    single = answer("single", "How much drift?", "answered", [{"text": "Drift was 0.4 percent.", "citations": [LUMEN]}])
    snap = snapshot([single, single | {"claims": [{"text": "Drift was 4 percent.", "citations": [LUMEN]}]}])
    bundle = tmp_path / "bundle"
    human_bundle.write(bundle, human_bundle.build(DEFAULT_ROOT, human.dump_json(snap), m4c_parts=m4c_parts))
    rows = rows_of(bundle, "answers")
    assert [r["response_id"] for r in rows] == ["test-run/01-single", "test-run/02-single"]
    assert {r["question_id"] for r in rows} == {"M7A-single"} and {r["mode"] for r in rows} == {"keyword"}
    provenance = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["m7a"]["responses"]
    assert [r["response_id"] for r in provenance] == ["test-run/01-single", "test-run/02-single", "test-run/chrome"]
    assert all(r["call"] is None for r in human.m7a_responses(snap, None, None)[:2])  # ledger route ambiguous
    records = tmp_path / "records"
    sheet = fill(bundle, tmp_path, "answers", {"test-run/01-single#c1.1": grade("x"),
                                               "test-run/02-single#c1.1": grade("x", "no", "yes")})
    assert import_answers(sheet, bundle, records)["written"]
    m = report(bundle, records)["m7a_answers"]["metrics"]
    assert m["grounded_answer_rate"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert m["answered_responses"]["conflicting"] == 0
    # Without response IDs the same rows would merge into one conflicting response (M4c sheets).
    merged = [{k: v for k, v in row.items() if k != "response_id"} | grade("x", s, "yes")
              for row, s in zip(rows, ("yes", "no"))]
    assert review.score(merged)["answered_responses"]["conflicting_ids"] == ["M7A-single/keyword"]


def test_stale_answer_bundle_and_snapshot_change(tmp_path, bundle, m4c_parts):
    records = tmp_path / "records"
    sheet = fill(bundle, tmp_path, "answers", {SINGLE: grade(SINGLE)})
    snap_path = bundle / human.SNAPSHOT_FILE
    original = snap_path.read_bytes()
    changed = json.loads(original)
    changed["answers"][0]["claims"][0]["text"] = "Drift was 0.5 percent."
    snap_path.write_bytes(human.dump_json(changed))
    assert any("snapshot changed" in e for e in import_answers(sheet, bundle, records)["errors"])
    snap_path.write_bytes(original)
    assert import_answers(sheet, bundle, records)["written"]
    # Re-preparing from a different run voids earlier answer records instead of carrying them over.
    other = tmp_path / "other"
    human_bundle.write(other, human_bundle.build(DEFAULT_ROOT, human.dump_json(changed), m4c_parts=m4c_parts))
    data = report(other, records)["m7a_answers"]
    assert data["live_records"] == 0 and data["stale_records"] == 1 and data["status"] == "awaiting_human_review"


def test_missing_artifacts_are_not_available(tmp_path, m4c_parts):
    state = tmp_path / "state"
    (state / "runtime").mkdir(parents=True)
    (state / ".env").write_text("OPENROUTER_API_KEY=should-never-be-read", encoding="utf-8")
    (state / "runtime" / "notes.txt").write_text("unrelated", encoding="utf-8")
    snap = human.m7a_snapshot(state, "test-run")
    assert snap["answers"] is None and set(snap["source_sha256"]) == {
        "answers.json", "chrome-answer.txt", "targets.json", "corpus.json", "ledger.jsonl"}
    assert "should-never-be-read" not in json.dumps(snap)
    bundle = tmp_path / "bundle"
    human_bundle.write(bundle, human_bundle.build(DEFAULT_ROOT, human.dump_json(snap), m4c_parts=m4c_parts))
    assert rows_of(bundle, "answers") == []
    data = report(bundle, tmp_path / "records")
    assert data["m7a_answers"]["status"] == "not_available" and data["status"] == "awaiting_human_review"
    sheet = write_sheet(tmp_path / "a.csv", "answers", [])
    assert any("not available" in e for e in import_answers(sheet, bundle, tmp_path / "records")["errors"])


def test_m7a_bindings_machine_checks_and_unscored_responses():
    files = human.m7a_files()
    texts = human.m7a_texts(files)
    snap = snapshot([
        answer("single", "q", "answered", [{"text": "t", "citations": [LUMEN, LUMEN | {"quote": "invented quote"},
                                                                     HARBOR | {"version_id": "v-unknown"}]}]),
        answer("mystery", "q", "answered", []),
        answer("compare", "q", "answered", [{"text": "t", "citations": []}]),
    ])
    responses = human.m7a_responses(snap, files, texts)
    rows = responses[0]["rows"]
    assert [r["machine_file_binding"] for r in rows] == [
        "derived:drill_upload_plan_user_a", "derived:drill_upload_plan_user_a", "unavailable:pdf_version_not_in_targets"]
    assert [r["machine_quote_verbatim"] for r in rows] == ["yes", "no", "not_available"]
    assert {r["machine_id_in_evidence"] for r in rows} == {r["machine_locator_covers_gold"] for r in rows} == {"not_available"}
    assert [(r["label"], r["scoreable"], r["not_scoreable_reason"]) for r in responses[1:]] == [
        ("mystery", False, "drill_mapping_unknown"), ("compare", False, "claim_without_citation"),
        ("chrome", False, "screen_text_only")]
    mismatch = human.m7a_responses(snapshot(corpus_ok=False), files, texts)
    assert {r["machine_quote_verbatim"] for r in mismatch[0]["rows"]} == {"not_available"}
    harbor = human.m7a_responses(snapshot(), files, texts)[1]["rows"][1]
    assert harbor["machine_file_binding"] == "recorded:targets.json" and harbor["machine_quote_verbatim"] == "yes"
    # One answer but two ledger calls on its route (a retried run): the call is not guessed.
    retried = snapshot()
    retried["generation_calls"].append(retried["generation_calls"][0] | {"seq": 9, "generation_id": "gen-test-9"})
    calls = [r["call"] for r in human.m7a_responses(retried, files, texts)]
    assert calls[0] is None and calls[1]["generation_id"] == "gen-test-2"


def test_ledger_extraction_keeps_only_rag_generation_metadata():
    lines = [
        {"event": "sending", "seq": 1, "route": "runtime:rag-single:generation"},
        {"event": "outcome", "seq": 1, "route": "runtime:rag-single", "status": "completed", "provider_cost_usd": 0.002,
         "observed": [{"event": "response", "id": "gen-1", "model": "m", "provider": "P", "selected": [],
                       "usage": {"prompt_tokens": 3, "completion_tokens": 4}, "body": "secret-ish payload"}]},
        {"event": "outcome", "seq": 2, "route": "runtime:a-lumen", "status": "completed", "observed": []},
    ]
    calls = human._ledger_calls("\n".join(json.dumps(line) for line in lines).encode())
    assert calls == [{"seq": 1, "route": "runtime:rag-single", "status": "completed", "provider_cost_usd": 0.002,
                      "error_code": None, "generation_id": "gen-1", "model": "m", "provider": "P", "selected": [],
                      "finish_reason": None, "prompt_tokens": 3, "completion_tokens": 4}]


def test_cli_prepare_validate_import_report(tmp_path, capsys):
    bundle, records, out = tmp_path / "bundle", tmp_path / "records", tmp_path / "out"
    snap = tmp_path / "snap.json"
    snap.write_bytes(human.dump_json(snapshot()))
    assert main(["human-prepare", "--bundle", str(bundle), "--m7a-snapshot", str(snap)]) == 0
    assert main(["human-prepare", "--bundle", str(bundle), "--check"]) == 0
    sheet = fill(bundle, tmp_path, "labels", {"SP-01": accept() | {"review_date": "2026-01-02"}})
    common = ["--bundle", str(bundle), "--records", str(records)]
    assert main(["human-validate", "labels", str(sheet), *common]) == 0 and not records.exists()
    assert main(["human-import", "labels", str(sheet), *common]) == 0
    bad = fill(bundle, tmp_path, "answers", {SINGLE: {"human_claim_supported": "yes"}}, "bad.csv")
    assert main(["human-import", "answers", str(bad), *common]) == 1
    assert "nothing was recorded" in capsys.readouterr().out
    assert main(["human-report", *common, "--output", str(out)]) == 0
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["m4c_labels"]["counts"]["accepted"] == 1 and data["m7a_answers"]["status"] == "awaiting_human_review"
    assert "provisional" in (out / "report.md").read_text(encoding="utf-8")
    fill(bundle, bundle, "labels", {"SP-01": accept()}, name=human.LABEL_SHEET)
    assert main(["human-prepare", "--bundle", str(bundle)]) == 1  # would drop a filled sheet
