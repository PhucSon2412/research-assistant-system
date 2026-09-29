# Bộ đánh giá retrieval/RAG ban đầu (M4c)

[Mục lục](README.md) · [Kiểm thử](testing.md) · [Search và RAG](search-and-rag.md) · [Phi chức năng](nonfunctional.md)

M4c thêm dữ liệu đánh giá, công cụ kiểm/chạy và báo cáo mẫu. PR này **không** đổi hành vi search/RAG production, không thêm streaming và không gọi OpenRouter. Mọi ngưỡng Recall@5, grounded answer, citation accuracy và abstention vẫn là **mục tiêu đề xuất, cần đo**, không phải CI gate.

## Trạng thái nhãn

Toàn bộ 80 câu, span chuẩn và lý do do coding agent soạn từ corpus tổng hợp, trạng thái `pending_human_review`, `label_author=coding_agent`. Schema chỉ nhận đúng giá trị này, nên không file nào có thể ghi là đã được người dùng hay chuyên gia duyệt. Offset/dòng do máy điền từ quote (`annotate`), còn việc chọn quote, side và kỳ vọng là phán đoán của agent cần người đọc kiểm.

## Dữ liệu

| Đường dẫn | Nội dung |
| --- | --- |
| `evaluation/rag/manifest.json` | Corpus: paper, file, version, SHA-256, kích thước, số trang, DOI hư cấu, giấy phép/phạm vi dùng, ngày kiểm, coverage kỳ vọng; item theo owner, marker ACL, collection |
| `evaluation/rag/corpus/markdown/*.md` | 12 paper Markdown tự viết |
| `evaluation/rag/corpus/pdf_sources.json`, `corpus/pdf/*.pdf` | Nguồn và bản render của 3 PDF (ReportLab `invariant`, tái lập byte-for-byte) |
| `evaluation/rag/questions.json` | 80 câu hỏi có ID ổn định |
| `evaluation/rag/review/human-review-template.csv` | Phiếu chấm cho người đọc |
| `evaluation/rag/reports/` | Báo cáo mẫu JSON + Markdown |
| `api/app/evaluation/` | Schema, locator, metric, embedder cục bộ, runner, report, review, CLI |

`.gitattributes` giữ LF/binary cho corpus để checksum không đổi khi checkout trên Windows.

### Corpus và giới hạn đại diện

15 paper tổng hợp (12 Markdown, 3 PDF sinh bằng code), tác giả, kết quả và DOI đều hư cấu, không có văn bản bên thứ ba; phạm vi dùng là đánh giá và test ACL trong repo này. Upload thành 16 item private (A 13, B 3; A và B cùng upload P11 byte-identical) và 2 item common `metadata_only` trùng DOI/tiêu đề. Sau ingest thật có **87 chunk body/abstract** được index. So với workload đề xuất 1.000 paper × 20 trang, kết quả **không đại diện** chất lượng extraction trên paper thật, từ vựng/độ dài thật, hiệu ứng quy mô lên ranking, hay ngôn ngữ/ngành khác. Không commit paper thật: ba PDF arXiv ở M2b dùng giấy phép mặc định của arXiv, không cho phép phân phối lại trong repo. Muốn thêm paper thật cần ghi giấy phép đúng version (ví dụ CC BY cho đúng file) vào manifest trước khi commit.

Ca đặc biệt: P05 chứa đoạn prompt injection; P06 là survey có reference list trỏ tới paper khác; P01/P04 mâu thuẫn về BLEU gain; P08 có trang Results chỉ có hình (trang thiếu ký tự), P09 có trang body không có text layer (bảng scan giả lập). P10 (A) và P20 (B) cùng DOI/tiêu đề với C01 common nhưng khác nội dung và có canary riêng `ZQXA7731`/`ZQXB4410`; P21 của B mang `ZQXB5582`. Admin không upload được (API trả 403), nên Admin chỉ có metadata chung.

### Câu hỏi

| Nhóm | Số câu | Holdout |
| --- | --- | --- |
| `single_paper` | 20 | 4 |
| `comparison` | 15 | 3 |
| `no_answer` | 15 | 3 |
| `partial_pdf` | 10 | 2 |
| `permission` | 10 | 2 |
| `references_conflict_injection` | 10 | 2 |

Mỗi câu tính một lần. Nhãn phụ nằm ở `tags` (ví dụ `conflict`, `bibliographic`, `prompt_injection_in_document`, `acl_pair:ACL-02`, `duplicate_hash`). 16 câu `holdout` không được dùng để chỉnh retrieval, prompt hay config; báo cáo tách dev/holdout. Cùng một câu chữ chỉ được lặp trong nhóm `permission` như cặp A/B/Admin khác quyền.

Mỗi câu có: `question`, `identity` (`user_a`, `user_b`, `admin`), `scope` (`item`/`collection`/`all` trỏ item/collection trong manifest), `expected.status` (`answered`, `insufficient_evidence`, `scope_not_found`) kèm `reason`/`coverage`, `evidence` (danh sách side; mỗi side cần ít nhất một span chấp nhận được), `non_evidence` (reference entry, injection, citing statement, claim mâu thuẫn, vùng không trích xuất được, metadata-only, item trái quyền) có lý do, `forbidden_markers`, `supported_claim` và `rationale`. Câu so sánh có một side cho mỗi paper. Reference list chỉ xuất hiện ở `non_evidence` loại `bibliography_entry`; validator từ chối span evidence nằm trong vùng references.

Span Markdown dùng dòng bắt đầu từ 1 và offset code point trong original đã decode UTF-8, như `app.ingest.markdown`. Span PDF dùng trang 1-based và offset code point trong `pdf_pages.text` do `app.ingest.pdf` trích ra (`pypdf-<version>-layout-v2`), không phải byte offset hay bounding box. Mỗi span ghi parser version; đổi parser làm validator báo lệch.

## Công cụ

```powershell
# PowerShell từ gốc repo; Conda ras; .env như dev-setup
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py -m app.evaluation validate          # schema, số câu/nhóm/holdout, checksum, quyền, gold span
& $py -m app.evaluation annotate          # điền offset/dòng từ quote rồi validate (sau khi sửa câu hỏi)
& $py -m app.evaluation build-pdfs --check  # render lại PDF và so SHA-256 với manifest
docker compose up -d --wait db minio
& $py -m app.evaluation run --output evaluation\rag\reports
& $py -m app.evaluation review-template --output review.csv --modes hybrid
& $py -m app.evaluation score-review review.csv
```

