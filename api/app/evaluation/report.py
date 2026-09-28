"""Evaluation report: JSON data plus a Markdown summary with explicit denominators."""
from __future__ import annotations

import platform
import sys
from collections import Counter
from datetime import datetime
from importlib.metadata import version

from app.evaluation import local_embedding
from app.evaluation.locator import DIAGNOSTIC_OVERLAP, FULL_SPAN, covers
from app.evaluation.metrics import K, Row, aggregate
from app.ingest import markdown, pdf

PACK_K = 10**6  # the whole evidence pack counts; it is already bounded by config
NOT_MEASURED = {
    "rerank": "Rerank needs an OpenRouter call; pricing_verified=false and outbound HTTP is blocked.",
    "semantic_vector_quality": "Vectors come from a local hashed bag of words, not openai/text-embedding-3-small.",
    "grounded_answer_rate": "No answer was generated; the capture returns no claims. Needs generation plus human review.",
    "citation_accuracy": "No generated citations exist. Needs generation plus human review.",
    "abstention_appropriateness": "Final abstention depends on generation for every question whose evidence reached the pack.",
    "latency_p95": "Single in-process run on a dev machine with fakes; not the proposed 5-user workload.",
    "cost_and_charge": "No provider call was made; ledger rows hold 0-token local estimates only.",
}


def _block(rows: list[Row], k: int, keys=("group", "scope", "split")) -> dict:
    block = {"overall": aggregate(rows, ["stage"], k)}
    for key in keys:
        block[f"by_{key}"] = aggregate(rows, ["stage", key], k)
    return block


def _metrics(rows: list[Row], keys=("group", "scope", "split")) -> dict:
    """Recall@5 for ranked search/vector lists; whole-pack coverage for RAG packs (not Recall@5)."""
    ranked = [row for row in rows if not row.stage.startswith("rag_pack")]
    packs = [row for row in rows if row.stage.startswith("rag_pack")]
    return {"recall_at_5": _block(ranked, K, keys), "evidence_pack_coverage": _block(packs, PACK_K, keys)}


def _rag_outcomes(qset, records: list[dict]) -> list[dict]:
    expected = {q.id: q.expected.status for q in qset.questions}
    counts: Counter = Counter()
    for record in records:
        if record["stage"] != "rag_pack":
            continue
        if record["status_code"] == 404:
            outcome = "http_404"
        elif record["status_code"] != 200:
            outcome = f"http_{record['status_code']}"
        elif record["generation_called"]:
            outcome = "evidence_sent_to_generation"
        else:
            outcome = "abstained_before_generation" + (f":{record['rag_reason']}" if record["rag_reason"] else "")
        counts[(record["mode"], expected[record["question_id"]], outcome)] += 1
    return [{"mode": m, "expected": e, "outcome": o, "count": n} for (m, e, o), n in sorted(counts.items())]


def _candidates(qset, records: list[dict]) -> list[dict]:
    """How often questions without an answer still receive candidates (not abstention quality)."""
    groups = {"no_answer_group": lambda q: q.group == "no_answer",
              "expected_insufficient_evidence": lambda q: q.expected.status == "insufficient_evidence",
              "answered": lambda q: q.expected.status == "answered"}
    table = []
    for mode in sorted({r["mode"] for r in records if r["stage"] == "search"}):
        for split in ("dev", "holdout", "all"):
            for name, keep in groups.items():
                chosen = {q.id for q in qset.questions if keep(q) and split in (q.split, "all")}
                search = [r for r in records if r["stage"] == "search" and r["mode"] == mode
                          and r["question_id"] in chosen]
                rag = [r for r in records if r["stage"] == "rag_pack" and r["mode"] == mode
                       and r["question_id"] in chosen]
                if not search:
                    continue
                table.append({"mode": mode, "split": split, "questions": name, "count": len(search),
                              "search_with_candidates": sum(bool(r["hits"]) for r in search),
                              "mean_search_hits": round(sum(len(r["hits"]) for r in search) / len(search), 2),
                              "keyword_no_terms": sum((r.get("keyword_query") or {}).get("status") == "no_terms"
                                                      for r in search),
                              "rag_evidence_sent": sum(bool(r["generation_called"]) for r in rag)})
    return table


