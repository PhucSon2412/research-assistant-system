"""M7b human review: label review for the M4c set, answer review for the M7a live responses.

The two sets stay apart: the 80 M4c questions have agent-written labels and no generated
answers; the M7a samples are live answers on a different synthetic corpus with no gold
labels. Their sheets, records and report sections never share a denominator.

Nothing here decides a human verdict. Sheets are filled by a person; import only checks
that each filled row is complete, binds to the exact label/response content the bundle
showed, and records it with the reviewer name the person typed (self-reported
attribution, not a verified identity). Reports count reviewed, pending, stale and
conflicting rows; an empty set is ``awaiting_human_review`` and a zero denominator is
``None``, never 0 % or 100 %.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.config import IngestPolicy, PdfBodyThreshold
from app.evaluation import review
from app.evaluation.corpus import DEFAULT_ROOT
from app.evaluation.schema import GROUP_MINIMUMS, Question, QuestionSet, Scope, check_manifest, load
from app.ingest import markdown, pdf

FORMAT = "ras-m7b-human-review/1"
RECORDS_FORMAT = "ras-m7b-review-records/1"
SNAPSHOT_FORMAT = "ras-m7b-m7a-snapshot/1"
HUMAN_DIR = DEFAULT_ROOT.parent / "human-review"
DEFAULT_BUNDLE = HUMAN_DIR / "bundle"
DEFAULT_RECORDS = HUMAN_DIR / "records"
M7A_CORPUS = DEFAULT_ROOT.parent / "m7a-synthetic"
DEFAULT_RUN_ID = "m7a-runtime-2026-10-18"
LABEL_SHEET = "label-review.csv"
ANSWER_SHEET = "answer-review.csv"
SNAPSHOT_FILE = "sources/m7a-snapshot.json"
ATTRIBUTION = ("self_reported: reviewer và review_date do người điền phiếu tự ghi; "
               "không phải danh tính hay chữ ký đã được xác thực độc lập")

LABEL_HUMAN = ["verdict", "reviewer", "review_date", "notes"]
LABEL_DISPLAY = ["question_id", "split", "group", "identity", "scope", "question", "expected_status",
                 "expected_reason", "open_page", "agent_note"]
LABEL_COLUMNS = LABEL_DISPLAY + LABEL_HUMAN
VERDICTS = ("accepted", "changes_requested", "rejected")

ANSWER_HUMAN = ["human_claim_supported", "human_citation_correct", "human_abstention_appropriate",
                "reviewer", "review_date", "notes"]
ANSWER_COLUMNS = ["run_id", "response_id", "row_key", *review.COLUMNS, "machine_file_binding", "open_page"]
ANSWER_DISPLAY = [column for column in ANSWER_COLUMNS if column not in ANSWER_HUMAN]

# How the M7a runtime drill asked each question (scripts/m7a_runtime_drill.py): synthetic user A,
# keyword mode; ``rag-<label>`` / ``chrome-rag`` are its ledger routes. ``expected_status`` is
# the drill's design intent, not a reviewed gold label.
M7A_DRILL = {
    "single": {"group": "single_paper", "scope": "item a-lumen (lumen-bench.md)", "expected_status": "answered"},
    "compare": {"group": "comparison", "scope": "all (a-lumen, a-harbor)", "expected_status": "answered"},
    "insufficient": {"group": "no_answer", "scope": "item a-lumen (lumen-bench.md)",
                     "expected_status": "insufficient_evidence"},
    "chrome": {"group": "single_paper", "scope": "item a-harbor (harbor-sensor.pdf)", "expected_status": "answered"},
}
M7A_MODE = "keyword"
M7A_IDENTITY = "user_a"
# User A uploaded exactly one Markdown and one PDF file in the drill; B's same-bytes copy is not
# visible to A, so a locator kind of A's answer names one corpus file.
M7A_FILES = {"markdown": "lumen-bench.md", "pdf": "harbor-sensor.pdf"}
NOT_AVAILABLE = "not_available"

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r", "'")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ReviewError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def digest(value) -> str:
    """SHA-256 of canonical JSON, so the same content always binds to the same hash."""
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


# --- sheets -------------------------------------------------------------------------------

def cell_out(value: str) -> str:
    """Keep a spreadsheet from running text as a formula; ``cell_in`` reverses it exactly."""
    return "'" + value if value[:1] in _DANGEROUS else value


def cell_in(value: str) -> str:
    return value[1:] if value.startswith("'") else value


def sheet_bytes(columns: list[str], rows: list[dict]) -> bytes:
    """UTF-8 with BOM so a spreadsheet shows non-ASCII text; LF line ends."""
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows({column: cell_out(row.get(column, "")) for column in columns} for row in rows)
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def read_sheet(path: Path, columns: list[str]) -> tuple[list[tuple[int, dict]], list[str]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], [f"{path.name}: file not found"]
    return parse_sheet(data, columns, path.name)


def parse_sheet(data: bytes, columns: list[str], name: str = "sheet") -> tuple[list[tuple[int, dict]], list[str]]:
    """Rows with their CSV line numbers; header and encoding problems are errors, never guessed."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [f"{name}: not UTF-8; save it as 'CSV UTF-8'"]
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return [], [f"{name}: empty file"]
    except csv.Error as exc:
        return [], [f"{name}: unreadable CSV ({exc})"]
    if header != columns:
        missing = [c for c in columns if c not in header]
        extra = [c for c in header if c not in columns]
        return [], [f"{name}: columns differ from the bundle sheet (missing {missing}, unexpected {extra}"
                    f"{', order changed' if not missing and not extra else ''})"]
    rows = []
    errors = []
    try:
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue
            line = reader.line_num
            if len(record) != len(columns):
                errors.append(f"line {line}: {len(record)} cells, expected {len(columns)}")
                continue
            rows.append((line, {column: cell_in(cell) for column, cell in zip(columns, record)}))
    except csv.Error as exc:
        errors.append(f"{name}: unreadable CSV ({exc})")
    return rows, errors


