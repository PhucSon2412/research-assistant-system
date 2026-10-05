# Review PR #37: C2c-2 invitation runtime

## Trạng thái sau review

Chủ dự án báo đã merge PR #37. Ngày 2026-10-05, orchestrator fetch xác minh merge `ca4a623ea9cd4d2f8454659a1d66c48dbf8093d0`, chứa `9f26535`, head mã nguồn `0039`. Kết luận và các số đo review ngày 2026-10-04 bên dưới giữ nguyên lịch sử; câu PR chưa merge mô tả thời điểm reviewer chạy, không là trạng thái hiện tại. File review này cùng các cập nhật review docs còn ở working tree sau merge; giao agent tiếp giữ và đưa vào PR. [Task tiếp C2d-1](../tasks/m6c2b2c2d1-final-caller-reads.md) nay đã triển khai trên nhánh review ([bàn giao](../c2d1-final-caller-reads.md)); safety pending tới C4.

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2c2-invitation-lifecycle.md) · [Bàn giao](../c2c2-invitation-runtime.md) · [Inventory agent](../c2c2-test-inventory.json)

## Kết luận

Ngày 2026-10-04, review source `9f2653545744603dcea4c9d993d4a3be1baecaef`, chứa code `2b69f85` và sửa recovery `b8a0967`, base merge PR #36 `d1b53e6`, head source `0039`. **Không phát hiện điểm chặn merge trong phạm vi C2c-2; đề nghị có thể merge PR #37.** PR vẫn chưa được reviewer merge/push/comment. Safety pending tới C4; kết luận này không mở readiness hoặc hoàn tất C2d/C3/C4.

Reviewer chạy **198 test chọn pass**, rồi riêng **1 ORM/schema test pass**, không failure/error/skip. Các lượt này độc lập với số **800/800** của coding agent; không cộng thành full suite hay chứng cứ web/production. Phạm vi chưa chạy của agent là 756 API node, không đổi thành pass từ kết quả review.

## Phạm vi đã đọc và kiểm

- SQL `0039`, ORM transaction markers, provisioning/grants cho runtime/recovery/definer; giữ revision đã merge `0033`–`0038`.
- Writer create/reinvite/list/inspect/accept/revoke, binding delivery/bù, Origin/CSRF/actor/clock, helper batch `commit_auth` và thay đổi SQL log redaction.
- Pair trigger buộc đúng User role user, credential/status receipt `invitation_accept`, accepted closure/time/transaction/audit, thứ tự ba event; ép constraints trước append.
- Băm ngoài transaction; email/invitation/User/journal lock order; kiểm actor/TTL sau append trước commit; hậu kiểm exact operations/receipts sau known commit.
- Bù chỉ hạn chế link theo issuance binding; actor mất quyền không bỏ bù, accept hoặc closure khác thắng được giữ; adapter chạy ngoài transaction/lock. Lỗi response không chứng minh chưa gửi message.
- Recovery hạn chế, marker defer tương thích snapshot cũ, grants migration trên DB đã provision, prefix/fsync/rollback/unknown commit và User absent. Journal không dựng lại account/verifier/receipt.

## Kiểm độc lập thực chạy

| Lượt | Phạm vi | Kết quả |
| --- | --- | --- |
| `review.xml` | `test_c2c2_invitations.py` 151 + `test_c2c2_migration.py` 2 + `test_c2c2_restore.py` 1 + HTTP `test_invitations.py` 20 + `test_schema_constraints.py` 24 | **198 pass**, 1 cảnh báo Starlette hiện có; **324,277 s** |
| `orm.xml` | `test_migrations.py::test_orm_models_match_migrated_schema` | **1 pass**, 2 cảnh báo computed defaults hiện có; **2,008 s** |

Các ca có routine gọi trực tiếp bằng runtime LOGIN, raw writes/grants bị chặn, NULL/sai binding/pair/audit/role/history, actor thay đổi sau dependency, clock sau hash/lock wait/append, race accept/reinvite/revoke/bù và adapter không giữ khóa. Fault matrix dùng lỗi mô phỏng; restore dùng `pg_dump`/`pg_restore` thật trước accept, journal hiện hành sao riêng, restore/recover hai lần; không dựng account và không nhận lại link đã đóng. ORM/schema so sánh đạt.

Không sửa source application hoặc thực hiện mutant trong review này. Runner kiểm SHA-256 Python/SQL API trước/sau mỗi lượt; không thay đổi trong lúc test. Các đối chứng 5/5 trong inventory là phép thử của coding agent, reviewer không chạy lại và không tính thành kết quả riêng.

**Điều kiện đo:** Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe` đã xác minh. Container standalone `ras-pr37-review-db-20261004`, image `pgvector/pgvector:pg16`, bind `127.0.0.1:58442`; labels `ras.review=pr37-20261004` và `ras.task=c2c2-20261004`. Dữ liệu/journal/mật khẩu tổng hợp, runtime/recovery LOGIN và NOLOGIN definer thật. Explicit admin URL và DB parts đặt trước pytest; không fallback `.env`. HTTPTransport sync/async bị chặn. Fixture nghiệp vụ chỉ thay pending cutover; integrity, receipt/head, SQL, actor, clock, journal và commit giữ thật. Không API/web/worker/beat/MinIO dev hoặc provider.

Runner, JUnit/log/source manifest ở `%TEMP%/ras-pr37-review-20261004`, ngoài repo. Lượt chuẩn bị đầu truyền sai cú pháp JUnit từ PowerShell, **0 test chạy**; sửa đối số rồi mới có hai lượt trên, không là failure application. Không cài dependency hay tạo venv.

Sau kiểm đúng tên/labels/image/port/mount của container thử, đã xóa container và volume anonymous bằng `docker rm -f -v`; credentials JSON và env dùng một lần đã xóa. Evidence tổng hợp còn ngoài repo. Stack dev không migrate/restart/truy vấn hoặc bị cleanup.

## Giới hạn và bước tiếp

- Chưa chạy full API inventory, web/typecheck/build/browser/proxy, object-store/full backup artifact hoặc cutover. Các caller resource còn lại thuộc C2d; không xem principal đầu request là kiểm quyền cuối hoàn chỉnh.
- Tương thích closure snapshot cũ nằm trong code và chứng cứ agent; review chọn hai test migration `0039` và dump/restore hiện hành, không chạy lại toàn bộ các migration lịch sử.
- Nội dung message đã gửi và response đã truyền không lấy lại được; intent journal không chứng minh account/delivery commit. Prefix thiếu invitation closure không tự chứng minh link đóng.
- Journal production độc lập hiện hành, operator/delivery/recovery C3, cutover/full drill C4 và độ bền mất điện chưa được xác nhận. Thời gian test trên fixture không là SLO/RPO/RTO hoặc latency production.

Sau merge, task tiếp theo là **C2d final callers/integration**, dựa trên danh sách caller ở [bàn giao C2c-2](../c2c2-invitation-runtime.md#kiểm-chứng-và-bàn-giao). Orchestrator viết prompt riêng sau khi xác minh merge; không mở pending hoặc triển khai C3/C4 trong PR #37.
