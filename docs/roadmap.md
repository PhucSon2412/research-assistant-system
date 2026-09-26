# Roadmap đề xuất

[Mục lục](README.md) · [Quyết định còn mở](decisions.md) · [Kiểm thử](testing.md)

Các milestone nhỏ, có thể review riêng. Thứ tự và phạm vi dưới đây là **đề xuất**. **Trạng thái:** M1a–M1c đã merge (PR #1–#3); M2a đã merge (PR #4, `66ba1d5`), M2b đã merge (PR #5, `f5483c8`). M3a/private index đang review; hybrid search, query embedding, rerank và RAG chưa triển khai. Mỗi bước giữ khả năng kiểm quyền và khả năng truy nguyên từ file/version đến chunk.

| Mốc | Phụ thuộc | Đầu ra kiểm chứng được | Rủi ro / phép thử then chốt |
| --- | --- | --- | --- |
| M0 — Quyết định nền | Bộ docs này | Stack, auth, ba model ID, key cap, xác nhận upload, arXiv/license/thu hồi, cấu hình MVP và phương pháp đo đã ghi ở [decisions](decisions.md) | Model/provider phải smoke test khi tích hợp; $50/ngày là key cap, không là credit |
| M1a — Nền dữ liệu và bootstrap (PR 1) | M0 | Compose dev cho web/API/PostgreSQL/Redis/MinIO, cấu hình an toàn, migration nền cho User và item/version/file/job/audit, kèm khóa nối cho rights/intent (bảng tạo ở PR sau, xem [decisions](decisions.md#ghi-nhận-khi-triển-khai-m1a-2026-09-24)); lệnh bootstrap Admin một lần, mật khẩu nhập an toàn | Dựng/hạ Compose theo hướng dẫn; migration trên DB trống và chạy lại; bootstrap lần đầu thành công, lần hai từ chối, không hardcode/echo/log mật khẩu; chưa nhận upload |
| M1b — Invite, session và ACL (PR 2) | M1a | Invite-only, login/logout/revoke, reset password; delivery adapter và dev outbox có kiểm soát; session server-side theo thời hạn cấu hình; API kiểm quyền common/private trên mỗi request; metadata library tối thiểu, audit | Contract/integration test invite dùng một lần/expiry, idle/absolute expiry, reset thu hồi session; hai User và Admin: User A/B không xem metadata của nhau qua ID/list/count; Admin không mặc nhiên đọc private; token không lộ qua log |
| M1c — Upload intent và original (PR 3) | M1b | UI thông báo/xác nhận mỗi upload → API tạo intent không có file → UI mới gửi PDF/Markdown; API kiểm intent một lần đúng owner rồi lưu original qua S3 adapter, item private, job `received`; giới hạn file và định dạng thật | E2E hai User: A upload và tải original, B không xem/tải bằng URL/ID; thiếu/sai/hết hạn/tái dùng intent không tạo file/item/job; thử race; oversize/giả MIME bị từ chối; chưa gọi provider hay chạy parser |
| M2 — Ingest PDF/Markdown | M1c | Redis/Celery worker; pipeline full text chung cho upload và discovery đủ quyền, validate, extract/coverage, section/reference, chunk locator, retry; parser timeout/resource limit; `needs_review` | Fixture PDF/MD, partial/scan, kill/retry; ngưỡng 70%/2.000 ký tự từ config, false rejection được báo; không đạt không indexed; không mất file/nhân đôi chunk |
| M3 — Index và search | M2; kiểm model/provider/dimensions | Collection cá nhân; PostgreSQL keyword + pgvector; OpenRouter `openai/text-embedding-3-small` document/query, `qwen/qwen3-reranker-8b` trên candidate có quyền; budget/quota/idempotency/reindex; hybrid retrieval | Test leak search/count/facet/rerank payload, model/dimensions match, rerank benefit/latency, reindex/swap; đo baseline |
| M4 — RAG có dẫn nguồn | M3; kiểm generation route | OpenRouter `openai/gpt-6-sol`; scope paper/collection/all, evidence pack giới hạn, answer/citation/insufficient evidence, viewer mở vị trí | Bộ ≥ 80 câu, citation/grounding/abstention, 0 chunk riêng trái quyền trong provider payload; token đầu/hoàn tất và chi phí |
| M5 — Discovery và citation | M2–M4 | arXiv scheduler theo chủ đề AI/ML; paper `metadata_only` chỉ ở trang khám phá; gate quyền **đúng file version** ghi nguồn/license/bằng chứng/ngày/người duyệt/phạm vi; chỉ sau active index thành công thành `indexed`; references/graph với ACL | Không quyền/index lỗi vẫn metadata-only; test result/count/facet/context; từ `t_rev` request mới bị chặn, job nhận tín hiệu hủy; item riêng khác độc lập |
| M6 — Công bố và vận hành | M1c–M5 | Review grant đúng version → common item với `AccessRights` riêng; delete/tombstone/restore, backup daily giữ 30 ngày và diễn tập tháng, metrics/alerts, load/security/accessibility baseline | User upload không đủ quyền công bố; từ chối hết review; từ `t_rev` request mới bị chặn dù cache/index cũ; RAG đang chạy dừng phát khi phát hiện; xóa private không phá common; cleanup ≤ 24h, restore không tái hiện; RPO/RTO thử ≤ 24h; SLO/quality gate chỉ chốt sau baseline |

**Đề xuất:** Nếu cần đánh giá discovery sớm, có thể làm luồng **metadata + link arXiv** ngay sau M1c; paper chỉ xuất hiện ở trang khám phá cho tới khi M2/M3 và gate quyền hoàn tất. Mỗi PR/mốc kết thúc bằng demo, test, giới hạn và cập nhật docs/decisions. Kết thúc **toàn bộ M1** phải có demo hai User: A upload private và mở original, B không thể xem qua list/ID/link, cùng Admin quản lý metadata item chung mà không đọc private của A ngoài review. [Workload phi chức năng](nonfunctional.md) chỉ là bài đo ban đầu, không là SLO trước benchmark.

## Task hiện tại và tiếp theo

**M1a (đã merge):** Compose dev, migration nền `0001`, bootstrap Admin một lần — xem [dev-setup](dev-setup.md).

**M1b (PR #2, đã merge):** migration `0002` cho invitation/session/reset token (chỉ lưu hash); login/logout, thu hồi session, reset bằng link một lần; session server-side hết hạn theo idle/absolute trong config; CSRF cho thao tác ghi; delivery adapter + dev outbox; Admin quản lý tài khoản; API metadata `list/count/detail` lọc common/private trước phân trang/đếm; web tối thiểu cho auth và xem metadata.

**M1c (PR #3, đã merge):** có hai bước intent không file rồi upload, lưu original private, download kiểm ACL, migration `0003`/`0004` ràng buộc job/version của file cùng item, đường streaming riêng trong Next.js. [Dev-setup](dev-setup.md) ghi cách kiểm chứng và giới hạn.

**M2a (PR kế tiếp):** worker Celery và bộ quét job bền trên PostgreSQL, Markdown extraction/section/chunk có locator, retry/duplicate trong phạm vi owner, trạng thái `awaiting_index`. Item vẫn `metadata_only`. M2b sẽ dùng schema kết quả và thêm adapter PDF/coverage theo trang; M3 mới index.

## Ưu tiên sau MVP — đã xác nhận về thứ tự

1. DOI/arXiv URL import: metadata + link trước; full text chỉ nếu có file và quyền.
2. Hugging Face daily sau khi arXiv ingest/chống trùng/`metadata_only` ổn định; gộp cùng paper theo identifier mà không nhân đôi full text.
3. Citation graph nâng cao sau khi đo chất lượng extraction/reference matching.
4. Format mới: HTML/JATS XML khi có nguồn cấu trúc, DOCX, BibTeX/RIS chỉ metadata hàng loạt, rồi LaTeX source.
5. Nhóm/chia sẻ sau cùng vì thay đổi ACL, RAG scope và xóa; bắt đầu khi test chống lộ đã ổn định.