# --- M4c labels ---------------------------------------------------------------------------

def parsers() -> dict:
    return {"markdown": markdown.PIPELINE_VERSION, "pdf": pdf.PIPELINE_VERSION}


def label_hash(question: Question) -> str:
    """Binds the whole question/label: text, scope, expectation, every span, non-evidence, rationale."""
    return digest(question.model_dump(mode="json", exclude_none=True))


def m4c_binding(root: Path = DEFAULT_ROOT) -> tuple[dict, QuestionSet]:
    manifest, qset = load(root)
    return {"dataset_id": qset.dataset_id, "corpus_id": manifest.corpus_id,
            "corpus_sha256": digest(manifest.model_dump(mode="json")), "parsers": parsers(),
            "corpus_errors": check_manifest(manifest, root),
            "labels": {question.id: label_hash(question) for question in qset.questions}}, qset


def scope_text(scope: Scope) -> str:
    return f"{scope.kind} {scope.item or scope.collection}" if scope.kind != "all" else "all"


def agent_note(question: Question) -> str:
    """Points the reader at expectations the docs flagged as product choices; never a verdict."""
    reason = question.expected.reason
    if reason == "bibliography_only":
        return "Agent: kỳ vọng dựa trên hợp đồng loại reference list khỏi evidence; xác nhận có hợp ý sản phẩm."
    if reason == "unresolved_conflict":
        return "Agent: kỳ vọng insufficient vì hai nguồn mâu thuẫn; xác nhận có hợp ý sản phẩm."
    if question.group == "comparison" and question.expected.status != "answered":
        return "Agent: so sánh chỉ có một phía; xác nhận kỳ vọng insufficient có hợp ý sản phẩm."
    return ""


def label_rows(qset: QuestionSet) -> list[dict]:
    return [{"question_id": q.id, "split": q.split, "group": q.group, "identity": q.identity,
             "scope": scope_text(q.scope), "question": q.question, "expected_status": q.expected.status,
             "expected_reason": q.expected.reason or "", "open_page": f"labels/{q.id}.html",
             "agent_note": agent_note(q)} | {column: "" for column in LABEL_HUMAN}
            for q in qset.questions]


# --- M7a responses ------------------------------------------------------------------------

def render_harbor_pdf(text: str) -> bytes:
    """Exactly the M7a drill's rendering, so the bytes can be checked against its recorded SHA-256."""
    from reportlab.pdfgen import canvas

    out = io.BytesIO()
    document = canvas.Canvas(out, pagesize=(612, 792), pageCompression=0, invariant=1)
    y = 760
    for line in text.splitlines():
        document.drawString(36, y, line)
        y -= 14
    document.showPage()
    document.save()
    return out.getvalue()


def m7a_files(corpus: Path = M7A_CORPUS) -> dict[str, bytes]:
    return {"lumen-bench.md": (corpus / "lumen-bench.md").read_bytes(),
            "harbor-sensor.pdf": render_harbor_pdf((corpus / "harbor-sensor.txt").read_text(encoding="utf-8"))}


def m7a_texts(files: dict[str, bytes]) -> dict[str, dict]:
    """Production-parser text of each M7a file: Markdown chunks by start line, PDF pages and segments."""
    policy = IngestPolicy()
    original = files["lumen-bench.md"].decode("utf-8")
    chunks = [chunk for section in markdown.extract(original).sections if section.kind in ("body", "abstract")
              for chunk in markdown.chunk_section(original, section, policy.markdown_max_chunk_chars)]
    settings = SimpleNamespace(ingest=policy, pdf_body=PdfBodyThreshold())
    raw = files["harbor-sensor.pdf"]
    pages = pdf.parse(raw, 1, settings)
    result = pdf.classify(pages, settings)
    return {"lumen-bench.md": {"kind": "markdown", "text": original,
                               "chunks": [(c.start_line, c.end_line, c.start_offset, c.end_offset) for c in chunks]},
            "harbor-sensor.pdf": {"kind": "pdf", "pages": {p.number: p.text for p in pages},
                                  "segments": [(s.page, s.start, s.end, s.kind) for s in result.segments]}}


def _read_optional(path: Path, sources: dict) -> bytes | None:
    if not path.is_file():
        sources[path.name] = None
        return None
    data = path.read_bytes()
    sources[path.name] = sha256_bytes(data)
    return data


def _ledger_calls(data: bytes | None) -> list[dict]:
    """Only the generation outcomes of RAG questions, reduced to IDs/model/provider/usage."""
    calls = []
    for line in (data or b"").decode("utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        route = entry.get("route", "")
        if entry.get("event") != "outcome" or not (route.startswith("runtime:rag-") or route == "runtime:chrome-rag"):
            continue
        observed = next((o for o in entry.get("observed") or [] if o.get("event") == "response"), {})
        usage = observed.get("usage") or {}
        calls.append({"seq": entry.get("seq"), "route": route, "status": entry.get("status"),
                      "provider_cost_usd": entry.get("provider_cost_usd"), "error_code": entry.get("error_code"),
                      "generation_id": observed.get("id"), "model": observed.get("model"),
                      "provider": observed.get("provider"), "selected": observed.get("selected"),
                      "finish_reason": observed.get("finish_reason"),
                      "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")})
    return calls


def m7a_snapshot(state: Path, run_id: str = DEFAULT_RUN_ID) -> dict:
    """Read only the result/provenance files the review needs; a missing file stays ``None``."""
    runtime = state / "runtime"
    sources: dict[str, str | None] = {}
    answers = _read_optional(runtime / "answers.json", sources)
    chrome = _read_optional(runtime / "chrome-answer.txt", sources)
    targets = _read_optional(runtime / "targets.json", sources)
    corpus = _read_optional(runtime / "corpus.json", sources)
    ledger = _read_optional(state / "ledger.jsonl", sources)
    return {"format": SNAPSHOT_FORMAT, "run_id": run_id, "origin": state.as_posix(), "source_sha256": sources,
            "answers": json.loads(answers) if answers is not None else None,
            "chrome_text": chrome.decode("utf-8") if chrome is not None else None,
            "targets": json.loads(targets) if targets is not None else None,
            "corpus": json.loads(corpus) if corpus is not None else None,
            "generation_calls": _ledger_calls(ledger)}