`validate` chạy parser production (Markdown và PDF qua tiến trình con) trên chính file trong repo, kiểm quote đúng tại offset, dòng khớp offset, parser version khớp, span nằm trong body/abstract, vùng `not_extracted_region` đúng là trang thiếu/không trích xuất được, evidence thuộc item của chính identity và nằm trong scope, câu `scope_not_found` đúng là scope người khác, marker cấm không nằm trong evidence và thuộc item của người khác.

`run` tạo database `ras_eval_*` và bucket MinIO tạm trên Compose, migrate tới head, rồi dùng API thật: bootstrap Admin, tạo A/B, đăng nhập, xác nhận từng upload bằng intent rồi gửi file, chạy worker thật (extraction/chunk/index), tạo collection qua API và item common bằng SQL fixture. Sau đó mỗi câu được hỏi đúng identity/scope qua `POST /search` (keyword, hybrid) và `POST /rag/ask` (keyword, hybrid), cộng một lượt vector-only qua `app.search._rank` với cùng gate SQL. Database/bucket bị xóa khi xong (`--keep` để debug).

Cách ly provider trong `run`:

- Outbound HTTP bị chặn ở `httpx.HTTPTransport` suốt lần chạy; báo cáo ghi số lần bị chặn.
- Embedding document/query dùng `local/hash-bow-512-v1`: bag-of-words băm, xác định, 0 token. Nó cho phép đi đúng đường pgvector/gate model–dimensions/fusion, nhưng **không** đo chất lượng `openai/text-embedding-3-small`.
- Generation được thay bằng capture ghi đúng payload sẽ gửi và trả `{"claims": []}`; không có câu trả lời nên chất lượng generation **not measured**.
- Rerank không yêu cầu (**not measured**).
- `pricing_verified` mặc định và trong `.env` giữ `false`; chỉ bản `Settings` trong tiến trình, gắn với fake cục bộ, bật cờ để pipeline chạy. Không đối soát charge nào vì không có lời gọi trả phí.

### Metric

- **Luật khớp chính:** một side trúng khi chunk chứa **trọn 100%** một span chuẩn của side đó: cùng item, cùng loại locator, cùng trang với PDF, offset nửa mở hợp lệ (`0 ≤ start < end`) ở cả span và chunk, và `chunk.start ≤ span.start`, `span.end ≤ chunk.end`. Chunk chỉ chứa một phần span không được tính. Luật “chunk chứa ≥ 50% ký tự span” chỉ còn là **số chẩn đoán** có nhãn riêng trong báo cáo, không phải kết quả chính và không so với mục tiêu.
- Mẫu số là câu `answered`. Giá trị của câu = số side trúng / số side; bảng báo trung bình theo câu, tổng `side trúng/tổng side` và số câu nhiều side tìm đủ **mọi** side, theo stage × nhóm/scope/split.
- **Recall@5** chỉ dành cho danh sách có thứ hạng: `search:keyword`, `search:hybrid` (keyword + vector cục bộ, RRF, không rerank) và `vector`; side phải nằm trong 5 chunk đầu.
- **Phủ evidence pack** (`rag_pack:*`) là tỷ lệ side nằm ở bất kỳ đâu trong **toàn evidence pack tối đa 6 chunk** (3 paper, 12.000 byte) mà RAG gửi generation. Đây **không** phải Recall@5 và không so trực tiếp với cột Recall@5.
- Phân lỗi: `acl` → `extraction` (không chunk active nào chứa trọn span) → `retrieval` (không có trong danh sách trả về) → `rank` (có nhưng hạng > 5) → `evidence_pack` (search nội bộ của RAG trả về nhưng pack giới hạn chunk/paper/byte không chọn). `generation` là **not measured** trong PR này.
- Với câu không có đáp án, báo cáo chỉ ghi pipeline tự trả `insufficient_evidence` trước generation hay đã gửi evidence đi; đó **không** là abstention appropriateness.
- **Candidate cho câu không có đáp án** (thêm ở M4d-1): theo mode × split × nhóm (`no_answer`, kỳ vọng `insufficient_evidence`, `answered` để so), số câu search trả ≥ 1 chunk, số hit TB, số câu từ khóa `no_terms` và số câu RAG gửi evidence. Đây không phải abstention appropriateness.
- ACL: mọi response search/vector/RAG và payload capture được kiểm item id thuộc tập được phép của identity/scope, marker của item người khác không xuất hiện, và scope trái quyền trả đúng `404 {"error":"not_found"}`. Mọi chunk xuất hiện được mở qua `/search/chunks/{id}` bằng cả ba identity: owner `200`, người khác `404`. Payload gửi model chỉ có khóa `question` và `evidence`; báo cáo đếm payload chứa văn bản chỉ có trong nhãn (`rationale`, `label_status`, ID câu hỏi). `supported_claim` có thể trùng nguyên văn paper nên không dùng làm tín hiệu rò rỉ.

## Chấm bằng người đọc

`review-template` tạo phiếu CSV mỗi dòng một câu × mode; khi có câu trả lời thật, `app.evaluation.review.answer_rows` tách một dòng cho mỗi citation và điền ba cột kiểm máy: chunk ID có trong evidence, quote nguyên văn, locator chứa trọn span chuẩn (`machine_locator_covers_gold`). Ba cột này chỉ là kiểm cấu trúc. Hai cột `response_claims` và `claim_citations` ghi số claim của response và số citation của từng claim lúc sinh phiếu, để phát hiện dòng bị thiếu. Người đọc điền:

- `human_claim_supported` (`yes`/`partial`/`no`): đọc span được dẫn trong file gốc, claim có được đoạn đó hỗ trợ đầy đủ không; câu so sánh phải có chứng cứ của đủ các phía và phân biệt ý kiến tác giả với kết luận hệ thống.
- `human_citation_correct` (`yes`/`no`): citation trỏ đúng span chứng cứ (item/version/file/vị trí), không phải reference entry hay injection.
- `human_abstention_appropriate` (`yes`/`no`/`n/a`) cho câu kỳ vọng không trả lời: hệ thống từ chối hoặc nêu rõ thiếu/mâu thuẫn, không bịa phần thiếu, không lộ item trái quyền.
- `reviewer`, `review_date`, `notes`.

`score-review` tính với tử số, mẫu số và số dòng chưa chấm:

