"""Offline M7b review bundle: escaped HTML pages, two fill-in sheets and a binding manifest.

Every page is static HTML with a ``default-src 'none'`` policy: no script, no remote
resource, only relative links inside the bundle. All corpus, question, claim and answer
text is HTML-escaped; sheet cells that a spreadsheet could run as a formula are prefixed
(``human.cell_out``). Contexts come from the production parsers, never from offsets the
reader has to look up.
"""
from __future__ import annotations

import html
import json
import os
import shutil
import tempfile
from pathlib import Path

from app.evaluation import human
from app.evaluation.corpus import DEFAULT_ROOT
from app.evaluation.locator import LocatorError, Source, locate
from app.evaluation.schema import Manifest, Question, load
from app.evaluation.validate import load_sources, validate
from app.ingest import markdown

CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
STYLE = """
body{font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:72rem;padding:0 1rem 3rem;color:#1d1d1f;background:#fff}
nav{padding:.6rem 0;border-bottom:1px solid #ccc;margin-bottom:1rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0}td,th{border:1px solid #ccc;padding:.25rem .4rem;vertical-align:top;text-align:left}
th{background:#f3f3f3}pre,.ctx{white-space:pre-wrap;word-break:break-word;background:#f7f7f7;border:1px solid #ddd;padding:.5rem;font:13px/1.45 ui-monospace,Consolas,monospace}
.q{font:17px/1.5 system-ui,sans-serif;white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;padding:.5rem .7rem}
mark{background:#ffe066;padding:0 .1rem}.ne mark{background:#ffc9c9}.note{background:#eef5ff;border-left:4px solid #3a7bd5;padding:.5rem .8rem}
.warn{background:#fff4e5;border-left:4px solid #d58b00;padding:.5rem .8rem}.side{border:1px solid #bbb;padding:.5rem .8rem;margin:.6rem 0}
.ln{color:#888;display:inline-block;min-width:3em;user-select:none}.ref{color:#7a4a00}:target{background:#fff3b0}
@media (prefers-color-scheme:dark){body{background:#151515;color:#eee}th{background:#262626}pre,.ctx,.q{background:#202020;border-color:#444}
mark{color:#000}.note{background:#14243a}.warn{background:#3a2a10}td,th,.side{border-color:#555}}
"""
GUIDE = """# Bộ duyệt chất lượng M7b — hướng dẫn người đọc

Bộ này đọc offline: mở `index.html` bằng trình duyệt, không cần đăng nhập hay mạng.
Có hai phiếu riêng, **không trộn mẫu số**:

1. `label-review.csv` — duyệt 80 nhãn M4c (corpus tổng hợp 15 paper). Một dòng một câu.
2. `answer-review.csv` — chấm câu trả lời live M7a đã lưu (corpus tổng hợp khác, 2 tài liệu).
   Một dòng một citation; response không có claim có một dòng.

Mở phiếu bằng bảng tính hoặc trình soạn thảo; lưu lại dạng **CSV UTF-8**, giữ nguyên tên cột,
số dòng và mọi cột không phải cột điền. Ngày ghi dạng `YYYY-MM-DD` (đặt cột ngày là Text nếu
bảng tính tự đổi định dạng). Ô bắt đầu bằng `'` là cách phiếu chặn bảng tính chạy công thức;
giữ nguyên dấu đó.

## Phiếu nhãn (`label-review.csv`)

Mở trang của câu (cột `open_page`), đọc câu hỏi, kỳ vọng, từng side evidence với quote, vị trí
và đoạn text parser production trích ra; non-evidence và lý do; supported claim, rationale.
Liên kết "bản đọc" mở đúng dòng/trang; "file gốc" mở file trong bundle.

Điền bốn cột:

- `verdict`: `accepted` (câu hỏi, kỳ vọng, evidence/quote/vị trí, non-evidence và claim đều đúng
  và đủ), `changes_requested` (cần sửa, ghi chỗ cần sửa trong `notes`) hoặc `rejected` (không
  dùng được, ghi lý do trong `notes`).
- `reviewer`: tên người duyệt. `review_date`: ngày duyệt. `notes`: bắt buộc khi không accepted.

Cột `agent_note` chỉ là điểm agent đề nghị chú ý, không phải phán đoán. Dòng để trống là
**chưa duyệt**; được phép nộp phiếu chỉ điền một phần.

## Phiếu answer (`answer-review.csv`)

Mở trang response (cột `open_page`): câu hỏi, scope, trạng thái quan sát, mọi claim/citation,
quote và đoạn text nguồn từ parser, model/provider/generation ID. Cột `machine_*` là kiểm cấu
trúc (quote có nguyên văn trong vùng được ghi, file nào), **không** chứng minh claim đúng.
`not_available` nghĩa là run không lưu đủ dữ liệu để kiểm (ví dụ evidence pack, gold label).

- `human_claim_supported` (`yes`/`partial`/`no`): đoạn được dẫn có hỗ trợ đầy đủ claim không.
  Câu so sánh phải có chứng cứ cho đủ các phía; mâu thuẫn giữa nguồn phải được nêu; reference
  list hay đoạn prompt injection không phải chứng cứ. Mọi dòng của cùng claim phải cùng giá trị.
- `human_citation_correct` (`yes`/`no`): citation trỏ đúng tài liệu/vị trí chứa nội dung hỗ trợ.
- `human_abstention_appropriate`: chỉ cho câu kỳ vọng không trả lời (`yes`/`no`); câu kỳ vọng
  có đáp án để trống hoặc `n/a`.
- `reviewer`, `review_date`, `notes`.

Mẫu Chrome chỉ có text màn hình nên được hiển thị để tham khảo, không có dòng chấm.

## Nhập và tính kết quả (từ gốc repo, PowerShell, Conda `ras`)

```powershell
$py = "$env:USERPROFILE\\.conda\\envs\\ras\\python.exe"
& $py -m app.evaluation human-validate labels evaluation\\human-review\\bundle\\label-review.csv
& $py -m app.evaluation human-import labels evaluation\\human-review\\bundle\\label-review.csv
& $py -m app.evaluation human-validate answers evaluation\\human-review\\bundle\\answer-review.csv
& $py -m app.evaluation human-import answers evaluation\\human-review\\bundle\\answer-review.csv
& $py -m app.evaluation human-report
```

`human-validate` chỉ kiểm, không ghi. `human-import` từ chối toàn bộ phiếu nếu có lỗi (dòng
thiếu/trùng/lạ, sửa nội dung, thiếu reviewer/ngày/verdict, phán đoán mâu thuẫn, bundle cũ) và
không ghi một phần. `human-report` ghi `evaluation/human-review/report.md` và `.json`.
Tên reviewer là do người điền tự ghi, không phải danh tính đã xác thực.
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, depth: int) -> bytes:
    up = "../" * depth
    return (f"<!doctype html>\n<html lang=\"vi\"><head><meta charset=\"utf-8\">"
            f"<meta http-equiv=\"Content-Security-Policy\" content=\"{CSP}\">"
            f"<meta name=\"referrer\" content=\"no-referrer\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{STYLE}</style></head><body>"
            f"<nav><a href=\"{up}index.html\">Mục lục</a> · <a href=\"{up}index.html#guide\">Hướng dẫn</a></nav>"
            f"{body}</body></html>\n").encode("utf-8")


def marked(text: str, start: int | None, end: int | None, css: str = "ctx") -> str:
    if start is None or end is None or not 0 <= start < end <= len(text):
        return f"<div class=\"{css}\">{esc(text)}</div>"
    return (f"<div class=\"{css}\">{esc(text[:start])}<mark>{esc(text[start:end])}</mark>"
            f"{esc(text[end:])}</div>")


def table(rows: list[tuple[str, str]]) -> str:
    return "<table>" + "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


# --- M4c ----------------------------------------------------------------------------------

def _region(source: Source, page_no: int | None, start: int, end: int) -> tuple[int, int, str] | None:
    for region_page, a, b, kind in source.regions:
        if region_page == (page_no or 0) and a <= start and end <= b:
            return a, b, kind
    return None


def _paper_links(manifest: Manifest, item_key: str, page_no: int | None, line: int | None, depth: int) -> str:
    item = next(i for i in manifest.items if i.key == item_key)
    if item.paper is None:
        return "metadata-only, không có file"
    paper = next(p for p in manifest.papers if p.id == item.paper)
    up = "../" * depth
    anchor = f"#page-{page_no}" if paper.format == "pdf" else f"#L{line}" if line else ""
    raw = f"{up}files/{paper.file}" + (f"#page={page_no}" if paper.format == "pdf" and page_no else "")
    return (f"<a href=\"{up}papers/{paper.id}.html{anchor}\">bản đọc {esc(paper.id)}</a> · "
            f"<a href=\"{esc(raw)}\">file gốc {esc(paper.file)}</a>")


def _item_text(manifest: Manifest, item_key: str) -> str:
    item = next((i for i in manifest.items if i.key == item_key), None)
    if item is None:
        return esc(item_key) + " (không có trong manifest)"
    if item.paper is None:
        return f"{esc(item.key)} — common metadata-only “{esc(item.title)}”"
    paper = next(p for p in manifest.papers if p.id == item.paper)
    return f"{esc(item.key)} — {esc(paper.title)} ({esc(paper.format)}, owner {esc(item.owner)})"


def _context(source: Source, page_no: int | None, start: int, end: int, css: str = "ctx") -> str:
    text = source.text if source.paper.format == "markdown" else source.pages.get(page_no, "")
    region = _region(source, page_no, start, end)
    if region is None:
        a, b, kind = max(0, start - 400), min(len(text), end + 400), "ngoài vùng index"
    else:
        a, b, kind = region
    return (f"<p>Vùng parser: <b>{esc(kind)}</b> [{a}:{b})</p>" + marked(text[a:b], start - a, end - a, css))


def label_page(question: Question, manifest: Manifest, sources: dict[str, Source], prev_id: str | None,
               next_id: str | None) -> bytes:
    q = question
    scope_items = ""
    if q.scope.kind == "collection":
        collection = next((c for c in manifest.collections if c.key == q.scope.collection), None)
        scope_items = "; ".join(_item_text(manifest, key) for key in (collection.items if collection else []))
    elif q.scope.kind == "item":
        scope_items = _item_text(manifest, q.scope.item)
    parts = [f"<h1>{esc(q.id)} — nhãn M4c</h1>",
             f"<p class=\"note\">Điền dòng <b>{esc(q.id)}</b> trong <code>label-review.csv</code>. "
             "Nhãn do coding agent soạn, trạng thái <code>pending_human_review</code>.</p>",
             f"<h2>Câu hỏi</h2><div class=\"q\">{esc(q.question)}</div>",
             table([("Split", esc(q.split)), ("Nhóm", esc(q.group)), ("Tags", esc(", ".join(q.tags) or "—")),
                    ("Identity", esc(q.identity)), ("Scope", f"{esc(human.scope_text(q.scope))}<br>{scope_items}"),
                    ("Expected status", esc(q.expected.status)), ("Expected reason", esc(q.expected.reason or "—")),
                    ("Expected coverage", esc(q.expected.coverage or "—")),
                    ("Supported claim", esc(q.supported_claim or "—")), ("Rationale", esc(q.rationale)),
                    ("Forbidden markers", esc(", ".join(q.forbidden_markers) or "—")),
                    ("Label", f"{esc(q.label_status)} / {esc(q.label_author)}")])]
    note = human.agent_note(q)
    if note:
        parts.append(f"<p class=\"warn\">{esc(note)}</p>")
    parts.append(f"<h2>Evidence ({len(q.evidence)} side)</h2>")
    if not q.evidence:
        parts.append("<p>Không có evidence: câu kỳ vọng không trả lời.</p>")
    for side in q.evidence:
        parts.append(f"<div class=\"side\"><h3>Side: {esc(side.label)}</h3>")
        for span in side.spans:
            where = (f"trang {span.page}" if span.locator_kind == "pdf"
                     else f"dòng L{span.start_line}–L{span.end_line}")
            parts.append(table([("Item", _item_text(manifest, span.item)),
                                ("Vị trí", f"{esc(where)}, offset [{span.start_offset}:{span.end_offset}) "
                                           f"— {_paper_links(manifest, span.item, span.page, span.start_line, 1)}"),
                                ("Parser", esc(span.parser)), ("Quote", esc(span.quote))]))
            source = sources.get(span.item)
            if source is not None and span.start_offset is not None and span.end_offset is not None:
                parts.append(_context(source, span.page, span.start_offset, span.end_offset))
        parts.append("</div>")
    parts.append(f"<h2>Non-evidence ({len(q.non_evidence)})</h2>")
    for entry in q.non_evidence:
        rows = [("Loại", esc(entry.kind)), ("Item", _item_text(manifest, entry.item)), ("Lý do", esc(entry.why))]
        context = ""
        source = sources.get(entry.item)
        if entry.kind == "not_extracted_region":
            rows.append(("Vị trí", f"trang {entry.page} — {_paper_links(manifest, entry.item, entry.page, None, 1)}"))
            text = source.pages.get(entry.page, "") if source is not None else ""
            context = (f"<p>Text parser của trang ({len(text)} ký tự):</p>"
                       f"<div class=\"ctx ne\">{esc(text) or '(trống)'}</div>")
        elif entry.quote is not None:
            rows.append(("Quote", esc(entry.quote)))
            if source is not None:
                try:
                    start, end = locate(source, entry.quote, entry.page)
                    rows.append(("Vị trí", _paper_links(manifest, entry.item, entry.page,
                                                       _line(source, start), 1)))
                    context = _context(source, entry.page, start, end, "ctx ne")
                except LocatorError as exc:
                    context = f"<p class=\"warn\">Không định vị được quote: {esc(exc)}</p>"
        parts.append(f"<div class=\"side ne\">{table(rows)}{context}</div>")
    parts.append("<p>" + (f"<a href=\"{esc(prev_id)}.html\">← {esc(prev_id)}</a> " if prev_id else "")
                 + (f"<a href=\"{esc(next_id)}.html\">{esc(next_id)} →</a>" if next_id else "") + "</p>")
    return page(f"{q.id} — duyệt nhãn", "".join(parts), 1)


def _line(source: Source, offset: int) -> int | None:
    if source.paper.format != "markdown":
        return None
    return source.text.count("\n", 0, offset) + 1


def paper_page(title: str, source_kind: str, *, text: str = "", pages: dict[int, str] | None = None,
               regions: list[tuple[int, int, int, str]] = (), raw_link: str, notes: str = "") -> bytes:
    parts = [f"<h1>{esc(title)}</h1><p>{notes}<a href=\"{esc(raw_link)}\">File gốc trong bundle</a></p>"]
    if source_kind == "markdown":
        refs = [(a, b) for _, a, b, kind in regions if kind == "references"]
        offset = 0
        lines = []
        for number, line in enumerate(text.split("\n"), 1):
            css = " class=\"ref\"" if any(a <= offset < b for a, b in refs) else ""
            lines.append(f"<div id=\"L{number}\"{css}><span class=\"ln\">{number}</span>{esc(line)}</div>")
            offset += len(line) + 1
        parts.append("<p>Dòng màu nâu thuộc vùng references (không phải evidence nội dung).</p>"
                     f"<div class=\"ctx\">{''.join(lines)}</div>")
    else:
        for number, page_text in sorted((pages or {}).items()):
            kinds = ", ".join(f"{kind} [{a}:{b})" for p, a, b, kind in regions if p == number) or "không có vùng index"
            parts.append(f"<h2 id=\"page-{number}\">Trang {number}</h2><p>Vùng parser: {esc(kinds)} · "
                         f"<a href=\"{esc(raw_link)}#page={number}\">mở trang PDF</a></p>"
                         f"<pre>{esc(page_text) or '(không có text layer)'}</pre>")
    return page(title, "".join(parts), 1)


def m4c_outputs(root: Path) -> tuple[dict[str, bytes], dict, list[dict]]:
    errors = validate(root)
    if errors:
        raise human.ReviewError([f"M4c dataset invalid: {e}" for e in errors])
    manifest, qset = load(root)
    binding, _ = human.m4c_binding(root)
    sources = load_sources(manifest, root)
    out: dict[str, bytes] = {}
    for paper in manifest.papers:
        out[f"files/{paper.file}"] = (root / "corpus" / paper.file).read_bytes()
        source = next(s for s in sources.values() if s.paper.id == paper.id)
        notes = ""
        if paper.format == "pdf":
            flagged = sorted(source.deficient_pages | source.failed_pages)
            notes = f"Trang thiếu/không trích xuất được: {esc(flagged or 'không')}. "
        out[f"papers/{paper.id}.html"] = paper_page(
            f"{paper.id} — {paper.title}", paper.format, text=source.text, pages=source.pages,
            regions=list(source.regions), raw_link=f"../files/{paper.file}", notes=notes)
    ids = [q.id for q in qset.questions]
    for index, question in enumerate(qset.questions):
        out[f"labels/{question.id}.html"] = label_page(question, manifest, sources,
                                                       ids[index - 1] if index else None,
                                                       ids[index + 1] if index + 1 < len(ids) else None)
    rows = human.label_rows(qset)
    out[human.LABEL_SHEET] = human.sheet_bytes(human.LABEL_COLUMNS, rows)
    groups = {}
    for q in qset.questions:
        groups.setdefault(q.group, {"dev": 0, "holdout": 0})[q.split] += 1
    binding = {k: v for k, v in binding.items() if k != "corpus_errors"} | {
        "questions": len(ids), "holdout": sum(q.split == "holdout" for q in qset.questions), "groups": groups}
    return out, binding, rows


# --- M7a ----------------------------------------------------------------------------------

def _citation_context(cited: dict, texts: dict | None) -> str:
    citation, name = cited["citation"], cited["file"]
    if name is None or texts is None:
        return "<p class=\"warn\">Không có context: file của citation không xác định được hoặc corpus lệch run.</p>"
    source, quote = texts[name], citation.get("quote") or ""
    if source["kind"] == "markdown":
        chunk = next((c for c in source["chunks"] if c[0] == citation.get("start_line")), None)
        if chunk is None:
            return "<p class=\"warn\">Không có chunk bắt đầu ở dòng được ghi.</p>"
        text = source["text"][chunk[2]:chunk[3]]
        where = f"chunk dòng L{chunk[0]}–L{chunk[1]} (parser Markdown production)"
    else:
        text = source["pages"].get(citation.get("start_page"), "")
        where = f"toàn trang {citation.get('start_page')} (parser PDF production; run không lưu offset chunk)"
    start = text.find(quote) if quote else -1
    return (f"<p>Context: {esc(where)}</p>" + marked(text, start if start >= 0 else None,
                                                    start + len(quote) if start >= 0 else None))


def answer_page(response: dict, texts: dict | None) -> bytes:
    r = response
    call = r.get("call") or {}
    selected = ", ".join(f"{s.get('model')} @ {s.get('provider')}" for s in call.get("selected") or [])
    drill = r.get("drill") or {}
    parts = [f"<h1>Response M7a — {esc(r['label'])}</h1>",
             f"<p class=\"note\">Corpus M7a riêng (2 tài liệu tổng hợp), <b>không</b> thuộc 80 câu M4c. "
             f"Response ID <code>{esc(r['response_id'])}</code>.</p>",
             table([("Câu hỏi", esc(r.get("question") or "không ghi trong artifact")),
                    ("Identity / mode", f"{esc(human.M7A_IDENTITY)} / {esc(human.M7A_MODE)}"),
                    ("Scope", esc(drill.get("scope", "không xác định"))),
                    ("Kỳ vọng (ý định drill, không phải gold label)", esc(drill.get("expected_status", "—"))),
                    ("Status quan sát", esc(r.get("observed_status") or "—")),
                    ("Reason quan sát", esc(r.get("observed_reason") if r.get("observed_reason") is not None
                                            else "không ghi (null)")),
                    ("Ledger", esc(f"seq {call.get('seq')} {call.get('route')} — {call.get('status')}"
                                   if call else "không ánh xạ chắc được")),
                    ("Model / endpoint / provider", esc(f"{call.get('model')} / {selected} / {call.get('provider')}"
                                                        if call else "không ghi")),
                    ("Generation ID", esc(call.get("generation_id") or "không ghi")),
                    ("Tokens / billed", esc(f"{call.get('prompt_tokens')} + {call.get('completion_tokens')} / "
                                            f"${call.get('provider_cost_usd')}" if call else "không ghi"))])]
    if not r["structured"]:
        parts.append("<p class=\"warn\">Chỉ có text màn hình Chrome; không chuyển thành claim/citation có cấu "
                     "trúc nên không có dòng chấm. Text màn hình có thể đã gộp xuống dòng của quote.</p>"
                     f"<pre>{esc(r.get('screen_text'))}</pre>")
    elif not r["scoreable"]:
        parts.append(f"<p class=\"warn\">Không chấm: {esc(r['not_scoreable_reason'])}.</p>")
    if r["structured"] and not r["claims"]:
        parts.append("<p>Response không có claim. Dòng phiếu: <code>"
                     f"{esc(r['response_id'])}#answer</code> (chấm abstention nếu câu kỳ vọng không trả lời).</p>")
    for claim_no, claim in enumerate(r["claims"], 1):
        parts.append(f"<div class=\"side\"><h2>Claim {claim_no}</h2><div class=\"q\">{esc(claim['text'])}</div>"
                     f"<p>comparison: {esc(claim.get('comparison') if claim.get('comparison') is not None else 'không ghi')}</p>")
        for citation_no, cited in enumerate(claim["citations"], 1):
            c = cited["citation"]
            links = ""
            if cited["file"] == "lumen-bench.md":
                links = (f"<a href=\"../papers/m7a-lumen-bench.html#L{esc(c.get('start_line'))}\">bản đọc</a> · "
                         "<a href=\"../files/m7a/lumen-bench.md\">file gốc</a>")
            elif cited["file"] == "harbor-sensor.pdf":
                links = (f"<a href=\"../papers/m7a-harbor-sensor.html#page-{esc(c.get('start_page'))}\">bản đọc</a> · "
                         f"<a href=\"../files/m7a/harbor-sensor.pdf#page={esc(c.get('start_page'))}\">file gốc</a>")
            where = f"trang {c.get('start_page')}" if c.get("locator_kind") == "pdf" else f"dòng L{c.get('start_line')}"
            parts.append(f"<h3>Citation {claim_no}.{citation_no} — dòng phiếu "
                         f"<code>{esc(r['response_id'])}#c{claim_no}.{citation_no}</code></h3>")
            parts.append(table([("Title", esc(c.get("title"))), ("Vị trí", f"{esc(where)} {links}"),
                                ("Version / file / chunk", esc(f"{c.get('version_id')} / {c.get('source_file_id')} / "
                                                               f"{c.get('chunk_id')}")),
                                ("File được dẫn (cơ sở)", esc(f"{cited['file'] or 'không xác định'} ({cited['basis']})")),
                                ("Quote", esc(c.get("quote"))),
                                ("Kiểm máy: quote nguyên văn trong vùng ghi", esc(cited["quote_check"])),
                                ("Kiểm máy: chunk trong evidence pack", human.NOT_AVAILABLE + " (run không lưu pack)"),
                                ("Kiểm máy: phủ gold span", human.NOT_AVAILABLE + " (M7a không có gold label)")]))
            parts.append(_citation_context(cited, texts))
        parts.append("</div>")
    return page(f"M7a {r['label']} — chấm answer", "".join(parts), 1)


def m7a_outputs(snapshot_bytes: bytes, corpus: Path) -> tuple[dict[str, bytes], dict, list[dict]]:
    binding, responses, extra = human.m7a_binding(snapshot_bytes, corpus)
    files, texts = extra["files"], extra["texts"]
    usable = texts if binding["corpus_matches_run"] else None
    out = {human.SNAPSHOT_FILE: snapshot_bytes, "files/m7a/lumen-bench.md": files["lumen-bench.md"],
           "files/m7a/harbor-sensor.pdf": files["harbor-sensor.pdf"]}
    lumen = texts["lumen-bench.md"]
    out["papers/m7a-lumen-bench.html"] = paper_page(
        "M7a — lumen-bench.md", "markdown", text=lumen["text"],
        regions=[(0, s.start_offset, s.end_offset, s.kind) for s in markdown.extract(lumen["text"]).sections],
        raw_link="../files/m7a/lumen-bench.md")
    harbor = texts["harbor-sensor.pdf"]
    out["papers/m7a-harbor-sensor.html"] = paper_page(
        "M7a — harbor-sensor.pdf", "pdf", pages=harbor["pages"], regions=harbor["segments"],
        raw_link="../files/m7a/harbor-sensor.pdf",
        notes="PDF render lại từ harbor-sensor.txt đúng như drill; SHA-256 khớp corpus.json của run: "
              f"{esc(binding['corpus_matches_run'])}. ")
    for response in responses:
        slug = response["response_id"].rsplit("/", 1)[1]
        out[f"answers/{slug}.html"] = answer_page(response, usable)
    rows = [row for r in responses for row in r["rows"]]
    out[human.ANSWER_SHEET] = human.sheet_bytes(human.ANSWER_COLUMNS, rows)
    return out, binding, responses


# --- index, manifest, write/check ----------------------------------------------------------

def index_page(label_rows: list[dict], m4c: dict, responses: list[dict], m7a: dict) -> bytes:
    guide = "".join(f"<pre>{esc(GUIDE)}</pre>")
    labels = "".join(
        f"<tr><td><a href=\"{esc(r['open_page'])}\">{esc(r['question_id'])}</a></td><td>{esc(r['split'])}</td>"
        f"<td>{esc(r['group'])}</td><td>{esc(r['identity'])}</td><td>{esc(r['scope'])}</td>"
        f"<td>{esc(r['question'])}</td><td>{esc(r['expected_status'])}</td><td>{esc(r['agent_note'])}</td></tr>"
        for r in label_rows)
    answers = "".join(
        f"<tr><td><a href=\"answers/{esc(r['response_id'].rsplit('/', 1)[1])}.html\">{esc(r['response_id'])}</a></td>"
        f"<td>{esc(r.get('question') or '—')}</td><td>{esc(r['observed_status'] or 'text màn hình')}</td>"
        f"<td>{len(r['rows'])}</td><td>{esc('có' if r['scoreable'] else r['not_scoreable_reason'])}</td></tr>"
        for r in responses) or "<tr><td colspan=\"5\">Artifact answer M7a không có khi chuẩn bị bundle.</td></tr>"
    groups = ", ".join(f"{g}: {c['dev']} dev + {c['holdout']} holdout" for g, c in m4c["groups"].items())
    body = (f"<h1>Bộ duyệt chất lượng M7b</h1>"
            "<p class=\"warn\">Chưa có phiếu người đọc nào được nhập khi tạo bundle này: trạng thái mặc định là "
            "<b>awaiting_human_review</b>. Trạng thái thật nằm ở <code>evaluation/human-review/report.md</code> sau "
            "<code>human-report</code>.</p>"
            f"<h2>1. Nhãn M4c — {m4c['questions']} câu, {m4c['holdout']} holdout</h2>"
            f"<p>Phiếu: <a href=\"{human.LABEL_SHEET}\">{human.LABEL_SHEET}</a>. Nhóm: {esc(groups)}.</p>"
            "<table><tr><th>ID</th><th>Split</th><th>Nhóm</th><th>Identity</th><th>Scope</th><th>Câu hỏi</th>"
            f"<th>Kỳ vọng</th><th>Agent note</th></tr>{labels}</table>"
            f"<h2>2. Answer live M7a — run {esc(m7a['run_id'])} (corpus riêng, không cộng với M4c)</h2>"
            f"<p>Phiếu: <a href=\"{human.ANSWER_SHEET}\">{human.ANSWER_SHEET}</a>.</p>"
            "<table><tr><th>Response</th><th>Câu hỏi</th><th>Status</th><th>Dòng phiếu</th><th>Chấm</th></tr>"
            f"{answers}</table>"
            f"<h2 id=\"guide\">3. Hướng dẫn</h2>{guide}"
            "<h2>4. Provenance (không cần gõ tay)</h2>"
            f"<pre>{esc(json.dumps({'m4c': {k: v for k, v in m4c.items() if k != 'labels'}, 'm7a': {k: v for k, v in m7a.items() if k != 'responses'}}, ensure_ascii=False, indent=1, sort_keys=True))}</pre>"
            "<p>Hash từng nhãn/dòng nằm trong <code>manifest.json</code>.</p>")
    return page("Bộ duyệt chất lượng M7b", body, 0)


def build(root: Path = DEFAULT_ROOT, snapshot_bytes: bytes | None = None,
          corpus: Path = human.M7A_CORPUS, m4c_parts: tuple | None = None) -> dict[str, bytes]:
    """All bundle files; ``m4c_parts`` reuses an earlier ``m4c_outputs(root)`` for the same root."""
    out, m4c, label_rows = m4c_parts or m4c_outputs(root)
    out = dict(out)
    snapshot_bytes = snapshot_bytes if snapshot_bytes is not None else human.dump_json(human.empty_snapshot())
    m7a_out, m7a, responses = m7a_outputs(snapshot_bytes, corpus)
    out |= m7a_out
    out["README.md"] = GUIDE.encode("utf-8")
    out["index.html"] = index_page(label_rows, m4c, responses, m7a)
    files = {path: human.sha256_bytes(data) for path, data in sorted(out.items())
             if path not in (human.LABEL_SHEET, human.ANSWER_SHEET)}
    out["manifest.json"] = human.dump_json({
        "format": human.FORMAT, "attribution": human.ATTRIBUTION, "m4c": m4c, "m7a": m7a,
        "sheets": {human.LABEL_SHEET: {"key": "question_id", "rows": len(label_rows), "columns": human.LABEL_COLUMNS,
                                       "fill": human.LABEL_HUMAN},
                   human.ANSWER_SHEET: {"key": "row_key", "rows": sum(len(r["rows"]) for r in responses),
                                        "columns": human.ANSWER_COLUMNS, "fill": human.ANSWER_HUMAN}},
        "files": files})
    return out


def _sheet_cells(data: bytes, columns: list[str], keep: list[str]) -> list[dict] | None:
    rows, errors = human.parse_sheet(data, columns)
    return None if errors else [{c: row[c] for c in keep} for _, row in rows]


def _human_filled(data: bytes, columns: list[str], fill: list[str]) -> bool:
    rows = _sheet_cells(data, columns, fill)
    return rows is None or any(any(v.strip() for v in row.values()) for row in rows)


SHEETS = {human.LABEL_SHEET: (human.LABEL_COLUMNS, human.LABEL_DISPLAY, human.LABEL_HUMAN),
          human.ANSWER_SHEET: (human.ANSWER_COLUMNS, human.ANSWER_DISPLAY, human.ANSWER_HUMAN)}


def check(bundle: Path, expected: dict[str, bytes]) -> list[str]:
    """Differences between a bundle on disk and a fresh build; sheets may carry filled human cells."""
    present = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} if bundle.is_dir() else set()
    problems = [f"missing: {p}" for p in sorted(set(expected) - present)]
    problems += [f"unexpected: {p}" for p in sorted(present - set(expected))]
    for path in sorted(set(expected) & present):
        data = (bundle / path).read_bytes()
        if path in SHEETS:
            columns, display, _ = SHEETS[path]
            if _sheet_cells(data, columns, display) != _sheet_cells(expected[path], columns, display):
                problems.append(f"differs: {path} (non-fill columns or rows)")
        elif data != expected[path]:
            problems.append(f"differs: {path}")
    return problems


def write(bundle: Path, outputs: dict[str, bytes], *, force: bool = False) -> None:
    """Replace the whole bundle directory; refuse to drop a sheet someone has started filling."""
    if bundle.exists():
        if not (bundle / "manifest.json").is_file() and any(bundle.iterdir()):
            raise human.ReviewError([f"{bundle} exists and is not a review bundle; choose another --bundle"])
        for name, (columns, _, fill) in SHEETS.items():
            sheet = bundle / name
            if sheet.is_file() and not force and _human_filled(sheet.read_bytes(), columns, fill):
                raise human.ReviewError([f"{sheet} already has filled or unreadable cells; import or move it first, "
                                         "or pass --force to discard it"])
    bundle.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=bundle.parent, prefix=f".{bundle.name}-"))
    try:
        for path, data in outputs.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if bundle.exists():
            shutil.rmtree(bundle)
        os.replace(staging, bundle)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