def empty_snapshot(run_id: str = DEFAULT_RUN_ID) -> dict:
    return {"format": SNAPSHOT_FORMAT, "run_id": run_id, "origin": None, "source_sha256": {}, "answers": None,
            "chrome_text": None, "targets": None, "corpus": None, "generation_calls": []}


def _call(snapshot: dict, route: str) -> dict | None:
    matches = [call for call in snapshot["generation_calls"] if call["route"] == route]
    return matches[0] if len(matches) == 1 else None  # two calls on one route: no guessed binding


def _file_binding(citation: dict, snapshot: dict) -> tuple[str | None, str]:
    """Which corpus file a citation names, and on what basis; never inferred from its title."""
    harbor = (snapshot.get("targets") or {}).get("harbor") or {}
    kind = citation.get("locator_kind")
    if harbor.get("version_id") and citation.get("version_id") == harbor.get("version_id"):
        if harbor.get("file_id") == citation.get("source_file_id") and kind == "pdf":
            return "harbor-sensor.pdf", "recorded:targets.json"
        return None, "unavailable:targets_mismatch"
    if kind == "pdf" and harbor.get("version_id"):
        return None, "unavailable:pdf_version_not_in_targets"
    if kind in M7A_FILES and citation.get("version_id") and citation.get("source_file_id"):
        return M7A_FILES[kind], "derived:drill_upload_plan_user_a"
    return None, "unavailable:not_recorded"


def _quote_check(citation: dict, name: str | None, texts: dict | None) -> str:
    """Quote is verbatim in the production-parser text of the cited chunk start line / page."""
    if name is None or texts is None or not citation.get("quote"):
        return NOT_AVAILABLE
    source = texts[name]
    quote = citation["quote"]
    if source["kind"] == "markdown":
        starts = [c for c in source["chunks"] if c[0] == citation.get("start_line")]
        if len(starts) != 1:
            return NOT_AVAILABLE
        _, _, start, end = starts[0]
        return "yes" if quote in source["text"][start:end] else "no"
    page = source["pages"].get(citation.get("start_page"))
    return NOT_AVAILABLE if page is None else "yes" if quote in page else "no"


def m7a_corpus_matches(snapshot: dict, files: dict[str, bytes]) -> bool | None:
    recorded = snapshot.get("corpus") or {}
    if not recorded:
        return None
    return (recorded.get("lumen-md_sha256") == sha256_bytes(files["lumen-bench.md"])
            and recorded.get("harbor-pdf_sha256") == sha256_bytes(files["harbor-sensor.pdf"]))


def m7a_responses(snapshot: dict, files: dict[str, bytes] | None, texts: dict | None) -> list[dict]:
    """Normalised responses with their sheet rows; unstructured or unmapped ones are listed, not scored."""
    run_id = snapshot["run_id"]
    matches = m7a_corpus_matches(snapshot, files) if files is not None else None
    usable_texts = texts if matches else None
    responses = []
    labels = Counter(str(entry.get("label")) for entry in snapshot.get("answers") or [])
    for index, entry in enumerate(snapshot.get("answers") or [], 1):
        label = str(entry.get("label") or f"unlabelled{index}")
        response_id = f"{run_id}/{index:02d}-{label}"
        drill = M7A_DRILL.get(label)
        claims = entry.get("claims") or []
        reason = (None if drill else "drill_mapping_unknown") or (
            "claim_without_citation" if any(not c.get("citations") for c in claims) else None)
        response = {"response_id": response_id, "label": label, "question": entry.get("question", ""),
                    "drill": drill, "structured": True, "scoreable": reason is None, "not_scoreable_reason": reason,
                    "observed_status": entry.get("status") or "", "observed_reason": entry.get("reason"),
                    "http": entry.get("http"), "error": entry.get("error"), "provider_calls": entry.get("provider_calls"),
                    # Two answers with one label cannot be told apart in the ledger: no guessed call.
                    "call": _call(snapshot, f"runtime:rag-{label}") if labels[label] == 1 else None,
                    "response_sha256": digest(entry),
                    "claims": [], "rows": []}
        if drill is None:
            responses.append(response)
            continue
        slug = f"{index:02d}-{label}"
        base = {column: "" for column in ANSWER_COLUMNS} | {
            "run_id": run_id, "response_id": response_id, "question_id": f"M7A-{label}", "split": "m7a",
            "group": drill["group"], "identity": M7A_IDENTITY, "scope": drill["scope"], "mode": M7A_MODE,
            "expected_status": drill["expected_status"], "gold_locators": "none (M7a has no gold label)",
            "observed_status": response["observed_status"], "response_claims": str(len(claims)),
            "open_page": f"answers/{slug}.html"}
        for claim_no, claim in enumerate(claims, 1):
            cited = []
            for citation_no, citation in enumerate(claim.get("citations") or [], 1):
                name, basis = _file_binding(citation, snapshot)
                kind = citation.get("locator_kind")
                where = f"p{citation.get('start_page')}" if kind == "pdf" else f"L{citation.get('start_line')}"
                check = _quote_check(citation, name, usable_texts)
                cited.append({"citation": citation, "file": name, "basis": basis, "quote_check": check})
                response["rows"].append(base | {
                    "row_key": f"{response_id}#c{claim_no}.{citation_no}", "claim_no": str(claim_no),
                    "claim_text": claim.get("text", ""), "claim_citations": str(len(claim["citations"])),
                    "citation_no": str(citation_no),
                    "citation_item": f"{citation.get('title')} | version {citation.get('version_id')} | "
                                     f"file {citation.get('source_file_id')}",
                    "citation_locator": where, "citation_quote": citation.get("quote", ""),
                    "machine_id_in_evidence": NOT_AVAILABLE, "machine_quote_verbatim": check,
                    "machine_locator_covers_gold": NOT_AVAILABLE, "machine_file_binding": basis})
            response["claims"].append({"text": claim.get("text", ""), "comparison": claim.get("comparison"),
                                       "citations": cited})
        if not claims:
            response["rows"].append(base | {"row_key": f"{response_id}#answer"})
        responses.append(response)
    if snapshot.get("chrome_text") is not None:
        responses.append({"response_id": f"{run_id}/chrome", "label": "chrome", "question": None,
                          "drill": M7A_DRILL["chrome"], "structured": False, "scoreable": False,
                          "not_scoreable_reason": "screen_text_only", "observed_status": "", "observed_reason": None,
                          "screen_text": snapshot["chrome_text"], "call": _call(snapshot, "runtime:chrome-rag"),
                          "response_sha256": digest(snapshot["chrome_text"]), "claims": [], "rows": []})
    return responses


