# Review PR #42: final read clock sau SQL

**Cập nhật sau review (2026-10-06):** chủ dự án đã merge; fetch/GitHub CLI xác minh merge/main `99db5285dfa59dd3d26e86345e849393ed88ce33`. Task tiếp [C2d-4a notice/intent](../tasks/m6c2b2c2d4a-upload-notice-intent.md) đã giao, chưa triển khai. Trạng thái OPEN trong phần kết luận dưới đây là snapshot lúc review, kết quả kiểm không đổi. Safety pending tới C4.

[Mục lục](../README.md) · [Task](../tasks/c2d-final-read-clock.md) · [Bàn giao](../c2d-final-read-clock.md) · [Inventory agent](../c2d-final-read-clock-test-inventory.json)

## Kết luận

Ngày 2026-10-06: fetch/GitHub CLI xác minh PR #42 **OPEN/chưa merge**, head `8f924c079b4e68a5d480c2c57d3b3da9708bd253`, base `5100ff67c5c61754e4dd29693cb670a22ce6cf83` (PR #41). Code/test `18faa3065de52b0fec4e9f0a441fcf5ae4ebba19`; head thêm docs/inventory, không đổi code/test. Source migration `0039`, không truy vấn DB dev.

**Không phát hiện điểm chặn merge trong phạm vi PR #42; có thể merge.** P2 clock ở read helper và final predicate riêng của session listing đã được khắc phục tại source này theo kiểm độc lập bên dưới. Không suy toàn C2d/T14 hoặc production/restore an toàn; safety **pending tới C4**. Reviewer chỉ sửa docs, không sửa ứng dụng, merge/push/comment PR. Docs review chưa commit để coding agent giữ vào PR phù hợp.

## Đối chiếu source

- Đúng hai file ứng dụng đổi: [final_caller](../../api/app/identity/final_caller.py) và [auth route](../../api/app/api/routes/auth.py). Trong auth route chỉ AST `my_sessions` đổi; bỏ import predicate cũ. Migrations, grants/predicates/authz, resource/review/writer boundaries, web và Compose giữ nguyên.
- Helper vẫn lấy journal shared/gate và scalar profile/session với exact IDs. SQL predicate thật kiểm latest head/receipt/generation/status/session. Sau predicate, clock mới kiểm absolute và idle từ timestamps scalar sẵn có; không query thêm cho TTL. `checked_at` dùng cùng mốc sau SQL; idle expiry vẫn capped bởi absolute.
- Tại/quá expiry trả `401 session_expired`, clear cookie; sát trước hạn vẫn được phép. Current Admin role vẫn kiểm riêng `403`. Không thêm row locks, touch activity, commit/rollback helper hoặc mở quyền private cho Admin.
- Session listing giữ SQL lọc owner và từng session trước dựng response; final helper kiểm lại đúng caller sau listing trên cùng transaction/journal shared. Shape response giữ nguyên; không suy session khác còn sống thành quyền của caller đã hết hạn.
- DevOutbox giữ audit commit và proof mới sau commit/snapshot, lọc link TTL theo clock mới. Bốn writer có JSON DTO vẫn kiểm transaction mới sau known commit; `_actor` trước commit và hai DELETE 204 không đổi. Response denial không chứng minh mutation/audit chưa lưu.
- Bảng 32 route liên quan trong handoff giữ các quyền owner/review grant/thin history/pending/common riêng; các caller writer/file/provider chưa chuyển không nằm trong kết luận.

## Kiểm độc lập tại head PR

| Nhóm | Node pass |
| --- | ---: |
| Test clock mới | 65 |
| C2a session/CSRF/logout/listing | 6 |
| C2d-1 caller/profile/role/expiry/pending/lock wait | 18 |
| C2d-2a: 13 resource success/empty branches | 13 |
| C2d-2b: 9 review/publication success branches | 9 |
| C2d-3: 6 success + 6 post-SQL clock trước commit | 12 |
| ORM/schema | 1 |
| **Tổng selection reviewer** | **124/124** |

0 failure/error/skip, **245,542 s**, 3 warning hiện có (Starlette/httpx và hai computed default Alembic). JUnit 124 unique khớp selection; **218 API Python/SQL hash không đổi**. SHA-256 JUnit: `89bdc436f673b529bad4ff841297632ad8c30c07a5550c7fd041f4d79526ef46`.

**Đối chứng độc lập:** tiến trình riêng nạp riêng body `final_caller` từ base PR #41 bằng AST trước import caller/test, không sửa file ứng dụng. Chạy 8 ca collections/listing: **6 failure đúng 200 thay vì 401** tại/quá idle/absolute expiry, **2 positive sát trước hạn pass**, 0 setup error/skip, 16,89 s. Mỗi hook vẫn gọi SQL thật rồi mới tăng fake clock; không thay boolean predicate/head/receipt. Source hash trước/sau đối chứng vẫn khớp 218 file hiện tại. Các failure có chủ đích này tách khỏi selection 124; không coi suite hiện tại fail hoặc cộng thành pass.

**Artifact agent:** kiểm ba JUnit có sẵn theo SHA/node trong inventory: 1094 node unique, không thiếu/trùng, không failure/error/skip; 2530 collected unique, 1436 not-run. Reviewer chưa chạy lại **970/1094** node còn lại của suite agent hoặc **2406/2530** node ngoài selection độc lập. Đọc artifact không được coi là reviewer đã chạy toàn bộ. Hồi quy/đối chứng source cũ của agent giữ riêng với đối chứng tiến trình của reviewer.

Markdown/link/control-character validator trước thêm review: **60 Markdown, 940 liên kết nội bộ, 0 lỗi**; PR diff check sạch, source allowlist đạt, không secret/key/.env trong thay đổi. Không chạy web/typecheck/build/browser/proxy vì web/proxy/Compose không đổi.

Sau cập nhật review: **61 Markdown, 952 liên kết nội bộ, 0 lỗi**; diff check của docs tracked và report mới sạch. Không có thay đổi API/web so HEAD do reviewer.

## Môi trường, cleanup và giới hạn

Conda `C:/Users/ADMIN/.conda/envs/ras/python.exe` và pip đã xác minh; không cài thư viện/venv. PostgreSQL standalone `ras-pr42-review-db-20261006`, labels `ras.review=pr42-20261006`/`ras.task=final-clock-review-20261006`, bind `127.0.0.1:58454`; runtime/recovery LOGIN thật, test admin URL/DB parts explicit trước fixture import, journal/credentials tổng hợp ngoài repo, external HTTPTransport bị chặn. Fixture nghiệp vụ chỉ scoped cutover; nhóm pending HTTP giữ gate thật.

Evidence ngoài repo: `%TEMP%/ras-pr42-review-20261006` gồm selected/counter JUnit/log, selection, source manifests, agent-evidence-check, summary và cleanup. Đã kiểm container ID/labels/mount/sole volume consumer, dọn container + anonymous volume đúng của review và credentials. Metadata ID/image/StartedAt của **24 container có trước task không đổi**. Không đọc/sửa `.env`, query/nâng/restart dev, gọi provider hoặc đổi pricing/safety.

Chưa chạy full inventory/business HTTP/cutover, file/upload/storage/stream/provider/worker/backup restore/drill/C3/C4. Latency/clock là mô phỏng sau SQL thật, không đo lỗi mạng/mất điện hoặc SLO. Response đã qua final boundary và nhả khóa vẫn có thể tiếp tục gửi; bytes đã gửi không thu hồi được. Các giới hạn này giữ nguyên sau merge.