- **Grounded answer rate** = response `answered` hoàn tất có mọi claim `yes` / số response `answered` **hoàn tất**. Một response chỉ hoàn tất khi đủ mọi claim (1..`response_claims`) và mọi dòng citation (1..`claim_citations`) của từng claim, mỗi dòng có `reviewer`, `human_claim_supported` và `human_citation_correct`. Các dòng lặp của cùng claim phải cùng `claim_text`, cùng `claim_citations` và cùng giá trị `human_claim_supported`; số citation không được trùng. Response chấm dở (`incomplete`, gồm thiếu dòng hoặc có phán đoán mà không có reviewer), mâu thuẫn (`conflicting`) hoặc chưa chấm (`unreviewed`) được đếm và liệt kê riêng, **không** vào mẫu số, không mặc nhiên thành grounded hay ungrounded.
- **Citation accuracy** = dòng citation có reviewer và `human_citation_correct=yes` / dòng citation có reviewer và giá trị.
- **Abstention appropriateness** = response cho câu kỳ vọng không trả lời có đúng một phán đoán `human_abstention_appropriate` nhất quán (từ dòng có reviewer) là `yes` / số response như vậy; phán đoán mâu thuẫn được đếm riêng.

Giá trị ngoài tập cho phép (ví dụ `probably`) làm lệnh báo lỗi. Không dùng LLM tự chấm nghĩa claim. Gold label không được đưa vào prompt RAG.

## Test

`api/tests/test_evaluation.py` (18 test): dataset commit hợp lệ và mọi nhãn `pending_human_review`; schema từ chối nhãn "đã duyệt", evidence ngoài scope, so sánh một phía, `scope_not_found` sai, marker của chính caller, câu lặp ngoài nhóm permission, thiếu holdout/số câu; checksum và cờ phân phối; lệch offset/dòng/parser và reference entry bị gắn làm evidence (Markdown); span PDF theo trang và trang không trích xuất; PDF render lại đúng SHA-256; luật phủ trọn span với các biên (vừa khít, thiếu một code point, đúng một nửa, kề nhau, locator không hợp lệ, sai trang/loại) và luật chẩn đoán 50%, Recall/phân lỗi/aggregate có mẫu số; phân biệt `evidence_pack`/`retrieval`/`rank`/`extraction`; embedder xác định, đơn vị, 0 token; `eval_settings` không đổi mặc định `pricing_verified`; chặn outbound HTTP và khôi phục; công thức chấm chỉ dựa cột người đọc, gồm response hai dòng citation mới chấm một dòng (không vào mẫu số), response hoàn tất, nhãn mâu thuẫn trên cùng claim, dòng bị xóa, phán đoán thiếu reviewer và abstention mâu thuẫn. Test ACL chạy chính runner trên PostgreSQL/MinIO thật với A/B/Admin, cùng DOI, file cùng hash và canary riêng; kiểm search/vector/RAG payload/response/viewer, IDOR item/collection, Admin không có evidence, và một generation giả cố dẫn chunk của B bị từ chối. Kiểm đối chứng thủ công: tạm bỏ điều kiện owner trong SQL search làm test ACL này fail; đã khôi phục.

## Kết quả lần chạy mẫu

Báo cáo: [`evaluation/rag/reports/rag-eval-2026-09-28.md`](../evaluation/rag/reports/rag-eval-2026-09-28.md) (JSON cùng tên có từng bản ghi). Lệnh tái lập nằm đầu báo cáo. Số liệu dưới đây chỉ đúng cho corpus tổng hợp, embedder cục bộ và config mặc định, không là baseline chất lượng production.

Lần chạy 2026-09-28 trên commit `04070f1` (tree sạch), Windows 11, Conda `ras` Python 3.12.14, pypdf 6.19.0, PostgreSQL/MinIO của Compose, khoảng 2 phút. Cả 80 nhãn vẫn `pending_human_review`, nên các số dưới đây **chưa chứng minh** đã đạt mục tiêu chất lượng nào.

- Ingest: 16/16 item `ready`/`indexed`, coverage khớp kỳ vọng (P08, P09 `partial`), 87 chunk; **63/63** side chứng cứ có chunk active chứa trọn span, nên không có lỗi `extraction` trên corpus này.
- Recall@5 (luật phủ trọn span) trên 49 câu `answered`, 63 side:

| Stage | Recall@5 TB theo câu | Side trúng / tổng | Câu nhiều side đủ mọi side | Phân lỗi |
| --- | --- | --- | --- | --- |
| `search:keyword` | 0,020 | 1/63 | 0/14 | ok 1, retrieval 48 |
| `search:hybrid` (vector cục bộ, không rerank) | 0,918 | 55/63 | 7/14 | ok 42, rank 7 |
| `vector` | 0,918 | 55/63 | 7/14 | ok 42, rank 7 |

- Phủ evidence pack (toàn pack tối đa 6 chunk, **không phải Recall@5**), cùng mẫu số:

| Stage | Phủ pack TB theo câu | Side trong pack / tổng | Câu nhiều side đủ mọi side | Phân lỗi |
| --- | --- | --- | --- | --- |
| `rag_pack:hybrid` | 0,939 | 57/63 | 9/14 | ok 44, evidence_pack 5 |
| `rag_pack:keyword` | 0,020 | 1/63 | 0/14 | ok 1, retrieval 48 |

- **Đối chiếu với phép tính review trước (hybrid 55/63, pack 57/63, luật ≥ 50%):** số mới giống hệt. Kiểm từng mẫu trên bản ghi của lần chạy: cả 315 lượt đánh giá side (63 side × 5 stage) cho cùng hạng dưới hai luật, và không có chunk trả về nào chỉ chồng một phần gold span. Lý do: mọi span chuẩn là một câu nằm gọn trong một section Markdown hoặc một trang PDF, còn chunk hiện hành là cả section (≤ 1.200 ký tự) hoặc đoạn trong một trang, nên không có span bị cắt ngang chunk. Trên corpus có section dài hơn, hai luật có thể khác nhau; khi đó chỉ số theo luật trọn span là kết quả.
- Nhóm `comparison` (12 câu có đáp án, 24 side): Recall@5 `search:hybrid` 16/24 side, đủ hai phía trong top 5 ở 5/12 câu; phủ pack hybrid 18/24 side, đủ hai phía trong pack ở 7/12 câu. Mất ở pack (CMP-04, 06, 07, 10, 13) là do chunk đúng nằm sau giới hạn 6 chunk hoặc paper đúng nằm sau 3 paper đầu. Regex so sánh nhận ra 14/15 câu (bỏ sót CMP-03 dùng “differ”).
- Recall@5 `search:hybrid` dev/holdout: dev 0,923 (45/51 side, 39 câu), holdout 0,900 (10/12 side, 10 câu). Holdout không được dùng để chỉnh gì.
- Pipeline RAG trước generation: mode hybrid gửi evidence cho 49/49 câu có đáp án và 27/28 câu kỳ vọng `insufficient_evidence` (chỉ Admin tự từ chối vì không có item); mode keyword tự từ chối 48/49 câu có đáp án vì không có hit. Ba câu scope trái quyền trả `404` ở cả hai mode.
- ACL: 400 bản ghi search/vector/RAG (response + payload) có 0 vi phạm; 87 chunk × 3 identity = 261 lần mở viewer, 0 vi phạm; payload chỉ có khóa `question`/`evidence` và 0 payload chứa văn bản chỉ có trong nhãn. **0 lỗi ACL trong phạm vi các request đã chạy**, không phải bằng chứng cho mọi đường truy cập.
- 0 lời gọi provider ngoài, 0 lần outbound HTTP bị chặn, 239 lời gọi embedder cục bộ. Chưa có response generation nào để người đọc chấm: grounded/citation/abstention có mẫu số 0.
- `not measured`: rerank, chất lượng vector ngữ nghĩa thật, grounded answer rate, citation accuracy, abstention appropriateness, latency p95, chi phí/charge.