def row_hash(row: dict) -> str:
    return digest({column: row[column] for column in ANSWER_DISPLAY})


def m7a_binding(snapshot_bytes: bytes, corpus: Path = M7A_CORPUS) -> tuple[dict, list[dict], dict]:
    snapshot = json.loads(snapshot_bytes)
    if snapshot.get("format") != SNAPSHOT_FORMAT:
        raise ReviewError([f"M7a snapshot format {snapshot.get('format')!r} is not {SNAPSHOT_FORMAT}"])
    files = m7a_files(corpus)
    texts = m7a_texts(files)
    responses = m7a_responses(snapshot, files, texts)
    binding = {"run_id": snapshot["run_id"], "snapshot_sha256": sha256_bytes(snapshot_bytes),
               "available": snapshot.get("answers") is not None, "parsers": parsers(),
               "corpus_sha256": {name: sha256_bytes(data) for name, data in sorted(files.items())},
               "corpus_matches_run": m7a_corpus_matches(snapshot, files),
               "responses": [{"response_id": r["response_id"], "label": r["label"], "structured": r["structured"],
                              "scoreable": r["scoreable"], "not_scoreable_reason": r["not_scoreable_reason"],
                              "response_sha256": r["response_sha256"],
                              "rows": {row["row_key"]: row_hash(row) for row in r["rows"]}} for r in responses]}
    return binding, responses, {"snapshot": snapshot, "files": files, "texts": texts}


# --- bundle manifest ----------------------------------------------------------------------

def load_manifest(bundle: Path) -> dict:
    path = bundle / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReviewError([f"{path}: bundle manifest not found; run human-prepare"]) from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError([f"{path}: unreadable manifest ({exc})"]) from None
    if data.get("format") != FORMAT:
        raise ReviewError([f"{path}: format {data.get('format')!r} is not {FORMAT}"])
    return data


def _snapshot_bytes(bundle: Path) -> bytes:
    path = bundle / SNAPSHOT_FILE
    if not path.is_file():
        raise ReviewError([f"{path}: M7a snapshot missing from the bundle"])
    return path.read_bytes()


# --- records ------------------------------------------------------------------------------

def records_path(records: Path, kind: str) -> Path:
    return records / f"{kind[:-1]}-reviews.json"


def load_records(records: Path, kind: str) -> list[dict]:
    path = records_path(records, kind)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError([f"{path}: unreadable records ({exc})"]) from None
    if data.get("format") != RECORDS_FORMAT or data.get("kind") != kind or not isinstance(data.get("records"), list):
        raise ReviewError([f"{path}: not a {kind} record file of format {RECORDS_FORMAT}"])
    return data["records"]


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _clean(value: str) -> str:
    return value.strip()


def _person(row: dict, line: int, errors: list[str], today: date) -> tuple[str, str]:
    reviewer, when = _clean(row["reviewer"]), _clean(row["review_date"])
    if reviewer and (len(reviewer) > 200 or _CONTROL.search(reviewer)):
        errors.append(f"line {line}: reviewer must be a short single-line name")
    if when:
        try:
            parsed = date.fromisoformat(when)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
                raise ValueError(when)
        except ValueError:
            errors.append(f"line {line}: review_date {when!r} is not YYYY-MM-DD")
        else:
            if parsed > today:
                errors.append(f"line {line}: review_date {when} is in the future")
    return reviewer, when


def _merge(existing: list[dict], new: list[dict], key, same, replace: bool,
           errors: list[str]) -> tuple[list[dict], dict]:
    """Same reviewer + same binding: identical is a no-op, a different judgement needs ``replace``."""
    merged = {key(record): record for record in existing}
    counts = Counter()
    for record in new:
        old = merged.get(key(record))
        if old is None:
            counts["added"] += 1
        elif same(old, record):
            counts["unchanged"] += 1
            continue
        elif replace:
            counts["replaced"] += 1
        else:
            errors.append(f"{record.get('question_id') or record.get('row_key')}: reviewer {record['reviewer']!r} "
                          "already recorded a different judgement for this exact content; "
                          "re-run with --replace to supersede it")
            continue
        merged[key(record)] = record
    return sorted(merged.values(), key=key), dict(counts)


def _records_file(kind: str, records: list[dict]) -> bytes:
    return dump_json({"format": RECORDS_FORMAT, "kind": kind, "attribution": ATTRIBUTION, "records": records})


