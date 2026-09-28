"""Evaluation report: JSON data plus a Markdown summary with explicit denominators."""
from __future__ import annotations

import platform
import sys
from collections import Counter
from datetime import datetime
from importlib.metadata import version

from app.evaluation import local_embedding
from app.evaluation.locator import covers
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


def _stage_tables(rows: list[Row], keys: list[str]) -> list[dict]:
    search_rows = [row for row in rows if not row.stage.startswith("rag_pack")]
    pack_rows = [row for row in rows if row.stage.startswith("rag_pack")]
    table = aggregate(search_rows, ["stage", *keys], K)
    for entry in aggregate(pack_rows, ["stage", *keys], PACK_K):
        entry["k"] = "pack"
        table.append(entry)
    for entry in table:
        entry.setdefault("k", K)
    return table


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


def build(*, manifest, qset, records, rows, index, seeded, viewer, capture, settings, configured_pricing,
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
        "index_check": _index_check(answered, index),
        "retrieval": {"k": K, "overall": _stage_tables(rows, []), "by_group": _stage_tables(rows, ["group"]),
                      "by_scope": _stage_tables(rows, ["scope"]), "by_split": _stage_tables(rows, ["split"])},
        "comparison": {"questions": sum(q.group == "comparison" for q in qset.questions),
                       "detected_by_rag_keyword_rule": sum(comparison_detected.values()),
                       "not_detected": sorted(k for k, v in comparison_detected.items() if not v)},
        "rag_pipeline": _rag_outcomes(qset, records),
        "acl": {"records_audited": len(records), "violations": acl, "viewer": viewer,
                "gold_label_in_payload": sum(bool(r.get("gold_label_in_payload")) for r in records),
                "payload_keys": sorted({k for r in records for k in r.get("payload_request_keys", [])})},
        "not_measured": NOT_MEASURED,
        "questions": per_question,
        "records": records,
    }


def _index_check(answered, index) -> dict:
    """Gold sides that no chunk of the item's active index covers (extraction/chunking losses)."""
    missing = []
    total = 0
    for q in answered:
        for side in q.evidence:
            total += 1
            if not any(covers(span, hit) for span in side.spans for hit in index.get(span.item, [])):
                missing.append(f"{q.id}:{side.label}")
    return {"gold_sides": total, "indexed_sides": total - len(missing), "not_indexed": missing}


STAGES = {
    "search:keyword": "`POST /search` mode `keyword` (PostgreSQL FTS `plainto_tsquery('simple')`)",
    "search:hybrid": "`POST /search` mode `hybrid`: keyword + vector băm cục bộ, fusion RRF, không rerank",
    "vector": "vector-only qua `app.search._rank` (cùng gate SQL), vector băm cục bộ",
    "rag_pack:keyword": "evidence pack mà `POST /rag/ask` mode `keyword` thực sự dựng để gửi generation",
    "rag_pack:hybrid": "evidence pack mà `POST /rag/ask` mode `hybrid` thực sự dựng để gửi generation",
}
REPRODUCE = [
    "# PowerShell từ gốc repo; .env theo docs/dev-setup.md",
    "docker compose up -d --wait db minio",
    '$py = "$env:USERPROFILE\\.conda\\envs\\ras\\python.exe"',
    "& $py -m app.evaluation validate",
    "& $py -m app.evaluation run --output evaluation\\rag\\reports",
]


def _table(entries: list[dict], key: str | None) -> list[str]:
    columns = ["Stage", *([key] if key else []), "k", "Câu", "Recall@k (TB theo câu)", "Side trúng / tổng",
               "Đủ mọi side / câu nhiều side", "Kết quả"]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for e in entries:
        outcomes = ", ".join(f"{name}={count}" for name, count in e["outcomes"].items())
        cells = [f"`{e['stage']}`", *([str(e[key])] if key else []), str(e["k"]), str(e["questions"]),
                 f"{e['recall_at_k']:.3f}", f"{e['sides_hit']}/{e['sides_total']}",
                 f"{e['all_sides_hit']}/{e['multi_side_questions']}", outcomes]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


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
        "Các ngưỡng trong docs là **mục tiêu đề xuất, cần đo**, không phải CI gate.",
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
    out += ["", f"Tổng {data['corpus']['indexed_chunks']} chunk đã index. Side chứng cứ được chunk active phủ: "
            f"**{check['indexed_sides']}/{check['gold_sides']}**{missing}.",
            "", "## Recall@5", "",
            "Mẫu số là các câu `answered`. Một side trúng khi chunk ở hạng ≤ k chứa ít nhất 50% ký tự của một span chuẩn "
            "của side đó. Recall@k của câu = side trúng / số side; bảng báo trung bình theo câu và tổng side. "
            "`rag_pack` tính trên toàn bộ pack (k = pack). Kết quả: `ok`; `extraction` (span không có trong index); "
            "`retrieval` (không có trong danh sách trả về); `rank` (có nhưng hạng > k); `evidence_pack` (search nội bộ "
            "của RAG trả về nhưng pack giới hạn chunk/paper/byte không chọn); `acl`.", ""]
    out += [f"- `{stage}`: {text}" for stage, text in STAGES.items()]
    out += ["", "### Tổng", "", *_table(data["retrieval"]["overall"], None),
            "", "### Theo nhóm", "", *_table(data["retrieval"]["by_group"], "group"),
            "", "### Theo scope", "", *_table(data["retrieval"]["by_scope"], "scope"),
            "", "### Dev / holdout", "", *_table(data["retrieval"]["by_split"], "split")]
    comparison = data["comparison"]
    undetected = f"; không nhận ra: {', '.join(comparison['not_detected'])}" if comparison["not_detected"] else ""
    out += ["", "## So sánh nhiều paper", "",
            "Ở nhóm `comparison`, cột “Đủ mọi side” là số câu tìm được chứng cứ của **tất cả** các phía trong top-k. "
            f"Luật từ khóa so sánh của RAG (`app.rag.COMPARISON`) nhận ra {comparison['detected_by_rag_keyword_rule']}/"
            f"{comparison['questions']} câu so sánh{undetected}.",
            "", "## Pipeline RAG trước generation", "",
            "Generation không chạy, nên bảng chỉ cho biết pipeline gửi evidence đi hay tự trả `insufficient_evidence` "
            "trước khi gọi model; đây **không** là tỷ lệ abstention đúng.", "",
            "| Mode | Kỳ vọng | Kết quả pipeline | Số lượt |", "| --- | --- | --- | --- |"]
    out += [f"| {r['mode']} | {r['expected']} | {r['outcome']} | {r['count']} |" for r in data["rag_pipeline"]]
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
            "Ô retrieval: hạng từng side (`×` = không có) và kết quả. Ô RAG: HTTP, trạng thái/lý do, `→ gen` nếu evidence "
            "được gửi tới generation, và số chunk trong pack.", "",
            "| ID | Nhóm | Split | Danh tính | Scope | Kỳ vọng | search:keyword | search:hybrid | vector | rag:keyword | rag:hybrid |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for q in data["questions"]:
        s = q["stages"]
        out.append(f"| {q['id']} | {q['group']} | {q['split']} | {q['identity']} | {q['scope']} | {q['expected']} | "
                   f"{_cell(s.get('search:keyword'))} | {_cell(s.get('search:hybrid'))} | {_cell(s.get('vector'))} | "
                   f"{_rag_cell(q.get('rag:keyword'))} | {_rag_cell(q.get('rag:hybrid'))} |")
    return "\n".join(out) + "\n"
