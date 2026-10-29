# M11 — thư viện cá nhân và lô paper đã chốt

Runtime/hậu kiểm hoàn tất ngày **27-10-2026 (Asia/Saigon)** trên nhánh `ops/m11-demo-knowledge-base`,
base `1f7bfac970fa0015a6d3211d3514a4136df4d4d6`. [PR #72](https://github.com/PhucSon2412/research-assistant-system/pull/72),
không merge. Chủ dự án đã chốt dừng upload ngày 26-10; lần này chỉ hoàn tất receipt cũ.

## Kết quả thư viện

| Trạng thái của lô M11 trong phạm vi | Số paper |
| --- | ---: |
| Indexed (`ready`) | 1.631 |
| Cần xem lại (`pdf_body_uncertain`) | 629 |
| Lỗi index/trích xuất (`terminal_failed`) | 873 |
| Từ chối full text (`pdf_body_below_threshold`) | 17 |
| Bỏ qua trước upload (`too_many_pages`) | 8 |
| Tổng bản ghi manifest đã xử lý | **3.158** |

**3.150 receipt** đã terminal; lỗi/từ chối gộp là **890**. Có 765 `pdf_text_invalid`,
103 `provider_outcome_uncertain`, 5 `embedding_outcome_uncertain` và 17 từ chối full text.
Không xóa item lỗi, không OCR, không tự retry/re-index outcome chưa rõ để làm đẹp số.

Thư viện user thực tế có **3.167 item, 1.643 indexed**, gồm 12 item cũ,
3.150 receipt mục tiêu và 5 item ngoài chủ đề được giữ lại. Năm item đó ở collection xem lại
(2 indexed, 1 cần xem lại, 2 lỗi), không tính vào lô mục tiêu.
Đã đối chiếu **3.155 tiêu đề thật và membership** qua API; 12 original cũ vẫn đúng SHA-256 baseline.
Không có admission upload hoặc item ID mới trong lượt hoàn tất ngày 27-10.

| Collection | Item được giữ | Indexed |
| --- | ---: | ---: |
| Dense retrieval & embeddings | 369 | 227 |
| Reranking & search | 412 | 219 |
| Retrieval-augmented generation | 265 | 136 |
| RAG evaluation & benchmarks | 532 | 266 |
| Grounding & hallucination | 595 | 282 |
| Long context & chunking | 286 | 152 |
| Search agents & tools | 691 | 349 |
| Cần xem lại — ngoài phạm vi M11 | 5 | 2 |

Tổng qua API: **54.751 trang**, **7.299.246.539 byte file gốc**,
**112.573 chunk dẫn xuất**, trong đó **68.162 chunk body/abstract thuộc item indexed**.
Chunk còn lại của job lỗi không được tính là searchable. Các số này bao gồm cả 12 item cũ và 5 item ngoài phạm vi.

## Corpus, license và file

Manifest cuối có 2.915 CC BY 4.0, 196 CC BY-SA 4.0 và 47 CC0 1.0, kể cả 8 file bỏ qua.
Kiểm license theo đúng version: OAI latest + exact abs cho lô OAI; Atom entry có version +
latest abs + exact abs cùng license/version cho lô Atom. Giữ hash/cache bằng chứng HTML/XML ngoài Git;
không nhận NC/ND, license mặc định hoặc bằng chứng bất đồng.

Đã đối chiếu SHA-256/kích thước **4.601 PDF** của manifest trước khi chốt; giữ nguyên 300 paper đầu,
không trùng arXiv ID hoặc SHA-256 của 12 paper cũ. Manifest xử lý có **7.337.957.314 byte PDF**,
gồm cả 8 file không upload. Các giới hạn PDF/parser của image rc2 giữ nguyên.

1.443 PDF chưa nạp được ghi `owner_stop_not_uploaded`, cùng 48 file quá lớn trong
`evaluation/m11-papers/download-exclusions.json`. 48 mục ngoài chủ đề giữ ở `topic-exclusions.json`,
trong đó 5 đã upload được giữ lại ở collection xem lại. Không xóa PDF hoặc ghi công.
PDF và `ATTRIBUTION.md` ở `%USERPROFILE%\ras-pilot\demo-papers\`; repo chỉ giữ metadata/manifest và script tải lại.

## Chi phí

| Loại số liệu | Kết quả |
| --- | --- |
| Billed thực riêng M11 đã xác minh với provider | **Chưa xác minh được** |
| Cam kết ledger trước 15 câu thử | $33,7896042 |
| Trần giữ cho đúng 15 câu thử | $0,813 |
| Cam kết ledger cuối | **$34,6026042** |
| Ước tính embedding theo token/giá trong rc2 | $1,60245220 cho 69.990 call completed |
| Call embedding chưa rõ kết quả | 109; giữ $0,17858560 ceiling ở kế toán sản phẩm |

Cam kết là tổng ceiling bảo thủ của từng operation, giữ cả phần job đã xong/lỗi; **không phải tiền đã billed**.
Ứng dụng rc2 lưu `usage_price_estimate`/ceiling nhưng không lưu provider generation ID/receipt của embedding.
Không thể gán delta usage của key nhóm cho M11. Không báo billed bằng 0, cũng không đổi estimate thành invoice.
Raw probe/key meter chỉ trả số tổng; key ở env demo, không đọc file key hoặc `.env`.

Giữ trần tổng $50, dừng 80% ở $40; model/quota/budget ngày của app không đổi.
Quyết định dừng upload của chủ dự án thay mục tiêu mở rộng tới ngân sách, nên phần còn lại không được tiêu để index thêm.
`upload-admission-closed.json` vẫn tồn tại, và ledger từ chối admission `upload:*` ngay cả khi chạy lại client.

## 15 câu thử thật

Đây là smoke trên API sản phẩm bằng user demo, **không chạy lại evaluation M7c hoặc chấm phiếu human**.
Đúng 11 câu RAG (5 một paper, 3 không có đáp án, 3 so sánh) và 4 hybrid search có rerank.
Không retry request trả phí. Raw answer/chunk ở evidence ngoài Git; bảng chỉ tóm tắt kết quả.

| # | Nhóm | Kết quả API | Claim | Citation khớp nguồn |
| ---: | --- | --- | ---: | ---: |
| 1 | Một paper | answered | 3 | 3/3 |
| 2 | Một paper | answered | 2 | 2/2 |
| 3 | Một paper | answered | 4 | 4/4 |
| 4 | Một paper | answered | 1 | 1/1 |
| 5 | Một paper | answered | 1 | 1/1 |
| 6 | Không có đáp án | insufficient_evidence | 0 | 0/0 |
| 7 | Không có đáp án | insufficient_evidence | 0 | 0/0 |
| 8 | Không có đáp án | insufficient_evidence | 0 | 0/0 |
| 9 | So sánh | insufficient_evidence | 0 | 0/0 |
| 10 | So sánh | insufficient_evidence | 0 | 0/0 |
| 11 | So sánh | insufficient_evidence | 0 | 0/0 |
| 12 | Hybrid search | 20 hit; rerank applied | 0 | 0/0 |
| 13 | Hybrid search | 20 hit; rerank applied | 0 | 0/0 |
| 14 | Hybrid search | 20 hit; rerank applied | 0 | 0/0 |
| 15 | Hybrid search | 20 hit; rerank applied | 0 | 0/0 |

**15/15 request có response**, 0 lỗi request; **11/11 citation khớp quote/locator**. Câu một paper có claim: 5/5; câu không có đáp án trả về không có claim: 3/3; so sánh có claim: 0/3. Kết quả từ chối/lỗi được giữ nguyên, không đổi câu hỏi rồi chạy lại để tăng số pass.

Quote được đối chiếu với `/search/chunks/{id}` của chính user, gồm nguyên văn,
item/version/file/page và original URL. Điều này xác minh locator/quote, không tự trở thành điểm human quality.
Các câu phù hợp để dùng và giới hạn so sánh được ghi trong [handoff M9](m9-lan-demo-deployment.md).

## Backup và vận hành

| Mốc | Backup | Cửa sổ bảo trì | Cả lượt | Ciphertext |
| --- | --- | ---: | ---: | ---: |
| Trước nạp | `20261023T130947Z-e981fa49` | 19,42 s | 27,73 s | 10.058.385 byte |
| Sau chốt và 15 câu thử | `20261027T082255Z-2d3d4bae` | **293,23 s** | 1147,95 s | **7.999.136.734 byte** |

Backup sau: status `ok`, reopen `ok`, **3.167 object**, dump **694.856.503 byte**;
ciphertext SHA-256 `df7fa157941c773b6b6e26beaa4c7c2448a8ea2a334624acb6715b2a040d6a6b`. Dùng entry point `ras_ops.py` đóng băng trong release,
explicit `--env-file`/project/compose, `backup --no-alert` rồi `check` và `status`.
Kết quả `ras_ops check`: exit 0, conditions/new/repeat đều rỗng, không có delivery error. Một email báo resolved hai cảnh báo cũ `backup_failed:preflight_failed` và `backup_stale`; không có cảnh báo mới. Status không còn maintenance/lock, replica equal seq 17/lag 0, đủ 9 service chạy lại. Retention không prune bản backup nào.

Có preflight fail trong lịch sử trước lượt này; preflight mới pass, staging còn 266.209.677.312 byte.
Không restore/drill, không `down`, không xóa dữ liệu/backup thủ công, không kill process theo tên.
Đích backup/replica cùng máy theo cấu hình M9 hiện có; kết quả này không chứng minh miền sự cố ngoài máy.

Edge vẫn chỉ **127.0.0.1:8443**; CA demo cũ, leaf DNS-only `ras-demo.lan`.
HTTPS CA/SNI/Host trả 200, IP trần bị TLS từ chối, Ethernet/Radmin/WSL đóng.
Không sửa hosts/firewall/trust store; hướng dẫn chủ dự án tự chạy ở M9.
API rc2/web ui2/ops image và cây `api/app`, `web`, `deploy/` giữ nguyên so với base M11.

## Evidence, kiểm và dùng lại

Evidence ở `%USERPROFILE%\ras-pilot\evidence\m11\`:

- `finalization-20261027/corpus-audit.json`: hash, license, 300 đầu và loại file chưa upload.
- `verified-metadata.json`, `baseline-originals-final-verified.json`: title/collection và original cũ.
- `inventory.json`, `smoke.json`, `smoke-answer-*.json`, `source-*.json`: API thật; không commit raw text/answer.
- `final-cost-report.json`, `cost/ledger.jsonl`: tách estimate, unknown và cam kết.
- `final-backup.json`, `final-check.json`, `final-status.json`, `final-verify.json`: backup/check/HTTPS.
- `offline-final-20261027.xml`: **104/104 test offline pass**; kiểm corpus đầy đủ không còn deselected.

Hai lần thử test ngày 26-10 lỗi do sandbox temp và guard ledger trong repo được giữ riêng;
lượt chạy ngoài repo sau đó 102 pass. Lượt đọc inventory tuần tự ngày 27-10 được dừng đúng PID
sau khi mọi metadata mutation đã có receipt; chỉ các GET được chuyển sang nhóm tối đa 4.
Mutation vẫn tuần tự, không tự retry. Các bước này không đổi code ứng dụng hoặc nạp thêm file.

CI của **head bàn giao cuối** phải kết thúc trước bàn giao; run ID/head và kết quả từng job
được ghi trong mô tả PR #72 và báo cáo bàn giao. Không dùng run xanh của head cũ để thay head mới.
Giữ author/committer, không AI trailer; PR mở cho review, không merge.

Từ repo, kiểm chỉ đọc:

```powershell
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m11_status.py
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m9_lan_demo.py verify
```

Tải lại manifest vào thư mục ngoài Git bằng `scripts/m11_papers.py fetch --dest <thư-mục-ngoài-repo>`;
script kiểm lại license/version/checksum và ghi attribution. Nếu metadata/version hiện hành lệch thì dừng để review.
Không chạy helper mở rộng, không gỡ marker đóng upload, không replay 15 câu hỏi khi `smoke.json` đã có record.
`run` nếu được dùng lại ở demo hiện tại chỉ hoàn tất receipt cũ; app UI/quyền truy cập không bị thay đổi.