def _sheet_rows(path: Path, columns: list[str], display: list[str], expected: dict[str, dict],
                key_column: str, errors: list[str]) -> list[tuple[int, dict]]:
    """Every bundle row exactly once, display cells unchanged; anything else is an error."""
    rows, read_errors = read_sheet(path, columns)
    errors += read_errors
    seen: dict[str, int] = {}
    kept = []
    for line, row in rows:
        key = row[key_column].strip()
        if key not in expected:
            errors.append(f"line {line}: unknown {key_column} {key!r} (not in this bundle)")
            continue
        if key in seen:
            errors.append(f"line {line}: duplicate {key_column} {key} (first on line {seen[key]})")
            continue
        seen[key] = line
        edited = [column for column in display if row[column] != expected[key][column]]
        if edited:
            errors.append(f"line {line}: {key}: {', '.join(edited)} differ from the bundle (edited or stale sheet)")
            continue
        kept.append((line, row))
    if not read_errors:
        errors += [f"{key_column} {key}: row missing from the sheet" for key in expected if key not in seen]
    return kept


def import_labels(sheet: Path, *, bundle: Path = DEFAULT_BUNDLE, records: Path = DEFAULT_RECORDS,
                  root: Path = DEFAULT_ROOT, today: date | None = None, now: datetime | None = None,
                  dry_run: bool = False, replace: bool = False) -> dict:
    today = today or date.today()
    errors: list[str] = []
    manifest = load_manifest(bundle)
    try:
        current, qset = m4c_binding(root)
    except (OSError, ValueError) as exc:
        raise ReviewError([f"M4c dataset unreadable: {exc}"]) from None
    errors += [f"corpus: {e}" for e in current["corpus_errors"]]
    shown = manifest["m4c"]
    for field in ("dataset_id", "corpus_sha256", "parsers"):
        if shown[field] != current[field]:
            errors.append(f"stale bundle: M4c {field} changed since the bundle was prepared; run human-prepare")
    for qid in sorted(set(shown["labels"]) | set(current["labels"])):
        if shown["labels"].get(qid) != current["labels"].get(qid):
            errors.append(f"stale bundle: {qid} label changed, added or removed since the bundle was prepared")
    expected = {row["question_id"]: row for row in label_rows(qset)}
    rows = _sheet_rows(sheet, LABEL_COLUMNS, LABEL_DISPLAY, expected, "question_id", errors)
    new, pending = [], []
    imported_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    sheet_sha = sha256_bytes(sheet.read_bytes()) if sheet.is_file() else None
    for line, row in rows:
        qid = row["question_id"]
        verdict, notes = _clean(row["verdict"]), _clean(row["notes"])
        reviewer, when = _person(row, line, errors, today)
        if not (verdict or reviewer or when or notes):
            pending.append(qid)
            continue
        if not (verdict and reviewer and when):
            errors.append(f"line {line}: {qid}: incomplete review; verdict, reviewer and review_date are all required")
            continue
        if verdict not in VERDICTS:
            errors.append(f"line {line}: {qid}: verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
            continue
        if verdict != "accepted" and not notes:
            errors.append(f"line {line}: {qid}: {verdict} needs notes saying what to change or why")
            continue
        new.append({"question_id": qid, "split": row["split"], "group": row["group"],
                    "label_sha256": current["labels"][qid], "corpus_sha256": current["corpus_sha256"],
                    "parsers": current["parsers"], "verdict": verdict, "reviewer": reviewer, "review_date": when,
                    "notes": notes, "sheet_sha256": sheet_sha, "imported_at": imported_at,
                    "attribution": "self_reported"})
    existing = [] if errors else load_records(records, "labels")
    merged, counts = _merge(existing, new,
                            lambda r: (r["question_id"], r["label_sha256"], r["corpus_sha256"],
                                       digest(r["parsers"]), r["reviewer"].casefold()),
                            lambda a, b: all(a[k] == b[k] for k in ("verdict", "review_date", "notes")),
                            replace, errors)
    written = False
    if not errors and not dry_run and counts.get("added", 0) + counts.get("replaced", 0):
        _write_atomic(records_path(records, "labels"), _records_file("labels", merged))
        written = True
    return {"kind": "labels", "errors": errors, "reviewed": [r["question_id"] for r in new], "pending": pending,
            "counts": counts if not errors else {}, "written": written, "dry_run": dry_run}


def _answer_judgements(row: dict, line: int, errors: list[str]) -> bool:
    """A filled row needs every judgement its kind requires; values stay in review.HUMAN_VALUES."""
    key = row["row_key"]
    for column, allowed in review.HUMAN_VALUES.items():
        if _clean(row[column]) not in allowed:
            errors.append(f"line {line}: {key}: {column}={row[column]!r} is not one of "
                          f"{', '.join(sorted(v for v in allowed if v))}")
            return False
    supported, correct, abstention = (_clean(row[c]) for c in ANSWER_HUMAN[:3])
    missing = []
    if row["claim_no"] and not supported:
        missing.append("human_claim_supported")
    if row["citation_no"] and not correct:
        missing.append("human_citation_correct")
    if row["expected_status"] != "answered" and abstention not in ("yes", "no"):
        missing.append("human_abstention_appropriate (yes/no)")
    if row["expected_status"] == "answered" and abstention not in ("", "n/a"):
        errors.append(f"line {line}: {key}: human_abstention_appropriate applies only to questions "
                      "expected not to be answered; use n/a or leave it empty")
        return False
    if not row["claim_no"] and (supported or correct):
        errors.append(f"line {line}: {key}: a response without claims has no claim/citation to grade")
        return False
    if missing:
        errors.append(f"line {line}: {key}: incomplete review; missing {', '.join(missing)}")
        return False
    return True


def _answer_conflicts(rows: list[tuple[int, dict]], errors: list[str]) -> None:
    """Judgements inside one sheet must agree per claim and per response."""
    claims: dict[tuple, list[dict]] = {}
    responses: dict[str, set] = {}
    for _, row in rows:
        if row["claim_no"]:
            claims.setdefault((row["response_id"], row["claim_no"]), []).append(row)
        if _clean(row["human_abstention_appropriate"]) in ("yes", "no"):
            responses.setdefault(row["response_id"], set()).add(_clean(row["human_abstention_appropriate"]))
    for (response_id, claim_no), group in sorted(claims.items()):
        verdicts = {_clean(r["human_claim_supported"]) for r in group}
        if len(verdicts) > 1:
            errors.append(f"{response_id} claim {claim_no}: conflicting human_claim_supported {sorted(verdicts)}")
        cited = [_clean(r["human_citation_correct"]) for r in group]
        if verdicts == {"yes"} and cited and set(cited) == {"no"}:
            errors.append(f"{response_id} claim {claim_no}: marked supported while every citation is marked "
                          "incorrect")
    errors += [f"{response_id}: conflicting human_abstention_appropriate {sorted(values)}"
               for response_id, values in sorted(responses.items()) if len(values) > 1]


def _bundle_m7a(bundle: Path, manifest: dict, corpus: Path) -> tuple[dict, list[dict], list[str]]:
    errors = []
    snapshot_bytes = _snapshot_bytes(bundle)
    current, responses, _ = m7a_binding(snapshot_bytes, corpus)
    shown = manifest["m7a"]
    if shown["snapshot_sha256"] != current["snapshot_sha256"]:
        errors.append("stale bundle: the M7a snapshot changed since the bundle was prepared; run human-prepare")
    if digest(shown["responses"]) != digest(current["responses"]) or shown["parsers"] != current["parsers"] \
            or shown["corpus_sha256"] != current["corpus_sha256"]:
        errors.append("stale bundle: M7a responses/rows, corpus or parser differ from the bundle; run human-prepare")
    return current, responses, errors


def import_answers(sheet: Path, *, bundle: Path = DEFAULT_BUNDLE, records: Path = DEFAULT_RECORDS,
                   corpus: Path = M7A_CORPUS, today: date | None = None, now: datetime | None = None,
                   dry_run: bool = False, replace: bool = False) -> dict:
    today = today or date.today()
    manifest = load_manifest(bundle)
    current, responses, errors = _bundle_m7a(bundle, manifest, corpus)
    if not current["available"]:
        errors.append("M7a answers were not available when the bundle was prepared; nothing to import")
    expected = {row["row_key"]: row for r in responses for row in r["rows"]}
    rows = _sheet_rows(sheet, ANSWER_COLUMNS, ANSWER_DISPLAY, expected, "row_key", errors)
    hashes = {key: value for r in current["responses"] for key, value in r["rows"].items()}
    imported_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    sheet_sha = sha256_bytes(sheet.read_bytes()) if sheet.is_file() else None
    new, pending, graded = [], [], []
    for line, row in rows:
        values = {column: _clean(row[column]) for column in ANSWER_HUMAN}
        if not any(values.values()):
            pending.append(row["row_key"])
            continue
        reviewer, when = _person(row, line, errors, today)
        if not (reviewer and when):
            errors.append(f"line {line}: {row['row_key']}: judgements need reviewer and review_date")
            continue
        if not _answer_judgements(row, line, errors):
            continue
        graded.append((line, row))
        new.append({"row_key": row["row_key"], "response_id": row["response_id"], "run_id": row["run_id"],
                    "row_sha256": hashes[row["row_key"]], "snapshot_sha256": current["snapshot_sha256"],
                    **{column: values[column] for column in ANSWER_HUMAN[:3]},
                    "reviewer": reviewer, "review_date": when, "notes": values["notes"],
                    "sheet_sha256": sheet_sha, "imported_at": imported_at, "attribution": "self_reported"})
    _answer_conflicts(graded, errors)
    existing = [] if errors else load_records(records, "answers")
    merged, counts = _merge(existing, new,
                            lambda r: (r["row_key"], r["row_sha256"], r["snapshot_sha256"], r["reviewer"].casefold()),
                            lambda a, b: all(a[k] == b[k] for k in ANSWER_HUMAN[:3] + ["review_date", "notes"]),
                            replace, errors)
    written = False
    if not errors and not dry_run and counts.get("added", 0) + counts.get("replaced", 0):
        _write_atomic(records_path(records, "answers"), _records_file("answers", merged))
        written = True
    return {"kind": "answers", "errors": errors, "reviewed": [r["row_key"] for r in new], "pending": pending,
            "counts": counts if not errors else {}, "written": written, "dry_run": dry_run}


# --- report -------------------------------------------------------------------------------

def _label_section(manifest: dict, records: list[dict], root: Path) -> dict:
    current, qset = m4c_binding(root)
    by_question: dict[str, dict] = {}
    stale_records = 0
    for question in qset.questions:
        mine = [r for r in records if r["question_id"] == question.id]
        live = [r for r in mine if r["label_sha256"] == current["labels"][question.id]
                and r["corpus_sha256"] == current["corpus_sha256"] and r["parsers"] == current["parsers"]]
        stale_records += len(mine) - len(live)
        verdicts = {r["verdict"] for r in live}
        state = ("unreviewed" if not verdicts else "conflicting" if len(verdicts) > 1 else verdicts.pop())
        by_question[question.id] = {"split": question.split, "group": question.group, "state": state,
                                    "reviewers": sorted({r["reviewer"] for r in live}),
                                    "stale_records": len(mine) - len(live)}
    states = ("accepted", "changes_requested", "rejected", "conflicting", "unreviewed")
    totals = Counter(entry["state"] for entry in by_question.values())
    by_split = {split: dict(Counter(e["state"] for e in by_question.values() if e["split"] == split))
                for split in ("dev", "holdout")}
    by_group = {group: dict(Counter(e["state"] for e in by_question.values() if e["group"] == group))
                for group in GROUP_MINIMUMS}
    total = len(by_question)
    reviewed = total - totals["unreviewed"]
    status = ("awaiting_human_review" if reviewed == 0 else
              "accepted_all" if totals["accepted"] == total else "provisional")
    unknown = sorted({r["question_id"] for r in records} - set(by_question))
    return {"status": status, "questions": total,
            "holdout": sum(e["split"] == "holdout" for e in by_question.values()),
            "counts": {state: totals[state] for state in states}, "by_split": by_split, "by_group": by_group,
            "stale_records": stale_records, "records_for_unknown_questions": unknown,
            "bundle_current": manifest["m4c"]["labels"] == current["labels"]
                              and manifest["m4c"]["corpus_sha256"] == current["corpus_sha256"]
                              and manifest["m4c"]["parsers"] == current["parsers"],
            "corpus_errors": current["corpus_errors"],
            "reviewers": sorted({r for e in by_question.values() for r in e["reviewers"]}),
            "not_accepted": {qid: e["state"] for qid, e in sorted(by_question.items()) if e["state"] != "accepted"
                             and e["state"] != "unreviewed"},
            "effect": "kết quả retrieval M4c vẫn provisional cho tới khi mọi nhãn hiện hành được accepted"
                      if status != "accepted_all" else "mọi nhãn hiện hành có verdict human accepted"}


def _score_rows(responses: list[dict], records: list[dict], snapshot_sha: str) -> tuple[list[dict], list[str], int]:
    """Sheet rows with recorded judgements; rows where reviewers disagree mark their response conflicting."""
    rows, conflicting, live_count = [], [], 0
    for response in responses:
        if not response["scoreable"]:
            continue
        filled, clash = [], False
        for row in response["rows"]:
            live = [r for r in records if r["row_key"] == row["row_key"] and r["row_sha256"] == row_hash(row)
                    and r["snapshot_sha256"] == snapshot_sha]
            live_count += len(live)
            judgements = {tuple(r[c] for c in ANSWER_HUMAN[:3]) for r in live}
            if len(judgements) > 1:
                clash = True
            chosen = live[0] if live else None
            filled.append(row | ({c: chosen[c] for c in ANSWER_HUMAN[:3]}
                                 | {"reviewer": " + ".join(sorted({r["reviewer"] for r in live})),
                                    "review_date": max(r["review_date"] for r in live), "notes": chosen["notes"]}
                                 if chosen else {}))
        if clash:
            conflicting.append(response["response_id"])
        else:
            rows += filled
    return rows, conflicting, live_count


def _answer_metrics(rows: list[dict], cross: list[str], expected_non_answer: set[str]) -> dict:
    metrics = review.score(rows)
    metrics["answered_responses"]["conflicting"] += sum(r not in expected_non_answer for r in cross)
    metrics["answered_responses"]["conflicting_ids"] += [r for r in cross if r not in expected_non_answer]
    metrics["abstention_responses"]["conflicting"] += sum(r in expected_non_answer for r in cross)
    metrics["abstention_responses"]["conflicting_ids"] += [r for r in cross if r in expected_non_answer]
    return metrics


def _answer_section(bundle: Path, manifest: dict, records: list[dict], corpus: Path) -> dict:
    shown = manifest["m7a"]
    if not shown["available"]:
        return {"status": NOT_AVAILABLE, "reason": "M7a answers artifact was missing when the bundle was prepared",
                "run_id": shown["run_id"], "responses": []}
    current, responses, stale = _bundle_m7a(bundle, manifest, corpus)
    rows, cross, live = _score_rows(responses, records, current["snapshot_sha256"])
    non_answer = {r["response_id"] for r in responses if r["drill"] and r["drill"]["expected_status"] != "answered"}
    groups = sorted({row["group"] for r in responses for row in r["rows"]})
    by_group = {}
    for group in groups:
        ids = {r["response_id"] for r in responses if r["rows"] and r["rows"][0]["group"] == group}
        by_group[group] = _answer_metrics([row for row in rows if row["response_id"] in ids],
                                          [c for c in cross if c in ids], non_answer)
    machine = Counter((column, row[column]) for r in responses for row in r["rows"] if row["citation_no"]
                      for column in ("machine_id_in_evidence", "machine_quote_verbatim",
                                     "machine_locator_covers_gold", "machine_file_binding"))
    scoreable = [r for r in responses if r["scoreable"]]
    metrics = _answer_metrics(rows, cross, non_answer)
    complete = (metrics["answered_responses"]["complete"] + metrics["abstention_responses"]["graded"]
                == len(scoreable))
    status = "awaiting_human_review" if live == 0 else "reviewed" if complete and not cross else "provisional"
    return {"status": status, "run_id": current["run_id"], "bundle_current": not stale, "stale": stale,
            "responses": len(responses), "scoreable_responses": len(scoreable),
            "rows": sum(len(r["rows"]) for r in scoreable), "live_records": live,
            "stale_records": len(records) - live,
            "not_scoreable": [{"response_id": r["response_id"], "reason": r["not_scoreable_reason"]}
                              for r in responses if not r["scoreable"]],
            "metrics": metrics, "by_group": by_group,
            "machine_checks": {f"{column}={value}": n for (column, value), n in sorted(machine.items())},
            "corpus_matches_run": current["corpus_matches_run"],
            "provenance": [{"response_id": r["response_id"], "label": r["label"], "question": r["question"],
                            "observed_status": r["observed_status"], "call": r["call"]} for r in responses],
            "reviewers": sorted({r["reviewer"] for r in records})}


def build_report(*, bundle: Path = DEFAULT_BUNDLE, records: Path = DEFAULT_RECORDS, root: Path = DEFAULT_ROOT,
                 corpus: Path = M7A_CORPUS, today: date | None = None) -> dict:
    manifest = load_manifest(bundle)
    labels = _label_section(manifest, load_records(records, "labels"), root)
    answers = _answer_section(bundle, manifest, load_records(records, "answers"), corpus)
    states = {labels["status"], answers["status"]} - {NOT_AVAILABLE}
    overall = ("awaiting_human_review" if states <= {"awaiting_human_review"} else
               "reviewed" if states <= {"accepted_all", "reviewed"} else "provisional")
    return {"format": FORMAT + "#report", "generated_on": (today or date.today()).isoformat(), "status": overall,
            "attribution": ATTRIBUTION,
            "m4c_labels": labels,
            "m4c_answers": {"status": "not_measured",
                            "reason": "các lượt M4c chỉ capture payload generation và trả claims rỗng; "
                                      "80 câu chưa có câu trả lời generation nào để chấm"},
            "m7a_answers": answers,
            "limits": ["M4c: 15 paper tổng hợp, 87 chunk, nhãn do agent soạn; không đại diện paper thật.",
                       "M7a: corpus tổng hợp riêng 2 tài liệu, vài answer live của một run (keyword, user A); "
                       "không có gold label và không lưu evidence pack.",
                       "Các số ở đây không là chứng nhận production, quality SLO hay CI gate.",
                       "Tên reviewer do người điền phiếu tự ghi, không phải danh tính đã xác thực."]}


def _rate(rate: dict) -> str:
    value = "not measured" if rate["value"] is None else f"{rate['value']:.4f}"
    return f"{rate['numerator']}/{rate['denominator']} ({value})"


def render_report(data: dict) -> str:
    import html

    def esc(value) -> str:
        return html.escape(str(value), quote=False).replace("|", "\\|").replace("\n", " ")

    labels, answers = data["m4c_labels"], data["m7a_answers"]
    lines = [f"# Báo cáo duyệt chất lượng M7b — {data['generated_on']}", "",
             f"Trạng thái chung: **{data['status']}**. Attribution: {esc(data['attribution'])}.", "",
             "## Nhãn M4c (80 câu, tách khỏi answer M7a)", "",
             f"Trạng thái: **{labels['status']}** — {esc(labels['effect'])}.", "",
             "| Trạng thái | Số câu |", "| --- | ---: |"]
    lines += [f"| {state} | {n} |" for state, n in labels["counts"].items()]
    lines += ["", f"Tổng {labels['questions']} câu, holdout {labels['holdout']}; record cũ (stale) "
                  f"{labels['stale_records']}; bundle khớp nguồn hiện tại: {labels['bundle_current']}.", "",
              "| Split | " + " | ".join(labels["counts"]) + " |", "| --- |" + " ---: |" * len(labels["counts"])]
    lines += [f"| {split} | " + " | ".join(str(c.get(s, 0)) for s in labels["counts"]) + " |"
              for split, c in labels["by_split"].items()]
    lines += ["", "| Nhóm | " + " | ".join(labels["counts"]) + " |", "| --- |" + " ---: |" * len(labels["counts"])]
    lines += [f"| {group} | " + " | ".join(str(c.get(s, 0)) for s in labels["counts"]) + " |"
              for group, c in labels["by_group"].items()]
    if labels["not_accepted"]:
        lines += ["", "Chưa accepted (đã có phiếu): " + ", ".join(f"{k} ({v})" for k, v in labels["not_accepted"].items())]
    lines += ["", "## Answer M4c", "", f"**{data['m4c_answers']['status']}** — {esc(data['m4c_answers']['reason'])}.",
              "", "## Answer live M7a (corpus tổng hợp riêng)", ""]
    if answers["status"] == NOT_AVAILABLE:
        lines += [f"**{NOT_AVAILABLE}** — {esc(answers['reason'])}."]
    else:
        m = answers["metrics"]
        lines += [f"Trạng thái: **{answers['status']}**, run `{esc(answers['run_id'])}`, bundle khớp: "
                  f"{answers['bundle_current']}. Response: {answers['responses']} (chấm được "
                  f"{answers['scoreable_responses']}), dòng phiếu {answers['rows']}, record hiện hành "
                  f"{answers['live_records']}, record cũ {answers['stale_records']}.", "",
                  "| Chỉ số | Tử/mẫu (giá trị) |", "| --- | --- |",
                  f"| Grounded answer rate | {_rate(m['grounded_answer_rate'])} |",
                  f"| Citation accuracy | {_rate(m['citation_accuracy'])} |",
                  f"| Abstention appropriateness | {_rate(m['abstention_appropriateness'])} |", "",
                  "Response answered: " + ", ".join(f"{k} {v}" for k, v in m["answered_responses"].items()
                                                    if not k.endswith("_ids")) + "; abstention: "
                  + ", ".join(f"{k} {v}" for k, v in m["abstention_responses"].items() if not k.endswith("_ids"))
                  + ".", ""]
        lines += ["| Nhóm | Grounded | Citation | Abstention |", "| --- | --- | --- | --- |"]
        lines += [f"| {g} | {_rate(x['grounded_answer_rate'])} | {_rate(x['citation_accuracy'])} | "
                  f"{_rate(x['abstention_appropriateness'])} |" for g, x in answers["by_group"].items()]
        lines += ["", "Không chấm: " + (", ".join(f"`{esc(n['response_id'])}` ({n['reason']})"
                                               for n in answers["not_scoreable"]) or "không có") + ".", "",
                  "Kiểm máy (cấu trúc, không phải phán đoán human): "
                  + ", ".join(f"{esc(k)}: {v}" for k, v in answers["machine_checks"].items()) + ".", "",
                  "| Response | Status | Model / endpoint / provider | Generation ID |", "| --- | --- | --- | --- |"]
        for p in answers["provenance"]:
            call = p["call"] or {}
            selected = ", ".join(f"{s.get('model')}@{s.get('provider')}" for s in call.get("selected") or [])
            lines.append(f"| `{esc(p['response_id'])}` | {esc(p['observed_status'] or 'screen text')} | "
                         f"{esc(call.get('model') or 'not recorded')} / {esc(selected or 'not recorded')} / "
                         f"{esc(call.get('provider') or 'not recorded')} | {esc(call.get('generation_id') or 'not recorded')} |")
    lines += ["", "## Giới hạn", ""] + [f"- {esc(limit)}" for limit in data["limits"]]
    return "\n".join(lines) + "\n"


def write_report(data: dict, output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    json_path, md_path = output / "report.json", output / "report.md"
    _write_atomic(json_path, dump_json(data))
    _write_atomic(md_path, render_report(data).encode("utf-8"))
    return json_path, md_path