def build(*, manifest, qset, records, rows, diagnostic_rows, index, seeded, viewer, capture, settings, configured_pricing,
          defaults, comparison_detected, started: datetime, git: dict, modes) -> dict:
    answered = [q for q in qset.questions if q.expected.status == "answered"]
    sides = sum(len(q.evidence) for q in answered)
    acl = [{"question_id": r["question_id"], "stage": r["stage"], "mode": r["mode"], "violations": r["acl_violations"]}
           for r in records if r["acl_violations"]]
    per_question = []
    for q in qset.questions:
        entry = {"id": q.id, "group": q.group, "split": q.split, "identity": q.identity, "scope": q.scope.kind,
                 "expected": q.expected.status, "stages": {}}
        for row in rows:
            if row.question_id == q.id:
                entry["stages"][row.stage] = {"ranks": list(row.ranks), "outcome": row.outcome}
        for record in records:
            if record["question_id"] == q.id and record["stage"] == "rag_pack":
                entry[f"rag:{record['mode']}"] = {"status_code": record["status_code"],
                                                  "status": record["rag_status"], "reason": record["rag_reason"],
                                                  "generation_called": record["generation_called"],
                                                  "pack_chunks": len(record["hits"])}
        per_question.append(entry)
    return {
        "report_version": 1,
        "generated_at": started.isoformat(timespec="seconds"),
        "command": "python -m app.evaluation run --output evaluation/rag/reports",
        "git": git,
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                        "pypdf": version("pypdf"), "pdf_parser": pdf.PIPELINE_VERSION,
                        "markdown_parser": markdown.PIPELINE_VERSION, "modes": list(modes)},
        "dataset": {"id": qset.dataset_id, "questions": len(qset.questions),
                    "holdout": sum(q.split == "holdout" for q in qset.questions),
                    "groups": dict(Counter(q.group for q in qset.questions)),
                    "label_status": dict(Counter(q.label_status for q in qset.questions)),
                    "answered_questions": len(answered), "gold_sides": sides},
        "corpus": {"id": manifest.corpus_id, "representativeness": manifest.representativeness.model_dump(),
                   "ingest": seeded.ingest,
                   "indexed_chunks": sum(v["indexed_chunks"] for v in seeded.ingest.values()),
                   "coverage_mismatches": sorted(k for k, v in seeded.ingest.items()
                                                 if v["coverage"] != v["expected_coverage"])},
        "config": {"keyword_top_k": settings.search.keyword_top_k, "vector_top_k": settings.search.vector_top_k,
                   "max_results": settings.search.max_results,
                   "max_evidence_chunks": settings.generation.max_evidence_chunks,
                   "max_evidence_papers": settings.generation.max_evidence_papers,
                   "max_evidence_bytes": settings.generation.max_evidence_bytes,
                   "markdown_max_chunk_chars": settings.ingest.markdown_max_chunk_chars},
        "providers": {"external_provider_calls": 0,
                      "blocked_outbound_http_attempts": len(capture.blocked_hosts),
                      "local_embedding_calls": capture.embedding_calls,
                      "embedding_model_used": local_embedding.MODEL_ID,
                      "generation": "not called; the payload that would be sent is captured and no claims are returned",
                      "rerank": "not requested",
                      "pricing_verified_code_defaults": defaults,
                      "pricing_verified_configured": configured_pricing,
                      "note": "The run overrides pricing_verified only on an in-process Settings copy bound to local fakes; the default and .env values above are unchanged."},
        "index_check": _index_check(answered, index) | {
            "diagnostic_overlap_50": _index_check(answered, index, DIAGNOSTIC_OVERLAP)},
        "retrieval": {"coverage_rule": "chunk contains 100% of a gold span (same item, kind, page; valid half-open offsets)",
                      "recall_at_5_k": K, "evidence_pack_max_chunks": settings.generation.max_evidence_chunks,
                      **_metrics(rows),
                      "diagnostic_overlap_50": {
                          "rule": "chunk holds >= 50% of a gold span's characters; diagnostic only, not a headline result",
                          **_metrics(diagnostic_rows, ())}},
        "comparison": _comparison(qset, comparison_detected),
        "rag_pipeline": _rag_outcomes(qset, records),
        "candidates": _candidates(qset, records),
        "acl": {"records_audited": len(records), "violations": acl, "viewer": viewer,
                "gold_label_in_payload": sum(bool(r.get("gold_label_in_payload")) for r in records),
                "payload_keys": sorted({k for r in records for k in r.get("payload_request_keys", [])})},
        "not_measured": NOT_MEASURED,
        "questions": per_question,
        "records": records,
    }