Recall hybrid cao ở đây chủ yếu vì corpus nhỏ, câu hỏi và paper do cùng một người viết với từ vựng trùng nhau, và embedder băm là phép so khớp từ; không suy ra được chất lượng production. Số keyword thấp là phát hiện về cách dựng query lúc đó (`plainto_tsquery`), không phải lỗi dữ liệu; M4d-1 đổi cách dựng query, xem mục dưới.

## M4d-1 — keyword query cho câu hỏi tự nhiên (đo trước/sau)

Task 1 trong [roadmap](roadmap.md#task-sau-m4c). Hành vi mới ở [search-and-rag](search-and-rag.md#m4d-1--keyword-query-giới-hạn); quyết định ở [decisions](decisions.md#quyết-định-triển-khai-m4d-1-2026-09-28). Mọi số dưới đây vẫn trên corpus tổng hợp 87 chunk, nhãn `pending_human_review`, embedder hybrid là vector băm cục bộ `local/hash-bow-512-v1`: **không** tuyên bố đạt mục tiêu chất lượng production nào.

**Quy trình.** Baseline chạy `run` trên `origin/main` `99e439d` (tree sạch) trước khi sửa; tổng khớp đúng báo cáo M4c (`1/63`, `55/63`, pack hybrid `57/63`). Các phương án chỉ được so trên **39 câu có đáp án / 51 side của split `dev`** bằng một script tạm: seed corpus một lần qua API/worker thật rồi chạy lại `runner.evaluate` cho câu dev, không gọi câu holdout. Holdout chỉ được xem **một lần** trong lần chạy cuối trên commit `3e7d3b5` (tree sạch), sau khi đã chốt triển khai.

**Phương án đã thử (dev).** Cột Recall@5 và phủ pack đếm side trúng. “No-answer có cand.” là số câu nhóm `no_answer` mà keyword search trả ≥ 1 chunk (kèm số hit trung bình); “RAG kw gửi” là số câu kỳ vọng `insufficient_evidence` mà RAG keyword gửi evidence tới generation; “top-5 đúng item” là tỷ lệ hit trong 5 hạng đầu của câu có đáp án thuộc item chứa chứng cứ.

| Phương án | kw R@5 | hybrid R@5 | pack kw | pack hybrid | No-answer có cand. (TB hit) | RAG kw gửi / 22 | Top-5 đúng item |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Baseline `plainto_tsquery` (AND mọi token) | 1/51 | 45/51 | 1/51 | 46/51 | 0/12 (0) | 0 | — |
| OR mọi token, cap 32, khớp ≥ 1 (tham chiếu, không chọn) | 47/51 | 46/51 | 49/51 | 47/51 | 12/12 (10,3) | 21 | 84% |
| Bỏ stopword, cap 8, khớp ≥ 1 | 44/51 | 46/51 | 45/51 | 45/51 | 12/12 (7,5) | 21 | 86% |
| Bỏ stopword, cap 8, ngưỡng 34% term câu hỏi | 40/51 | 45/51 | 41/51 | 45/51 | 10/12 (2,0) | 16 | 89% |
| Bỏ stopword, cap 8, ngưỡng 50% term câu hỏi | 29/51 | 45/51 | 29/51 | 46/51 | 9/12 (1,1) | 14 | 94% |
| Bỏ stopword, cap 8, ngưỡng 67% | 14/51 | 45/51 | 13/51 | 46/51 | 1/12 (0,25) | 3 | 88% |
| Bỏ stopword, cap 8, AND mọi term hữu ích | 2/51 | 45/51 | 2/51 | 46/51 | 0/12 | 0 | — |
| Cap 6 / cap 12, ngưỡng 50% | 34 / 31 | 44 / 45 | 33 / 30 | 44 / 46 | 9/12 (1,4 / 1,1) | 15 / 13 | 90% / 95% |
| Ngưỡng 50% **term có trong scope đã lọc quyền** | 37/51 | 45/51 | 37/51 | 45/51 | 12/12 (4,9) | 19 | 91% |
| Ngưỡng 67% term có trong scope | 23/51 | 45/51 | 22/51 | 46/51 | 9/12 (1,25) | 14 | 93% |

Sau khi sửa lỗi nhóm từ ghép (dấu gạch nối giữa các phần làm đóng nhóm sớm) và thêm sàn 2 term, chạy lại trên hai lần seed:

| Ngưỡng (stopword, cap 8, sàn 2) | kw R@5 | hybrid R@5 (seed 1 / seed 2) | pack kw | pack hybrid | No-answer có cand. (TB hit) | RAG kw gửi / 22 | Top-5 đúng item |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 25% | 44/51 | 46 / 47 | 46/51 | 46/51 | 11/12 (3,1) | 18 | 88% |
| 34% | 41/51 | 44 / 45 | 40/51 | 45/51 | 10/12 (1,8) | 16 | 90% |
| 40% | 33/51 | 45 / 45 | 34/51 | 46/51 | 9/12 (1,7) | 15 | 95% |
| **50% (chọn)** | 28/51 | 45 / 45 | 29/51 | 46/51 | 9/12 (1,1) | 14 | 94% |

Nhận xét dùng để chọn:

- Câu hỏi hầu như luôn có từ không xuất hiện trong chunk chứng cứ: từ khung câu hỏi (“reach”, “score”, “happened”) và tên paper (“Dense Adapter Stacks”, “GSR”) mà thân bài không lặp lại. Vì vậy AND mọi term hữu ích (2/51) gần như không hơn baseline.
- Tính ngưỡng trên term có mặt trong scope bỏ luôn các từ vắng mặt như “Yoruba”, “French”, “carbon” — đúng là tín hiệu câu không có đáp án — nên no-answer nhận candidate 12/12 mà Recall vẫn thấp hơn ngưỡng 34% theo câu hỏi. Đã bỏ phương án này cùng truy vấn đếm term theo scope.
- Hạ ngưỡng tăng Recall keyword nhưng số hit của câu không có đáp án tăng 2–3 lần và hybrid dao động (34% làm pack hybrid mất 1 side so sánh ở cả hai seed). Trên corpus 1.000 paper, “2 trong 8 từ” sẽ khớp rất nhiều chunk; ngưỡng một nửa được kỳ vọng ổn định hơn khi quy mô tăng, nhưng điều này **chưa đo**.
- Chọn 50% + sàn 2 + cap 8: Recall keyword tăng lớn, hybrid và pack hybrid không thoái lui ở cả hai seed, nhiễu thấp nhất trong các phương án có recall đáng kể. 25–34% là phương án thay thế nếu chấp nhận nhiều candidate hơn; không đổi khi chưa có corpus lớn hơn.
- **Nhiễu giữa các lần seed:** tie-break cuối cùng là `chunk_id` UUID sinh mới mỗi lần ingest, nên cùng code có thể lệch ±1 side hybrid giữa hai lần chạy (quan sát được ở 25% và 34%). Trong một trạng thái DB, thứ hạng xác định.

**Trước/sau (baseline `99e439d` → `3e7d3b5`), luật phủ trọn span.** Báo cáo: [`rag-eval-2026-09-28-m4d1-keyword.md`](../evaluation/rag/reports/rag-eval-2026-09-28-m4d1-keyword.md) (JSON cùng tên); báo cáo M4c [`rag-eval-2026-09-28.md`](../evaluation/rag/reports/rag-eval-2026-09-28.md) giữ làm baseline. Recall@5 chỉ cho danh sách có thứ hạng; **phủ evidence pack** (toàn pack ≤ 6 chunk) là metric riêng, không phải Recall@5. Config lúc chạy là mặc định `SearchPolicy`: `keyword_max_terms=8`, `keyword_min_match_fraction=0.5`, `keyword_min_match_terms=2`, term 2–64 ký tự, `keyword_top_k=30`.

| Stage | Dev (39 câu, 51 side) | Holdout (10 câu, 12 side) | Tổng (49 câu, 63 side) |
| --- | --- | --- | --- |
| Recall@5 `search:keyword` | 1 → **28** (TB câu 0,026 → 0,603) | 0 → **7** (0 → 0,650) | 1 → **35** (0,020 → 0,612) |
| Recall@5 `search:hybrid` | 45 → 45 (0,923) | 10 → 11 (0,900 → 0,950) | 55 → 56 (0,918 → 0,929) |
| Recall@5 `vector` (không đổi, đối chứng) | 45 → 45 | 10 → 10 | 55 → 55 |
| Phủ pack `rag_pack:keyword` | 1 → **29** (0,026 → 0,615) | 0 → **8** (0 → 0,700) | 1 → **37** (0,020 → 0,633) |
| Phủ pack `rag_pack:hybrid` | 46 → 46 (0,936) | 11 → 12 (0,950 → 1,000) | 57 → 58 (0,939 → 0,949) |

Theo nhóm (side trúng, trước → sau):

| Nhóm | kw R@5 dev | kw R@5 holdout | hybrid R@5 dev | hybrid R@5 holdout | pack kw dev | pack hybrid dev |
| --- | --- | --- | --- | --- | --- | --- |
| `single_paper` | 0 → 10/17 | 0 → 2/4 | 17 → 17/17 | 4 → 4/4 | 0 → 10/17 | 17 → 17/17 |
| `comparison` | 0 → 9/20 | 0 → 1/4 | 14 → 14/20 | 2 → 3/4 | 0 → 10/20 | 15 → 15/20 |
| `partial_pdf` | 1 → 6/6 | 0 → 1/1 | 6 → 6/6 | 1 → 1/1 | 1 → 6/6 | 6 → 6/6 |
| `permission` | 0 → 2/3 | 0 → 2/2 | 3 → 3/3 | 2 → 2/2 | 0 → 2/3 | 3 → 3/3 |
| `references_conflict_injection` | 0 → 1/5 | 0 → 1/1 | 5 → 5/5 | 1 → 1/1 | 0 → 1/5 | 5 → 5/5 |

Theo scope, keyword Recall@5 dev: `item` 0 → 11/18, `collection` 1 → 9/20, `all` 0 → 8/13; holdout `item` 0 → 4/6, `all` 0 → 3/6 (holdout không có câu `collection` có đáp án). Hybrid theo scope dev không đổi (18/18, 15/20, 12/13); holdout `all` 4 → 5/6. Mức tăng hybrid/pack hybrid ở holdout (+1 side) nằm trong biên nhiễu ±1 do tie-break UUID nêu trên, nên không coi là cải thiện hybrid đã chứng minh; điều đo được là **không thoái lui**.

**Câu không có đáp án.** Có candidate không phải có bằng chứng; pipeline vẫn để generation và kiểm citation quyết định.

| Mode | Nhóm (dev / holdout) | Dev: có candidate (TB hit) | Dev: RAG gửi evidence | Holdout: có candidate (TB hit) | Holdout: RAG gửi evidence |
| --- | --- | --- | --- | --- | --- |
| keyword | `no_answer` (12 / 3) | 0 → 9 (0 → 1,08) | 0 → 9 | 0 → 1 (0 → 0,33) | 0 → 1 |
| keyword | kỳ vọng `insufficient_evidence` (22 / 6) | 0 → 14 (0 → 1,23) | 0 → 14 | 0 → 3 (0 → 1,17) | 0 → 3 |
| hybrid | `no_answer` | 12 → 12 (11,0) | 12 → 12 | 3 → 3 (8,0) | 3 → 3 |
| hybrid | kỳ vọng `insufficient_evidence` | 21 → 21 (11,86) | 21 → 21 | 6 → 6 (9,17) | 6 → 6 |

Trước đây keyword mode “tránh” gửi evidence cho câu không có đáp án chỉ vì gần như không khớp gì, kể cả với 48/49 câu có đáp án. Sau thay đổi, RAG keyword gửi evidence cho 17/28 câu kỳ vọng thiếu bằng chứng (hybrid vẫn 27/28); abstention đúng phụ thuộc generation và vẫn **not measured**. RAG keyword còn tự trả `insufficient_evidence` cho 6 câu có đáp án không có hit và 4 câu so sánh chỉ có hit của một paper (`comparison_requires_multiple_papers`). Không câu nào trong bộ chỉ gồm từ phổ biến (`no_terms` = 0); trường hợp đó có test riêng.

**ACL và provider:** 400 bản ghi search/vector/RAG (response + payload) và 87 chunk × 3 identity = 261 lần mở viewer, **0 vi phạm** trong phạm vi chạy, ở cả baseline và sau; 0 payload chứa văn bản chỉ có trong nhãn; 0 lời gọi provider ngoài, 0 outbound HTTP bị chặn; embedder cục bộ 239 lời gọi ở cả hai lần (keyword không thêm embedding). Regex so sánh vẫn bỏ sót CMP-03 (“differ”) và pack so sánh vẫn làm rơi chứng cứ (`evidence_pack` 5 → 4 side) — hai task riêng sau.

**Đề xuất tiếp theo (chưa làm):** đo lại trên corpus lớn hơn/paper thật trước khi hạ ngưỡng; cân nhắc trọng số theo độ hiếm của term trong scope (IDF) hoặc cụm từ (`<->`) thay vì đếm term ngang nhau; tie-break theo khóa ổn định theo nội dung để lần chạy lặp lại được; đo latency truy vấn với workload 1.000 paper; rerank thật sau khi đối soát charge.

## M4d-2 — nhận diện câu so sánh (đo trước/sau)

Task 2 trong [roadmap](roadmap.md#task-sau-m4c). Hành vi mới ở [search-and-rag](search-and-rag.md#m4d-2--nhận-diện-câu-so-sánh); quyết định ở [decisions](decisions.md#quyết-định-triển-khai-m4d-2-2026-09-28). Vẫn là corpus tổng hợp 87 chunk, nhãn `pending_human_review`, embedder cục bộ `local/hash-bow-512-v1`, generation capture không trả claim, 0 lời gọi OpenRouter: **không** chứng minh grounded answer, citation accuracy, abstention hay chất lượng production.

**Quy trình.** Luật được thiết kế từ CMP-03 (split `dev`) và các cách diễn đạt tiếng Anh tương đương, rồi kiểm ca âm trên 65 câu ngoài nhóm `comparison` (chỉ kiểm luật có coi câu là so sánh hay không; không chỉnh theo kết quả retrieval của câu nào). Để tránh nhiễu tie-break UUID giữa hai lần seed (±1 side, xem M4d-1), trước/sau được đo bằng một script tạm: seed corpus **một lần** qua API/worker thật, chạy `runner.evaluate` hai lần trên cùng DB, lần “trước” vá `app.rag.is_comparison` về luật cũ (`COMPARISON.search`) và câu truy vấn keyword về cách bỏ từ cũ. Chạy trên `dev` trong lúc làm; holdout chạy **một lần** trên commit `12f614c` (tree sạch) sau khi chốt code. Báo cáo chính thức chạy `run` trên cùng commit, rồi chạy lại trên `0660c80` (tree sạch) sau khi sửa nhánh “differ … from” theo review PR #13: [`rag-eval-2026-09-28-m4d2-comparison.md`](../evaluation/rag/reports/rag-eval-2026-09-28-m4d2-comparison.md) (JSON cùng tên; file sinh ra được đổi tên để không ghi đè báo cáo cùng ngày; bản trên `0660c80` thay bản `12f614c`). Mọi tổng Recall@5, phủ pack, pipeline RAG và ACL của hai lần chạy giống hệt.

**Nhận diện.** Nhóm `comparison`: **14/15 → 15/15** (dev 11/12 → 12/12, holdout 3/3 → 3/3). Câu ngoài nhóm bị coi là so sánh: 0/65 trước và sau (gồm PP-08 “differential privacy”, PP-05 “compared with”, PP-03 “change between … and …”). Báo cáo JSON có thêm `comparison.detected_outside_group` để thấy dương tính giả. Sửa theo review PR #13 (phía A của “differ … from” không được là đại từ hay cụm ở mệnh đề trước) không đổi kết quả của câu nào trong 80 câu: ngoài nhóm vẫn 0/65, và 15 câu được nhận diện đều thuộc nhóm `comparison` (14 câu qua luật từ vựng, CMP-03 qua “A and B differ”); không câu nào trong bộ dùng “differ … from”.

**Recall@5 và phủ pack (luật phủ trọn span, side trúng).** Cùng seed, trước và sau **giống hệt** ở mọi stage, nhóm, scope và split; CMP-03 cũng giữ nguyên thứ hạng:

| Stage | Dev (39 câu, 51 side) | Holdout (10 câu, 12 side) | Tổng, báo cáo `run` (49 câu, 63 side) |
| --- | --- | --- | --- |
| Recall@5 `search:keyword` | 28 → 28 | 7 → 7 | 35 (0,612) |
| Recall@5 `search:hybrid` | 45 → 45 | 11 → 11 | 56 (0,929) |
| Recall@5 `vector` | 45 → 45 | 10 → 10 | 55 (0,918) |
| Phủ pack `rag_pack:keyword` | 29 → 29 | 8 → 8 | 37 (0,633) |
| Phủ pack `rag_pack:hybrid` | 46 → 46 | 12 → 12 | 58 (0,949) |

Tổng của lần `run` trùng báo cáo M4d-1 ở mọi dòng. Nhóm `comparison`: Recall@5 hybrid 17/24 side (đủ hai phía 6/12 câu), phủ pack hybrid 19/24 (đủ hai phía 8/12), `evidence_pack` 4 side — phần mất ở pack thuộc task M4d-3, không xử lý ở đây. Search không dùng luật so sánh nên Recall@5 không thể đổi; phủ pack không đổi vì với CMP-03 cả hai paper đã đứng đầu thứ hạng, nên việc dành chỗ cho mỗi paper chọn đúng những chunk như trước.

**CMP-03.** Search keyword hạng side (2, 1), hybrid (2, 1), vector (1, 2). Pack keyword 2 chunk (P01, P02), side ở vị trí (1, 2); pack hybrid 6 chunk (P01, P02, P12), cả hai side trong pack. Câu truy vấn keyword không còn “differ”; payload generation vẫn là câu hỏi gốc. Điều thay đổi là **gate**: script gửi thêm ba response generation giả cho CMP-03 trên corpus này, ở scope collection và all, mode keyword và hybrid (12 lượt mỗi phía):

| Response giả | Trước | Sau |
| --- | --- | --- |
| Một claim `comparison=true` dẫn P01 và P02 | `answered` 4/4 | `answered` 4/4 |
| Hai claim riêng, mỗi claim một paper | `answered` 4/4 | `insufficient_evidence` / `comparison_requires_multiple_papers` 4/4 |
| Một claim chỉ dẫn một paper | `answered` 4/4 | `insufficient_evidence` / `comparison_requires_multiple_papers` 4/4 |

Đây chỉ là kiểm cấu trúc với claim do script soạn, không phải chất lượng answer.

**Pipeline trước generation, câu không có đáp án và ACL.** Không đổi: RAG gửi evidence cho câu kỳ vọng `insufficient_evidence` keyword 17/28, hybrid 27/28 (dev 14/22 và 21/22, holdout 3/6 và 6/6, trước = sau); RAG keyword tự từ chối 4 câu so sánh có đáp án vì pack chỉ có một paper (CMP-05, 07, 08, 11 — cùng bốn câu như M4d-1). Báo cáo `run`: 400 bản ghi search/vector/RAG và 87 chunk × 3 identity = 261 lần mở viewer, **0 vi phạm ACL**; 0 payload chứa văn bản chỉ có trong nhãn; khóa payload chỉ `question`/`evidence`; 0 outbound HTTP bị chặn; 239 lời gọi embedder cục bộ. Script trước/sau: 0 vi phạm ở cả hai phía, viewer dev 255 và holdout 246 lần mở, 0 vi phạm.

## M4d-3 — chọn evidence pack cho câu so sánh

Task 3 trong [roadmap](roadmap.md#task-sau-m4c). Hành vi ở [search-and-rag](search-and-rag.md#m4d-3--chọn-evidence-pack-cho-câu-so-sánh), quyết định ở [decisions](decisions.md#quyết-định-triển-khai-m4d-3-2026-09-29). [Báo cáo JSON và bảng chi tiết](../evaluation/rag/reports/rag-eval-2026-09-29-m4d3-pack.md) chứa cả phân bố dev trước/sau. Đo trên code commit `8197caab09a64f80868b835d3e402d630d79cb59`, `dirty=false` **trước khi seed và trước khi tạo output**, bằng lệnh `& $py api\m4d3_same_seed.py` với Conda `ras`. Corpus `ras-synthetic-rag-v1` có 87 chunk, vector băm cục bộ, generation giả trả `claims: []`, 80 nhãn `pending_human_review`; rerank và OpenRouter **không** được gọi, `pricing_verified` trong code/config vẫn `false`. Cấu hình: keyword/vector top K 30/30, tối đa 20 hit, pack 3 paper/6 chunk/12.000 byte.

**Chẩn đoán baseline M4d-2.** Tổng phủ pack hybrid 58/63 side; nhóm so sánh 19/24, đủ hai phía 8/12 câu có đáp án. Bốn side `evidence_pack` thuộc dev CMP-04/06/07/13. CMP-10 là holdout đã có chứng cứ trong pack ở baseline này. CMP-04: paper P12/P02 đều trong 3 paper đầu nhưng chunk P02 đúng ở hạng 12, sau 6 slot; CMP-06: P07 đúng ở hạng 20, trong khi P01/P08 nhiễu chen trước, nên vướng cả paper và chunk; CMP-07: P07 Methods hạng 9 và P13 Methods hạng 8 bị thay bởi chunk đầu trong cùng hai paper; CMP-13: P02 Methods hạng 6 thua abstract/section khác. Byte không phải nút thắt trên corpus này: pack gửi generation tối đa 3.207 byte text, dưới trần 12.000 byte. Con số “5 ca” gồm CMP-10 ở [baseline M4c](#kết-quả-lần-chạy-mẫu), giữ làm lịch sử và không dùng để chỉnh luật mới.

**Thử trên dev.** Các phương án M4d-3 đầu tiên: chia slot kèm đếm từ trong chunk giữ 46/51 side hybrid nhưng chuyển lỗi từ CMP-13 sang CMP-08; lọc title sơ bộ không nhận viết tắt/tên file rút gọn còn 42/51. Chọn hai title khớp đầu tiên theo thứ hạng hit rồi ưu tiên section đạt 51/51 nhưng sai danh tính khi có title nhiễu. Bản sửa PR #14 phân giải hai đoạn tên độc lập với hit rank, chặn cặp đồng hạng, tránh nhầm `GSR replication` với paper GSR gốc và viết tắt `CS` từ `code-switched`. Một bản luật quá thận trọng cho 42/51 trên dev; bản cuối đạt 51/51, không làm rơi side dev cũ. Script [`api/m4d3_same_seed.py`](../api/m4d3_same_seed.py) seed qua upload/worker thật, vá tạm pack M4d-2 cho lượt dev trước, chạy pack mới trên cùng DB rồi mới chạy holdout. Lượt `--dev-shapes` chỉ đo dev trên seed riêng; báo cáo chính chứa phân bố trước/sau trên cùng seed. Holdout hoàn chỉnh chỉ chạy sau khi chốt code, không dùng để chọn luật.

**Review ca scope hai paper.** Trên cùng seed dev, so luật ở code `8f0d04d` với code `8197caa`: Recall@5 search keyword 28/51 và hybrid 45/51 đều không đổi; phủ pack keyword **29/51 → 29/51**, hybrid **51/51 → 51/51**; nhóm comparison hybrid **20/20 → 20/20**, đủ hai phía **10/10 → 10/10**. Generation giả dev keyword **45 → 45**, hybrid **59 → 59**; số câu không có đáp án vẫn gửi evidence keyword **14 → 14**, hybrid **20 → 20**. **Không có câu dev nào đổi pack hoặc lời gọi generation**. Holdout luật mới đạt hybrid 12/12 side, 2/2 câu comparison đủ hai phía; các outcome chuẩn hóa theo câu không khác báo cáo PR #14 trước đó, nhưng hai lần holdout dùng seed khác nên không coi đây là phép so trước/sau cùng seed. Corpus này thiếu ca câu hỏi nêu GSR/DAS trên scope hai paper khác tên. Test API mới chứng minh ca đó từ chối trước generation và câu `these two papers` vẫn dùng hai paper đúng scope; vì vậy 63/63 không kiểm chứng riêng lỗi fallback cũ.

| Stage (side trúng) | Dev trước → sau (51 side) | Holdout sau (12 side) | Tổng sau (63 side) |
| --- | --- | --- | --- |
| Recall@5 search keyword | 28 → 28 | 7 | 35 |
| Recall@5 search hybrid | 45 → 45 | 11 | 56 |
| Phủ toàn pack keyword | 29 → 29 | 8 | 37 |
| Phủ toàn pack hybrid | **46 → 51** | 12 | **63** |

Nhóm `comparison` dev: Recall@5 keyword 9/20, hybrid 14/20 side (đủ hai phía lần lượt 3/10 và 5/10); phủ pack keyword 10/20, hybrid **15/20 → 20/20** (đủ hai phía **6/10 → 10/10**). Holdout sau: Recall@5 keyword 1/4, hybrid 3/4 (đủ hai phía 0/2 và 1/2); phủ pack keyword 2/4, hybrid 4/4 (đủ hai phía 1/2 và 2/2). Toàn nhóm sau: pack hybrid 24/24, đủ 12/12. Phủ toàn pack tối đa 6 chunk **không phải** Recall@5.

| Nhóm | Split | Search keyword R@5 | Search hybrid R@5 | Pack keyword | Pack hybrid |
| --- | --- | ---: | ---: | ---: | ---: |
| `single_paper` | dev | 10/17 | 17/17 | 10/17 | 17/17 |
| `single_paper` | holdout | 2/4 | 4/4 | 2/4 | 4/4 |
| `comparison` | dev | 9/20 | 14/20 | 10/20 | 20/20 |
| `comparison` | holdout | 1/4 | 3/4 | 2/4 | 4/4 |
| `partial_pdf` | dev | 6/6 | 6/6 | 6/6 | 6/6 |
| `partial_pdf` | holdout | 1/1 | 1/1 | 1/1 | 1/1 |
| `permission` | dev | 2/3 | 3/3 | 2/3 | 3/3 |
| `permission` | holdout | 2/2 | 2/2 | 2/2 | 2/2 |
| `references_conflict_injection` | dev | 1/5 | 5/5 | 1/5 | 5/5 |
| `references_conflict_injection` | holdout | 1/1 | 1/1 | 1/1 | 1/1 |

Trong 14 câu nhiều side có đáp án, pack hybrid sau đủ mọi side 12/12 dev và 2/2 holdout; pack keyword 4/12 dev và 1/2 holdout. Câu kỳ vọng thiếu bằng chứng nhưng vẫn gửi evidence cho generation giả: keyword 14/22 dev, 3/6 holdout; hybrid **21/22 → 20/22** dev, 5/6 holdout. Lời gọi generation giả dev: keyword 45, hybrid **60 → 59**; holdout sau 11 và 15; tổng sau 56 và 74. Câu có tên paper mơ hồ bị chặn trước generation. Không đánh giá abstention đúng vì capture luôn trả không có claim.

**Kích thước pack.** Trên dev cùng seed, hybrid có **60 → 59 payload**: số chunk `{3:1, 4:4, 5:5, 6:50}` → `{3:1, 4:4, 5:5, 6:49}`; số paper `{1:28, 2:6, 3:26}` → `{1:28, 2:13, 3:18}`. Text chunk byte UTF-8 min/trung vị/p95 gần nhất/max: `550/1391,5/2906/3207` → `550/1403/2906/3207`. JSON messages byte min/trung vị/p95/max: `2914/5269/7931/8230` → `2914/5274/7943/8230`. Keyword dev có 45 payload: chunk `{1:16, 2:15, 3:7, 4:5, 5:1, 6:1}` → `{1:16, 2:15, 3:10, 4:3, 6:1}`; paper `{1:34, 2:4, 3:7}` → `{1:34, 2:7, 3:4}`; trung vị text 536 byte và JSON 2.275 byte trước/sau. Phân bố holdout và tổng sau ở báo cáo JSON. Pipeline dùng **số byte JSON messages** như một trần ước lượng token đầu vào để so với `max_input_tokens_estimate=16.000` trước reservation; đây không phải token usage provider đo. Trần evidence vẫn 3 paper/6 chunk/12.000 byte, không tăng cap.

**An toàn và giới hạn.** 400 bản ghi search/vector/RAG và 261 lượt mở viewer (87 chunk × 3 identity), 0 vi phạm ACL; 0 nhãn chỉ có trong payload, 0 outbound HTTP, 239 embedding cục bộ trong lượt sau. Test bao gồm hit nhiễu, chunk sâu, cap/duplicate/reference, `metadata_only`, A/B/Admin, mất membership trước provider và khi generation đang chạy, title nhiễu xếp trước, cặp title đồng hạng và scope hai paper với tên không khớp câu hỏi. Không thay public search ranking, luật so sánh hay keyword query. Kết quả **63/63 chỉ là độ phủ evidence pack trên corpus tổng hợp nhỏ**, không chứng minh chất lượng trên paper thật; 80 nhãn agent chưa được người duyệt. Grounded answer, citation accuracy, abstention appropriateness, chi phí thật, p95 production đều chưa đo.

## Cần người đọc duyệt và việc tiếp theo

- Duyệt 80 nhãn: quote/side có đủ và đúng không, kỳ vọng `insufficient_evidence` của câu mâu thuẫn (RCI-06), bibliographic (RCI-01) và so sánh một phía có hợp ý sản phẩm không. Sau khi duyệt, đổi trạng thái cần một quyết định schema mới (hiện schema cố ý chỉ nhận `pending_human_review`).
- Chạy lại `run` với embedding/rerank/generation thật chỉ sau khi đối soát charge trên key và bật `pricing_verified` có chủ đích; khi đó mới có answer để chấm bằng phiếu người đọc và mới báo grounded/citation/abstention.
- Thêm corpus thật có giấy phép phân phối rõ cho đúng version, tiến dần tới workload 1.000 paper; giữ holdout tách khỏi mọi lần chỉnh.
- Ba phát hiện search/RAG được tách thành task riêng sau khi merge PR M4c (xem [roadmap](roadmap.md#task-sau-m4c)): keyword query yếu với câu hỏi tự nhiên (đã xử lý ở [M4d-1](#m4d-1--keyword-query-cho-câu-hỏi-tự-nhiên-đo-trướcsau)), regex so sánh bỏ sót “differ” (đã xử lý ở [M4d-2](#m4d-2--nhận-diện-câu-so-sánh-đo-trướcsau)), giới hạn pack so sánh làm rơi chứng cứ (đo ở [M4d-3](#m4d-3--chọn-evidence-pack-cho-câu-so-sánh)). Mỗi task đo lại bằng bộ này, không chỉnh theo holdout.
