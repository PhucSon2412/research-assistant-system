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

Recall hybrid cao ở đây chủ yếu vì corpus nhỏ, câu hỏi và paper do cùng một người viết với từ vựng trùng nhau, và embedder băm là phép so khớp từ; không suy ra được chất lượng production. Số keyword thấp là phát hiện về cách dựng query hiện hành, không phải lỗi dữ liệu.

## Cần người đọc duyệt và việc tiếp theo

- Duyệt 80 nhãn: quote/side có đủ và đúng không, kỳ vọng `insufficient_evidence` của câu mâu thuẫn (RCI-06), bibliographic (RCI-01) và so sánh một phía có hợp ý sản phẩm không. Sau khi duyệt, đổi trạng thái cần một quyết định schema mới (hiện schema cố ý chỉ nhận `pending_human_review`).
- Chạy lại `run` với embedding/rerank/generation thật chỉ sau khi đối soát charge trên key và bật `pricing_verified` có chủ đích; khi đó mới có answer để chấm bằng phiếu người đọc và mới báo grounded/citation/abstention.
- Thêm corpus thật có giấy phép phân phối rõ cho đúng version, tiến dần tới workload 1.000 paper; giữ holdout tách khỏi mọi lần chỉnh.
- Ba phát hiện search/RAG được tách thành task riêng **sau khi merge PR này**, không xử lý ở đây (xem [roadmap](roadmap.md#task-sau-m4c)): keyword query yếu với câu hỏi tự nhiên, regex so sánh bỏ sót “differ”, giới hạn pack so sánh làm rơi chứng cứ. Mỗi task đo lại bằng bộ này, không chỉnh theo holdout.