def _comparison(qset, detected: dict[str, bool]) -> dict:
    """``app.rag.is_comparison`` per question; outside the group a hit is a false positive."""
    group = {q.id for q in qset.questions if q.group == "comparison"}
    return {"questions": len(group),
            "detected_by_rag_rule": sum(detected.get(k, False) for k in group),
            "not_detected": sorted(k for k in group if not detected.get(k, False)),
            "other_questions": len(qset.questions) - len(group),
            "detected_outside_group": sorted(k for k, v in detected.items() if v and k not in group)}


def _index_check(answered, index, minimum: float = FULL_SPAN) -> dict:
    """Gold sides that no chunk of the item's active index covers (extraction/chunking losses)."""
    missing = []
    total = 0
    for q in answered:
        for side in q.evidence:
            total += 1
            if not any(covers(span, hit, minimum) for span in side.spans for hit in index.get(span.item, [])):
                missing.append(f"{q.id}:{side.label}")
    return {"gold_sides": total, "indexed_sides": total - len(missing), "not_indexed": missing}


STAGES = {
    "search:keyword": "`POST /search` mode `keyword` (PostgreSQL FTS `simple`, truy vấn từ khóa giới hạn của `app.keyword_query`)",
    "search:hybrid": "`POST /search` mode `hybrid`: keyword + vector băm cục bộ, fusion RRF, không rerank",
    "vector": "vector-only qua `app.search._rank` (cùng gate SQL), vector băm cục bộ",
    "rag_pack:keyword": "toàn evidence pack (tối đa 6 chunk) mà `POST /rag/ask` mode `keyword` dựng để gửi generation",
    "rag_pack:hybrid": "toàn evidence pack (tối đa 6 chunk) mà `POST /rag/ask` mode `hybrid` dựng để gửi generation",
}
REPRODUCE = [
    "# PowerShell từ gốc repo; .env theo docs/dev-setup.md",
    "docker compose up -d --wait db minio",
    '$py = "$env:USERPROFILE\\.conda\\envs\\ras\\python.exe"',
    "& $py -m app.evaluation validate",
    "& $py -m app.evaluation run --output evaluation\\rag\\reports",
]


def _table(entries: list[dict], key: str | None, metric: str) -> list[str]:
    columns = ["Stage", *([key] if key else []), "Câu", f"{metric} (TB theo câu)", "Side trúng / tổng",
               "Đủ mọi side / câu nhiều side", "Kết quả"]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for e in entries:
        outcomes = ", ".join(f"{name}={count}" for name, count in e["outcomes"].items())
        cells = [f"`{e['stage']}`", *([str(e[key])] if key else []), str(e["questions"]),
                 f"{e['mean_per_question']:.3f}", f"{e['sides_hit']}/{e['sides_total']}",
                 f"{e['all_sides_hit']}/{e['multi_side_questions']}", outcomes]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _tables(block: dict, metric: str) -> list[str]:
    out = ["", "Tổng:", "", *_table(block["overall"], None, metric)]
    for key, title in (("group", "Theo nhóm"), ("scope", "Theo scope"), ("split", "Dev / holdout")):
        if f"by_{key}" in block:
            out += ["", f"{title}:", "", *_table(block[f"by_{key}"], key, metric)]
    return out


