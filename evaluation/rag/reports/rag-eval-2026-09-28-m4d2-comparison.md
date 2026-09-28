# Báo cáo đánh giá retrieval/RAG — lần chạy 2026-09-28

> Nhãn do coding agent soạn, trạng thái `pending_human_review`; **chưa** được người dùng hay chuyên gia duyệt. Corpus tổng hợp 15 paper, nhỏ hơn nhiều so với workload đề xuất 1,000 paper: kết quả chỉ kiểm pipeline, gate ACL và khớp locator, không đại diện chất lượng trên paper thật. Các ngưỡng trong docs là **mục tiêu đề xuất, cần đo**, không phải CI gate; vì cả 80 nhãn còn `pending_human_review`, báo cáo này **chưa chứng minh** đã đạt mục tiêu chất lượng nào.

## Tái lập

```powershell
# PowerShell từ gốc repo; .env theo docs/dev-setup.md
docker compose up -d --wait db minio
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py -m app.evaluation validate
& $py -m app.evaluation run --output evaluation\rag\reports
```

- Commit `0660c8043188621c03fc3f610e3b44b8224f53bc`
- Python 3.12.14, pypdf 6.19.0, parser `pypdf-6.19.0-layout-v2` / `markdown-lines-v2`; Windows-11-10.0.26200-SP0
- Dataset `ras-rag-eval-v1`: 80 câu, 16 holdout, 49 câu có đáp án với 63 side chứng cứ; nhóm single_paper 20, comparison 15, no_answer 15, partial_pdf 10, permission 10, references_conflict_injection 10.
- Config: keyword_top_k=30, vector_top_k=30, max_results=20, evidence ≤ 6 chunk / 3 paper / 12000 byte, chunk ≤ 1200 ký tự.

## Provider và giá

- Lời gọi provider ngoài: **0**; outbound HTTP bị chặn suốt lần chạy (số lần bị chặn: 0).
- Embedding `local/hash-bow-512-v1` cục bộ (239 lời gọi), không phải `openai/text-embedding-3-small`. Rerank không yêu cầu. Generation không gọi: payload được ghi lại, response trả không có claim.
- `pricing_verified` mặc định trong code: `{'embedding': False, 'rerank': False, 'generation': False}`; giá trị cấu hình lúc chạy: `{'embedding': False, 'rerank': False, 'generation': False}`. Chỉ bản `Settings` trong tiến trình, gắn với fake cục bộ, được bật để pipeline chạy.

## Corpus và index

15 synthetic papers (12 Markdown, 3 generated PDFs) uploaded as 16 private items plus 2 metadata-only common items, 87 indexed body/abstract chunks, versus the proposed 1,000-paper x 20-page workload. Results only exercise the pipeline, ACL gates and locator matching on short, cleanly written English text; they do not represent real extraction quality, real-paper vocabulary, scale effects on ranking, or other languages and domains.

| Item | Job | Content | Coverage (kỳ vọng) | Chunk đã index |
| --- | --- | --- | --- | --- |
| A/P01 | ready | indexed | complete (complete) | 8 |
| A/P02 | ready | indexed | complete (complete) | 7 |
| A/P03 | ready | indexed | complete (complete) | 6 |
| A/P04 | ready | indexed | complete (complete) | 5 |
| A/P05 | ready | indexed | complete (complete) | 6 |
| A/P06 | ready | indexed | complete (complete) | 4 |
| A/P07 | ready | indexed | complete (complete) | 6 |
| A/P08 | ready | indexed | partial (partial) | 7 |
| A/P09 | ready | indexed | partial (partial) | 6 |
| A/P10 | ready | indexed | complete (complete) | 4 |
| A/P11 | ready | indexed | complete (complete) | 5 |
| A/P12 | ready | indexed | complete (complete) | 5 |
| A/P13 | ready | indexed | complete (complete) | 5 |
| B/P11 | ready | indexed | complete (complete) | 5 |
| B/P20 | ready | indexed | complete (complete) | 4 |
| B/P21 | ready | indexed | complete (complete) | 4 |

Tổng 87 chunk đã index. Side chứng cứ có ít nhất một chunk active chứa trọn span chuẩn: **63/63**.

## Cách tính