def _cell(stage: dict | None) -> str:
    if not stage:
        return "—"
    ranks = ",".join("×" if rank is None else str(rank) for rank in stage["ranks"])
    return f"{ranks} ({stage['outcome']})"


def _rag_cell(result: dict | None) -> str:
    if not result:
        return "—"
    status = result["status"] or "error"
    reason = f"/{result['reason']}" if result["reason"] else ""
    sent = " → gen" if result["generation_called"] else ""
    return f"{result['status_code']} {status}{reason}{sent} ({result['pack_chunks']})"


def render_markdown(data: dict) -> str:
    rep = data["corpus"]["representativeness"]
    acl = data["acl"]
    acl_ok = not acl["violations"] and not acl["viewer"]["violations"]
    env, cfg, providers, dataset = data["environment"], data["config"], data["providers"], data["dataset"]
    out = [
        f"# Báo cáo đánh giá retrieval/RAG — lần chạy {data['generated_at'][:10]}",
        "",
        "> Nhãn do coding agent soạn, trạng thái `pending_human_review`; **chưa** được người dùng hay chuyên gia duyệt. "
        f"Corpus tổng hợp {rep['papers']} paper, nhỏ hơn nhiều so với workload đề xuất {rep['target_papers']:,} paper: "
        "kết quả chỉ kiểm pipeline, gate ACL và khớp locator, không đại diện chất lượng trên paper thật. "
        "Các ngưỡng trong docs là **mục tiêu đề xuất, cần đo**, không phải CI gate; vì cả 80 nhãn còn "
        "`pending_human_review`, báo cáo này **chưa chứng minh** đã đạt mục tiêu chất lượng nào.",
        "", "## Tái lập", "", "```powershell", *REPRODUCE, "```", "",
        f"- Commit `{data['git']['commit']}`" + (" (working tree có thay đổi chưa commit)" if data["git"]["dirty"] else ""),
        f"- Python {env['python']}, pypdf {env['pypdf']}, parser `{env['pdf_parser']}` / `{env['markdown_parser']}`; {env['platform']}",
        f"- Dataset `{dataset['id']}`: {dataset['questions']} câu, {dataset['holdout']} holdout, "
        f"{dataset['answered_questions']} câu có đáp án với {dataset['gold_sides']} side chứng cứ; nhóm "
        + ", ".join(f"{name} {count}" for name, count in dataset["groups"].items()) + ".",
        f"- Config: keyword_top_k={cfg['keyword_top_k']}, vector_top_k={cfg['vector_top_k']}, max_results={cfg['max_results']}, "
        f"evidence ≤ {cfg['max_evidence_chunks']} chunk / {cfg['max_evidence_papers']} paper / {cfg['max_evidence_bytes']} byte, "
        f"chunk ≤ {cfg['markdown_max_chunk_chars']} ký tự.",
        "", "## Provider và giá", "",
        f"- Lời gọi provider ngoài: **{providers['external_provider_calls']}**; outbound HTTP bị chặn suốt lần chạy "
        f"(số lần bị chặn: {providers['blocked_outbound_http_attempts']}).",
        f"- Embedding `{providers['embedding_model_used']}` cục bộ ({providers['local_embedding_calls']} lời gọi), không phải "
        "`openai/text-embedding-3-small`. Rerank không yêu cầu. Generation không gọi: payload được ghi lại, response trả không có claim.",
        f"- `pricing_verified` mặc định trong code: `{providers['pricing_verified_code_defaults']}`; giá trị cấu hình lúc chạy: "
        f"`{providers['pricing_verified_configured']}`. Chỉ bản `Settings` trong tiến trình, gắn với fake cục bộ, được bật để pipeline chạy.",
        "", "## Corpus và index", "", rep["statement"], "",
        "| Item | Job | Content | Coverage (kỳ vọng) | Chunk đã index |", "| --- | --- | --- | --- | --- |",
    ]
    for key, v in sorted(data["corpus"]["ingest"].items()):
        out.append(f"| {key} | {v['job_state']} | {v['content_status']} | {v['coverage']} ({v['expected_coverage']}) | {v['indexed_chunks']} |")
    check = data["index_check"]
    missing = f"; chưa phủ: {', '.join(check['not_indexed'])}" if check["not_indexed"] else ""
    retrieval = data["retrieval"]
    pack_max = retrieval["evidence_pack_max_chunks"]
    out += ["", f"Tổng {data['corpus']['indexed_chunks']} chunk đã index. Side chứng cứ có ít nhất một chunk active chứa "
            f"trọn span chuẩn: **{check['indexed_sides']}/{check['gold_sides']}**{missing}.",
            "", "## Cách tính", "",
            "Mẫu số là các câu `answered`. Một side trúng khi chunk chứa **trọn 100%** một span chuẩn của side đó: cùng item, "
            "cùng loại locator, cùng trang với PDF, offset nửa mở hợp lệ và `chunk.start ≤ span.start`, `span.end ≤ chunk.end`. "
            "Giá trị của câu = side trúng / số side; bảng báo trung bình theo câu, tổng side và số câu nhiều side tìm đủ mọi side. "
            "Kết quả: `ok`; `extraction` (không chunk active nào chứa trọn span); `retrieval` (không có trong danh sách trả về); "
            "`rank` (có nhưng hạng > 5); `evidence_pack` (search nội bộ của RAG trả về nhưng pack giới hạn chunk/paper/byte "
            "không chọn); `acl`.", ""]
    out += [f"- `{stage}`: {text}" for stage, text in STAGES.items()]
    out += ["", "## Recall@5 — search và vector", "",
            "Chỉ các danh sách có thứ hạng (`search:*`, `vector`) được báo là Recall@5: side phải nằm trong 5 chunk đầu.",
            *_tables(retrieval["recall_at_5"], "Recall@5"),
            "", f"## Phủ evidence pack — toàn pack tối đa {pack_max} chunk (không phải Recall@5)", "",
            f"`rag_pack:*` đo side có nằm **ở bất kỳ vị trí nào** trong evidence pack mà RAG gửi generation "
            f"(tối đa {pack_max} chunk / {cfg['max_evidence_papers']} paper / {cfg['max_evidence_bytes']} byte), "
            "không giới hạn top 5. Không so trực tiếp với Recall@5 ở trên.",
            *_tables(retrieval["evidence_pack_coverage"], "Phủ pack"),
            "", "## Chẩn đoán: chồng ≥ 50% span (không phải kết quả chính)", "",
            "Cùng dữ liệu, nhưng side được tính khi chunk chứa ít nhất 50% ký tự span. Chỉ để thấy span bị cắt ngang "
            "chunk; không dùng làm kết quả hay so với mục tiêu. Side được index theo luật này: "
            f"{check['diagnostic_overlap_50']['indexed_sides']}/{check['diagnostic_overlap_50']['gold_sides']}.",
            "", "Recall@5 (chẩn đoán ≥ 50%):", "", *_table(retrieval["diagnostic_overlap_50"]["recall_at_5"]["overall"],
                                                          None, "Recall@5 ≥50%"),
            "", f"Phủ pack ≤ {pack_max} chunk (chẩn đoán ≥ 50%):", "",
            *_table(retrieval["diagnostic_overlap_50"]["evidence_pack_coverage"]["overall"], None, "Phủ pack ≥50%")]
    comparison = data["comparison"]
    undetected = f"; không nhận ra: {', '.join(comparison['not_detected'])}" if comparison["not_detected"] else ""
    outside = ", ".join(comparison["detected_outside_group"]) or "không câu nào"
    out += ["", "## So sánh nhiều paper", "",
            "Ở nhóm `comparison`, cột “Đủ mọi side” là số câu có chứng cứ của **tất cả** các phía: trong top 5 với "
            "Recall@5, hoặc ở bất kỳ đâu trong pack với phủ pack. "
            f"Luật nhận diện câu so sánh của RAG (`app.rag.is_comparison`) nhận ra {comparison['detected_by_rag_rule']}/"
            f"{comparison['questions']} câu so sánh{undetected}; trong {comparison['other_questions']} câu thuộc nhóm khác, "
            f"luật coi là so sánh: {outside}.",
            "", "## Pipeline RAG trước generation", "",
            "Generation không chạy, nên bảng chỉ cho biết pipeline gửi evidence đi hay tự trả `insufficient_evidence` "
            "trước khi gọi model; đây **không** là tỷ lệ abstention đúng.", "",
            "| Mode | Kỳ vọng | Kết quả pipeline | Số lượt |", "| --- | --- | --- | --- |"]
    out += [f"| {r['mode']} | {r['expected']} | {r['outcome']} | {r['count']} |" for r in data["rag_pipeline"]]
    out += ["", "## Candidate cho câu không có đáp án", "",
            "Số câu mà search trả ít nhất một chunk, số hit trung bình, số câu không còn thuật ngữ từ khóa hữu ích "
            "(`no_terms`) và số câu RAG gửi evidence tới generation. Có candidate **không** có nghĩa có bằng chứng "
            "trả lời; dòng `answered` chỉ để so sánh.", "",
            "| Mode | Split | Nhóm câu | Câu | Có candidate | TB hit | Từ khóa `no_terms` | RAG gửi evidence |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    out += [f"| {c['mode']} | {c['split']} | {c['questions']} | {c['count']} | {c['search_with_candidates']} | "
            f"{c['mean_search_hits']} | {c['keyword_no_terms']} | {c['rag_evidence_sent']} |"
            for c in data.get("candidates", [])]
    out += ["", "## ACL", "",
            f"- Bản ghi đã kiểm (search, vector, RAG response + payload): {acl['records_audited']}; vi phạm: **{len(acl['violations'])}**.",
            f"- Viewer `/search/chunks/{{id}}`: {acl['viewer']['chunks']} chunk × 3 danh tính = {acl['viewer']['requests']} request; "
            f"vi phạm: **{len(acl['viewer']['violations'])}**.",
            f"- Payload chứa văn bản chỉ có trong nhãn (`rationale`, `label_status`, ID câu hỏi): **{acl['gold_label_in_payload']}**; "
            f"khóa JSON gửi model: {acl['payload_keys']}.",
            "- **0 lỗi ACL trong phạm vi các request đã chạy ở trên**; không phải bằng chứng cho mọi đường truy cập." if acl_ok
            else "- **PHÁT HIỆN LỖI ACL — dừng và sửa trước khi dùng các kết quả khác.**",
            "", "## Chưa đo", ""]
    out += [f"- `{name}`: not measured — {why}" for name, why in data["not_measured"].items()]
    out += ["", "## Theo câu hỏi", "",
            "Ô search/vector: hạng từng side (`×` = không có) và kết quả Recall@5. Ô pack: vị trí side trong pack và kết quả "
            "phủ pack. Ô RAG: HTTP, trạng thái/lý do, `→ gen` nếu evidence được gửi tới generation, và số chunk trong pack.", "",
            "| ID | Nhóm | Split | Danh tính | Scope | Kỳ vọng | search:keyword | search:hybrid | vector | pack:keyword | pack:hybrid | rag:keyword | rag:hybrid |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for q in data["questions"]:
        s = q["stages"]
        out.append(f"| {q['id']} | {q['group']} | {q['split']} | {q['identity']} | {q['scope']} | {q['expected']} | "
                   f"{_cell(s.get('search:keyword'))} | {_cell(s.get('search:hybrid'))} | {_cell(s.get('vector'))} | "
                   f"{_cell(s.get('rag_pack:keyword'))} | {_cell(s.get('rag_pack:hybrid'))} | "
                   f"{_rag_cell(q.get('rag:keyword'))} | {_rag_cell(q.get('rag:hybrid'))} |")
    return "\n".join(out) + "\n"