Mẫu số là các câu `answered`. Một side trúng khi chunk chứa **trọn 100%** một span chuẩn của side đó: cùng item, cùng loại locator, cùng trang với PDF, offset nửa mở hợp lệ và `chunk.start ≤ span.start`, `span.end ≤ chunk.end`. Giá trị của câu = side trúng / số side; bảng báo trung bình theo câu, tổng side và số câu nhiều side tìm đủ mọi side. Kết quả: `ok`; `extraction` (không chunk active nào chứa trọn span); `retrieval` (không có trong danh sách trả về); `rank` (có nhưng hạng > 5); `evidence_pack` (search nội bộ của RAG trả về nhưng pack giới hạn chunk/paper/byte không chọn); `acl`.

- `search:keyword`: `POST /search` mode `keyword` (PostgreSQL FTS `simple`, truy vấn từ khóa giới hạn của `app.keyword_query`)
- `search:hybrid`: `POST /search` mode `hybrid`: keyword + vector băm cục bộ, fusion RRF, không rerank
- `vector`: vector-only qua `app.search._rank` (cùng gate SQL), vector băm cục bộ
- `rag_pack:keyword`: toàn evidence pack (tối đa 6 chunk) mà `POST /rag/ask` mode `keyword` dựng để gửi generation
- `rag_pack:hybrid`: toàn evidence pack (tối đa 6 chunk) mà `POST /rag/ask` mode `hybrid` dựng để gửi generation

## Recall@5 — search và vector

Chỉ các danh sách có thứ hạng (`search:*`, `vector`) được báo là Recall@5: side phải nằm trong 5 chunk đầu.

Tổng:

| Stage | Câu | Recall@5 (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- |
| `search:hybrid` | 49 | 0.929 | 56/63 | 8/14 | ok=43, rank=6 |
| `search:keyword` | 49 | 0.612 | 35/63 | 3/14 | ok=28, retrieval=21 |
| `vector` | 49 | 0.918 | 55/63 | 7/14 | ok=42, rank=7 |

Theo nhóm:

| Stage | group | Câu | Recall@5 (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- | --- |
| `search:hybrid` | comparison | 12 | 0.708 | 17/24 | 6/12 | ok=6, rank=6 |
| `search:hybrid` | partial_pdf | 7 | 1.000 | 7/7 | 0/0 | ok=7 |
| `search:hybrid` | permission | 5 | 1.000 | 5/5 | 0/0 | ok=5 |
| `search:hybrid` | references_conflict_injection | 5 | 1.000 | 6/6 | 1/1 | ok=5 |
| `search:hybrid` | single_paper | 20 | 1.000 | 21/21 | 1/1 | ok=20 |
| `search:keyword` | comparison | 12 | 0.417 | 10/24 | 3/12 | ok=3, retrieval=9 |
| `search:keyword` | partial_pdf | 7 | 1.000 | 7/7 | 0/0 | ok=7 |
| `search:keyword` | permission | 5 | 0.800 | 4/5 | 0/0 | ok=4, retrieval=1 |
| `search:keyword` | references_conflict_injection | 5 | 0.400 | 2/6 | 0/1 | ok=2, retrieval=3 |
| `search:keyword` | single_paper | 20 | 0.600 | 12/21 | 0/1 | ok=12, retrieval=8 |
| `vector` | comparison | 12 | 0.667 | 16/24 | 5/12 | ok=5, rank=7 |
| `vector` | partial_pdf | 7 | 1.000 | 7/7 | 0/0 | ok=7 |
| `vector` | permission | 5 | 1.000 | 5/5 | 0/0 | ok=5 |
| `vector` | references_conflict_injection | 5 | 1.000 | 6/6 | 1/1 | ok=5 |
| `vector` | single_paper | 20 | 1.000 | 21/21 | 1/1 | ok=20 |

Theo scope:

| Stage | scope | Câu | Recall@5 (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- | --- |
| `search:hybrid` | all | 15 | 0.933 | 17/19 | 2/4 | ok=13, rank=2 |
| `search:hybrid` | collection | 11 | 0.773 | 15/20 | 5/9 | ok=7, rank=4 |
| `search:hybrid` | item | 23 | 1.000 | 24/24 | 1/1 | ok=23 |
| `search:keyword` | all | 15 | 0.633 | 11/19 | 1/4 | ok=9, retrieval=6 |
| `search:keyword` | collection | 11 | 0.500 | 9/20 | 2/9 | ok=4, retrieval=7 |
| `search:keyword` | item | 23 | 0.652 | 15/24 | 0/1 | ok=15, retrieval=8 |
| `vector` | all | 15 | 0.900 | 16/19 | 1/4 | ok=12, rank=3 |
| `vector` | collection | 11 | 0.773 | 15/20 | 5/9 | ok=7, rank=4 |
| `vector` | item | 23 | 1.000 | 24/24 | 1/1 | ok=23 |

Dev / holdout:

| Stage | split | Câu | Recall@5 (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- | --- |
| `search:hybrid` | dev | 39 | 0.923 | 45/51 | 7/12 | ok=34, rank=5 |
| `search:hybrid` | holdout | 10 | 0.950 | 11/12 | 1/2 | ok=9, rank=1 |
| `search:keyword` | dev | 39 | 0.603 | 28/51 | 3/12 | ok=22, retrieval=17 |
| `search:keyword` | holdout | 10 | 0.650 | 7/12 | 0/2 | ok=6, retrieval=4 |
| `vector` | dev | 39 | 0.923 | 45/51 | 7/12 | ok=34, rank=5 |
| `vector` | holdout | 10 | 0.900 | 10/12 | 0/2 | ok=8, rank=2 |

## Phủ evidence pack — toàn pack tối đa 6 chunk (không phải Recall@5)

`rag_pack:*` đo side có nằm **ở bất kỳ vị trí nào** trong evidence pack mà RAG gửi generation (tối đa 6 chunk / 3 paper / 12000 byte), không giới hạn top 5. Không so trực tiếp với Recall@5 ở trên.

Tổng:

| Stage | Câu | Phủ pack (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- |
| `rag_pack:hybrid` | 49 | 0.949 | 58/63 | 10/14 | evidence_pack=4, ok=45 |
| `rag_pack:keyword` | 49 | 0.633 | 37/63 | 5/14 | ok=30, retrieval=19 |

Theo nhóm:

| Stage | group | Câu | Phủ pack (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- | --- |
| `rag_pack:hybrid` | comparison | 12 | 0.792 | 19/24 | 8/12 | evidence_pack=4, ok=8 |
| `rag_pack:hybrid` | partial_pdf | 7 | 1.000 | 7/7 | 0/0 | ok=7 |
| `rag_pack:hybrid` | permission | 5 | 1.000 | 5/5 | 0/0 | ok=5 |
| `rag_pack:hybrid` | references_conflict_injection | 5 | 1.000 | 6/6 | 1/1 | ok=5 |
| `rag_pack:hybrid` | single_paper | 20 | 1.000 | 21/21 | 1/1 | ok=20 |
| `rag_pack:keyword` | comparison | 12 | 0.500 | 12/24 | 5/12 | ok=5, retrieval=7 |
| `rag_pack:keyword` | partial_pdf | 7 | 1.000 | 7/7 | 0/0 | ok=7 |
| `rag_pack:keyword` | permission | 5 | 0.800 | 4/5 | 0/0 | ok=4, retrieval=1 |
| `rag_pack:keyword` | references_conflict_injection | 5 | 0.400 | 2/6 | 0/1 | ok=2, retrieval=3 |
| `rag_pack:keyword` | single_paper | 20 | 0.600 | 12/21 | 0/1 | ok=12, retrieval=8 |

Theo scope:

| Stage | scope | Câu | Phủ pack (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- | --- |
| `rag_pack:hybrid` | all | 15 | 0.967 | 18/19 | 3/4 | evidence_pack=1, ok=14 |
| `rag_pack:hybrid` | collection | 11 | 0.818 | 16/20 | 6/9 | evidence_pack=3, ok=8 |
| `rag_pack:hybrid` | item | 23 | 1.000 | 24/24 | 1/1 | ok=23 |
| `rag_pack:keyword` | all | 15 | 0.667 | 12/19 | 2/4 | ok=10, retrieval=5 |
| `rag_pack:keyword` | collection | 11 | 0.545 | 10/20 | 3/9 | ok=5, retrieval=6 |
| `rag_pack:keyword` | item | 23 | 0.652 | 15/24 | 0/1 | ok=15, retrieval=8 |

Dev / holdout:

| Stage | split | Câu | Phủ pack (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- | --- |
| `rag_pack:hybrid` | dev | 39 | 0.936 | 46/51 | 8/12 | evidence_pack=4, ok=35 |
| `rag_pack:hybrid` | holdout | 10 | 1.000 | 12/12 | 2/2 | ok=10 |
| `rag_pack:keyword` | dev | 39 | 0.615 | 29/51 | 4/12 | ok=23, retrieval=16 |
| `rag_pack:keyword` | holdout | 10 | 0.700 | 8/12 | 1/2 | ok=7, retrieval=3 |

## Chẩn đoán: chồng ≥ 50% span (không phải kết quả chính)

Cùng dữ liệu, nhưng side được tính khi chunk chứa ít nhất 50% ký tự span. Chỉ để thấy span bị cắt ngang chunk; không dùng làm kết quả hay so với mục tiêu. Side được index theo luật này: 63/63.

Recall@5 (chẩn đoán ≥ 50%):

| Stage | Câu | Recall@5 ≥50% (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- |
| `search:hybrid` | 49 | 0.929 | 56/63 | 8/14 | ok=43, rank=6 |
| `search:keyword` | 49 | 0.612 | 35/63 | 3/14 | ok=28, retrieval=21 |
| `vector` | 49 | 0.918 | 55/63 | 7/14 | ok=42, rank=7 |

Phủ pack ≤ 6 chunk (chẩn đoán ≥ 50%):

| Stage | Câu | Phủ pack ≥50% (TB theo câu) | Side trúng / tổng | Đủ mọi side / câu nhiều side | Kết quả |
| --- | --- | --- | --- | --- | --- |
| `rag_pack:hybrid` | 49 | 0.949 | 58/63 | 10/14 | evidence_pack=4, ok=45 |
| `rag_pack:keyword` | 49 | 0.633 | 37/63 | 5/14 | ok=30, retrieval=19 |

## So sánh nhiều paper

Ở nhóm `comparison`, cột “Đủ mọi side” là số câu có chứng cứ của **tất cả** các phía: trong top 5 với Recall@5, hoặc ở bất kỳ đâu trong pack với phủ pack. Luật nhận diện câu so sánh của RAG (`app.rag.is_comparison`) nhận ra 15/15 câu so sánh; trong 65 câu thuộc nhóm khác, luật coi là so sánh: không câu nào.

## Pipeline RAG trước generation

Generation không chạy, nên bảng chỉ cho biết pipeline gửi evidence đi hay tự trả `insufficient_evidence` trước khi gọi model; đây **không** là tỷ lệ abstention đúng.

| Mode | Kỳ vọng | Kết quả pipeline | Số lượt |
| --- | --- | --- | --- |
| hybrid | answered | evidence_sent_to_generation | 49 |
| hybrid | insufficient_evidence | abstained_before_generation | 1 |
| hybrid | insufficient_evidence | evidence_sent_to_generation | 27 |
| hybrid | scope_not_found | http_404 | 3 |
| keyword | answered | abstained_before_generation | 6 |
| keyword | answered | abstained_before_generation:comparison_requires_multiple_papers | 4 |
| keyword | answered | evidence_sent_to_generation | 39 |
| keyword | insufficient_evidence | abstained_before_generation | 11 |
| keyword | insufficient_evidence | evidence_sent_to_generation | 17 |
| keyword | scope_not_found | http_404 | 3 |

## Candidate cho câu không có đáp án

Số câu mà search trả ít nhất một chunk, số hit trung bình, số câu không còn thuật ngữ từ khóa hữu ích (`no_terms`) và số câu RAG gửi evidence tới generation. Có candidate **không** có nghĩa có bằng chứng trả lời; dòng `answered` chỉ để so sánh.

| Mode | Split | Nhóm câu | Câu | Có candidate | TB hit | Từ khóa `no_terms` | RAG gửi evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| hybrid | dev | no_answer_group | 12 | 12 | 11.0 | 0 | 12 |
| hybrid | dev | expected_insufficient_evidence | 22 | 21 | 11.86 | 0 | 21 |
| hybrid | dev | answered | 39 | 39 | 13.13 | 0 | 39 |
| hybrid | holdout | no_answer_group | 3 | 3 | 8.0 | 0 | 3 |
| hybrid | holdout | expected_insufficient_evidence | 6 | 6 | 9.17 | 0 | 6 |
| hybrid | holdout | answered | 10 | 10 | 10.3 | 0 | 10 |
| hybrid | all | no_answer_group | 15 | 15 | 10.4 | 0 | 15 |
| hybrid | all | expected_insufficient_evidence | 28 | 27 | 11.29 | 0 | 27 |
| hybrid | all | answered | 49 | 49 | 12.55 | 0 | 49 |
| keyword | dev | no_answer_group | 12 | 9 | 1.08 | 0 | 9 |
| keyword | dev | expected_insufficient_evidence | 22 | 14 | 1.23 | 0 | 14 |
| keyword | dev | answered | 39 | 33 | 1.72 | 0 | 31 |
| keyword | holdout | no_answer_group | 3 | 1 | 0.33 | 0 | 1 |
| keyword | holdout | expected_insufficient_evidence | 6 | 3 | 1.17 | 0 | 3 |
| keyword | holdout | answered | 10 | 8 | 1.1 | 0 | 8 |
| keyword | all | no_answer_group | 15 | 10 | 0.93 | 0 | 10 |
| keyword | all | expected_insufficient_evidence | 28 | 17 | 1.21 | 0 | 17 |
| keyword | all | answered | 49 | 41 | 1.59 | 0 | 39 |

## ACL

- Bản ghi đã kiểm (search, vector, RAG response + payload): 400; vi phạm: **0**.
- Viewer `/search/chunks/{id}`: 87 chunk × 3 danh tính = 261 request; vi phạm: **0**.
- Payload chứa văn bản chỉ có trong nhãn (`rationale`, `label_status`, ID câu hỏi): **0**; khóa JSON gửi model: ['evidence', 'question'].
- **0 lỗi ACL trong phạm vi các request đã chạy ở trên**; không phải bằng chứng cho mọi đường truy cập.

## Chưa đo

- `rerank`: not measured — Rerank needs an OpenRouter call; pricing_verified=false and outbound HTTP is blocked.
- `semantic_vector_quality`: not measured — Vectors come from a local hashed bag of words, not openai/text-embedding-3-small.
- `grounded_answer_rate`: not measured — No answer was generated; the capture returns no claims. Needs generation plus human review.
- `citation_accuracy`: not measured — No generated citations exist. Needs generation plus human review.
- `abstention_appropriateness`: not measured — Final abstention depends on generation for every question whose evidence reached the pack.
- `latency_p95`: not measured — Single in-process run on a dev machine with fakes; not the proposed 5-user workload.
- `cost_and_charge`: not measured — No provider call was made; ledger rows hold 0-token local estimates only.

## Theo câu hỏi

Ô search/vector: hạng từng side (`×` = không có) và kết quả Recall@5. Ô pack: vị trí side trong pack và kết quả phủ pack. Ô RAG: HTTP, trạng thái/lý do, `→ gen` nếu evidence được gửi tới generation, và số chunk trong pack.

| ID | Nhóm | Split | Danh tính | Scope | Kỳ vọng | search:keyword | search:hybrid | vector | pack:keyword | pack:hybrid | rag:keyword | rag:hybrid |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SP-01 | single_paper | dev | user_a | item | answered | ×,× (retrieval) | 3,3 (ok) | 3,3 (ok) | ×,× (retrieval) | 4,4 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| SP-02 | single_paper | dev | user_a | all | answered | × (retrieval) | 4 (ok) | 2 (ok) | × (retrieval) | 4 (ok) | 200 insufficient_evidence → gen (3) | 200 insufficient_evidence → gen (6) |
| SP-03 | single_paper | dev | user_a | item | answered | × (retrieval) | 2 (ok) | 2 (ok) | × (retrieval) | 5 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-04 | single_paper | holdout | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 6 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-05 | single_paper | dev | user_a | item | answered | × (retrieval) | 3 (ok) | 3 (ok) | × (retrieval) | 4 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| SP-06 | single_paper | dev | user_a | item | answered | × (retrieval) | 1 (ok) | 1 (ok) | × (retrieval) | 1 (ok) | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| SP-07 | single_paper | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 5 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-08 | single_paper | dev | user_a | all | answered | × (retrieval) | 1 (ok) | 1 (ok) | × (retrieval) | 3 (ok) | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| SP-09 | single_paper | holdout | user_a | item | answered | × (retrieval) | 3 (ok) | 3 (ok) | × (retrieval) | 4 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-10 | single_paper | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 5 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-11 | single_paper | dev | user_a | item | answered | 2 (ok) | 2 (ok) | 4 (ok) | 2 (ok) | 3 (ok) | 200 insufficient_evidence → gen (4) | 200 insufficient_evidence → gen (6) |
| SP-12 | single_paper | dev | user_a | all | answered | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 5 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| SP-13 | single_paper | holdout | user_a | item | answered | 1 (ok) | 1 (ok) | 3 (ok) | 1 (ok) | 4 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-14 | single_paper | dev | user_a | item | answered | 3 (ok) | 3 (ok) | 4 (ok) | 2 (ok) | 3 (ok) | 200 insufficient_evidence → gen (3) | 200 insufficient_evidence → gen (5) |
| SP-15 | single_paper | dev | user_a | collection | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 4 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-16 | single_paper | dev | user_a | all | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 4 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| SP-17 | single_paper | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 4 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (5) |
| SP-18 | single_paper | holdout | user_a | item | answered | × (retrieval) | 1 (ok) | 1 (ok) | × (retrieval) | 4 (ok) | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (5) |
| SP-19 | single_paper | dev | user_a | all | answered | 2 (ok) | 2 (ok) | 1 (ok) | 1 (ok) | 3 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| SP-20 | single_paper | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 2 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (4) |
| CMP-01 | comparison | dev | user_a | collection | answered | 2,× (retrieval) | 2,4 (ok) | 1,3 (ok) | 1,3 (ok) | 2,4 (ok) | 200 insufficient_evidence → gen (4) | 200 insufficient_evidence → gen (6) |
| CMP-02 | comparison | dev | user_a | collection | answered | 1,2 (ok) | 1,2 (ok) | 1,2 (ok) | 1,2 (ok) | 3,5 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| CMP-03 | comparison | dev | user_a | collection | answered | 2,1 (ok) | 2,1 (ok) | 1,2 (ok) | 1,2 (ok) | 1,5 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| CMP-04 | comparison | dev | user_a | collection | answered | 1,× (retrieval) | 1,12 (rank) | 1,12 (rank) | 4,× (retrieval) | 5,× (evidence_pack) | 200 insufficient_evidence → gen (5) | 200 insufficient_evidence → gen (6) |
| CMP-05 | comparison | holdout | user_a | all | answered | ×,× (retrieval) | 1,7 (rank) | 1,7 (rank) | ×,× (retrieval) | 6,3 (ok) | 200 insufficient_evidence/comparison_requires_multiple_papers (0) | 200 insufficient_evidence → gen (6) |
| CMP-06 | comparison | dev | user_a | all | answered | ×,× (retrieval) | 2,20 (rank) | 2,20 (rank) | ×,× (retrieval) | 4,× (evidence_pack) | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| CMP-07 | comparison | dev | user_a | collection | answered | ×,× (retrieval) | 9,8 (rank) | 9,8 (rank) | ×,× (retrieval) | ×,× (evidence_pack) | 200 insufficient_evidence/comparison_requires_multiple_papers (0) | 200 insufficient_evidence → gen (6) |
| CMP-08 | comparison | dev | user_a | collection | answered | ×,× (retrieval) | 6,2 (rank) | 6,2 (rank) | ×,× (retrieval) | 2,4 (ok) | 200 insufficient_evidence/comparison_requires_multiple_papers (0) | 200 insufficient_evidence → gen (6) |
| CMP-09 | comparison | dev | user_a | all | answered | 2,1 (ok) | 2,1 (ok) | 2,1 (ok) | 2,3 (ok) | 2,4 (ok) | 200 insufficient_evidence → gen (4) | 200 insufficient_evidence → gen (6) |
| CMP-10 | comparison | holdout | user_a | all | answered | 2,× (retrieval) | 2,4 (ok) | 9,2 (rank) | 2,4 (ok) | 3,6 (ok) | 200 insufficient_evidence → gen (4) | 200 insufficient_evidence → gen (6) |
| CMP-11 | comparison | dev | user_a | collection | answered | ×,1 (retrieval) | 3,1 (ok) | 3,1 (ok) | ×,× (retrieval) | 1,6 (ok) | 200 insufficient_evidence/comparison_requires_multiple_papers (0) | 200 insufficient_evidence → gen (6) |
| CMP-12 | comparison | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (4) | 200 insufficient_evidence → gen (6) |
| CMP-13 | comparison | dev | user_a | collection | answered | ×,× (retrieval) | 2,6 (rank) | 1,6 (rank) | 1,× (retrieval) | 1,× (evidence_pack) | 200 insufficient_evidence → gen (3) | 200 insufficient_evidence → gen (6) |
| CMP-14 | comparison | holdout | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| CMP-15 | comparison | dev | user_a | collection | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| NA-01 | no_answer | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| NA-02 | no_answer | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| NA-03 | no_answer | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| NA-04 | no_answer | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| NA-05 | no_answer | holdout | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| NA-06 | no_answer | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (5) |
| NA-07 | no_answer | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| NA-08 | no_answer | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (3) | 200 insufficient_evidence → gen (6) |
| NA-09 | no_answer | dev | user_a | collection | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| NA-10 | no_answer | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (5) |
| NA-11 | no_answer | holdout | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (5) |
| NA-12 | no_answer | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| NA-13 | no_answer | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (4) |
| NA-14 | no_answer | holdout | user_b | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| NA-15 | no_answer | dev | user_a | collection | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| PP-01 | partial_pdf | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 4 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| PP-02 | partial_pdf | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| PP-03 | partial_pdf | dev | user_a | item | answered | 1 (ok) | 2 (ok) | 4 (ok) | 1 (ok) | 1 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| PP-04 | partial_pdf | holdout | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (5) | 200 insufficient_evidence → gen (6) |
| PP-05 | partial_pdf | dev | user_a | all | answered | 2 (ok) | 2 (ok) | 2 (ok) | 2 (ok) | 6 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| PP-06 | partial_pdf | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 3 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| PP-07 | partial_pdf | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| PP-08 | partial_pdf | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 4 (ok) | 200 insufficient_evidence → gen (3) | 200 insufficient_evidence → gen (6) |
| PP-09 | partial_pdf | holdout | user_a | item | answered | 1 (ok) | 1 (ok) | 3 (ok) | 2 (ok) | 3 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| PP-10 | partial_pdf | dev | user_a | collection | answered | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 2 (ok) | 200 insufficient_evidence → gen (6) | 200 insufficient_evidence → gen (6) |
| ACL-01 | permission | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (3) | 200 insufficient_evidence → gen (6) |
| ACL-02 | permission | dev | user_b | all | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| ACL-03 | permission | dev | user_a | all | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 3 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (5) |
| ACL-04 | permission | holdout | user_b | all | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 200 insufficient_evidence → gen (2) | 200 insufficient_evidence → gen (6) |
| ACL-05 | permission | dev | admin | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence (0) |
| ACL-06 | permission | dev | user_a | item | scope_not_found | — | — | — | — | — | 404 error (0) | 404 error (0) |
| ACL-07 | permission | dev | admin | item | scope_not_found | — | — | — | — | — | 404 error (0) | 404 error (0) |
| ACL-08 | permission | dev | user_a | collection | scope_not_found | — | — | — | — | — | 404 error (0) | 404 error (0) |
| ACL-09 | permission | holdout | user_b | all | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 6 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| ACL-10 | permission | dev | user_a | all | answered | × (retrieval) | 1 (ok) | 1 (ok) | × (retrieval) | 3 (ok) | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| RCI-01 | references_conflict_injection | dev | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (4) |
| RCI-02 | references_conflict_injection | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (6) |
| RCI-03 | references_conflict_injection | dev | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 3 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (4) |
| RCI-04 | references_conflict_injection | holdout | user_a | item | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (4) |
| RCI-05 | references_conflict_injection | dev | user_a | collection | answered | ×,× (retrieval) | 5,3 (ok) | 5,2 (ok) | ×,× (retrieval) | 1,6 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| RCI-06 | references_conflict_injection | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (4) | 200 insufficient_evidence → gen (6) |
| RCI-07 | references_conflict_injection | dev | user_a | item | answered | × (retrieval) | 3 (ok) | 3 (ok) | × (retrieval) | 5 (ok) | 200 insufficient_evidence (0) | 200 insufficient_evidence → gen (5) |
| RCI-08 | references_conflict_injection | dev | user_a | item | answered | × (retrieval) | 5 (ok) | 5 (ok) | × (retrieval) | 4 (ok) | 200 insufficient_evidence → gen (3) | 200 insufficient_evidence → gen (6) |
| RCI-09 | references_conflict_injection | holdout | user_a | item | answered | 1 (ok) | 1 (ok) | 1 (ok) | 1 (ok) | 2 (ok) | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (6) |
| RCI-10 | references_conflict_injection | dev | user_a | all | insufficient_evidence | — | — | — | — | — | 200 insufficient_evidence → gen (1) | 200 insufficient_evidence → gen (3) |
