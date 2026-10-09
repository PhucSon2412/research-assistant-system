# Review PR #49 — C2d-5c2 common revoke

**Cập nhật sau review (2026-10-09): PR đã merge `859dfe9793dd9549e3ca9b8f0102361f414d0340`; tree giống head đã review.** Không rerun tại merge, số bên dưới vẫn theo source review. [Task C2d-5d1 tiếp theo](../tasks/m6c2b2c2d5d1-common-index-request.md) chỉ chuyển HTTP tạo/trả job index; worker/provider và C3/C4 riêng, safety pending.

Ngày **2026-10-09**. PR **OPEN, chưa merge** tại lúc review. Base `1eede17c1eed82fa7a00f7018fe01b4362ce30a9` (merge PR #48); head `f8dc44ce5e146e5c867534f51a7e866769c1bc6b`. Agent chạy 528 ca tại `964b9f6` và một Chrome bổ sung tại `30cb729`; production/shared fixtures không đổi giữa hai lượt. Source migration **0042**, không suy revision DB dev. Working tree sạch trước lượt reviewer.

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi POST revoke bản chung.** Kết luận áp cho `POST /admin/common-copies/{item_id}/revoke`; **safety vẫn pending tới C4**. Common-index/worker/provider và C3/C4 còn riêng. Reviewer chỉ bổ sung tài liệu, không sửa ứng dụng/migration, commit/push/merge hoặc đăng comment PR.

[Task](../tasks/m6c2b2c2d5c2-common-revoke.md), [handoff](../c2d5c2-common-revoke.md) và [inventory agent](../c2d5c2-test-inventory.json) là nguồn phạm vi/evidence. Kết quả thực chạy dưới đây tách khỏi artifact của agent.

## Đối chiếu code và hợp đồng

- Acceptance kiểm current Admin/session/head/receipt/generation/status/CSRF/clock; khóa User → common item → rights/publication/jobs/version/file → flush DML/FK/retention/audit → journal exclusive trực tiếp. Không nâng shared lock; không giữ/khóa tombstone cũ sau item. Revoke không đòi eligibility publish/index, không I/O storage hoặc gọi provider.
- Revision `0042` mint prior state/epoch/transaction tại trigger. Routine mirror kiểm shape/type/NULL, hash/chain và binding đúng tombstone/item/version/file/rights/actor/time/epoch/retention/audit trong transaction hiện tại. Mirror false và constraints được kiểm trước byte đầu. Confirm đúng line và transaction chỉ sau append/fsync thành công; generic `ras_auth_confirm(seq)` không xác nhận domain revocation. Runtime không được raw write journal/auth hoặc thêm DDL/TEMP; routine fixed search path và owner NOLOGIN. Savepoint bị từ chối theo hợp đồng xmin.
- Caller/binding/clock kiểm lại sau SQL mirror ngay trước append. Lỗi chính append/fsync giữ restriction + mirror false trong known commit rồi trả 503, kể cả TTL qua biên trong lúc lỗi; gate vẫn đóng. Đây là ngoại lệ restriction-only scoped, không áp cho lỗi SQL/confirm hoặc quyền ghi mới. Append thành công phải kiểm lại caller/gate/clock trước commit.
- Outcome độc lập rollback/close/invalidate. Commit ném lỗi luôn uncertain, không SELECT để suy success, replay HTTP, tự retry hoặc trả DTO. Known commit + lỗi release/response giữ dữ liệu đã lưu. Lỗi journal sau known false commit giữ ưu tiên, không bị teardown thay thành success hoặc rollback.
- Response dùng transaction/proof mới, item/resources SHARE → journal shared → current actor/clock sau formatter SQL cuối; DTO phản ánh cleanup đã thắng, còn cancelled_jobs là số của attempt. Session ngắn release sạch, invalidate khi cần; cancellation giữ executor/transaction đến thread settle, không gửi DTO khi đã ngắt.
- Epoch, trạng thái revoked/metadata_only, active generation NULL, rights/publication, job cancellation, tombstone/retention/audit/mirror cùng transaction. Request nội dung mới bị chặn trước cleanup; private và bản khác cùng checksum không bị ảnh hưởng. Response đã qua boundary trước t_rev có thể gửi tiếp; bytes/provider payload đã gửi không lấy lại được.
- UI reason trống/unticked, ref guard chống hai submit đồng bộ, disable khi busy, bỏ tick sau lỗi. Mất response/504 hiển thị unknown, không tự POST lại. Revoke giữ proxy timeout normal 15 giây; không lấy timeout publish hoặc SLO làm cam kết mới.

## Reviewer thực chạy tại head review

| Nhóm | Kết quả mới |
| --- | ---: |
| Core: 170 revoke + 2 migration mới + 28 migration/ORM + Chrome revoke | 201/201 |
| Publish/revoke và cleanup có tác động trực tiếp | 11/11 |
| Legacy publication/recovery/cleanup | 34/34 |
| Common index/search/RAG + download/original | 24/24 |
| Auth foundation/login có tác động privilege/fsync | 24/24 |
| Chrome publish dùng runtime revoke đã đổi helper | 1/1 |
| **Tổng API unique, không cộng lượt lặp** | **295/295** |
| Web tests | **66/66** |
| Web typecheck/build | Đạt / đạt |

API **0 fail/error/skip**, selection khớp JUnit, 290 source hashes trước/sau khớp current source; không sửa source khi chạy. Thời gian cộng testcase 1133.790 s chỉ là phép thử fixture, không benchmark. Trong inventory 4.096 node, reviewer **không chạy lại 3.801 node**; không tuyên bố toàn suite đã pass.

PostgreSQL/MinIO/journal/runtime roles thật và mới; business cutover chỉ fixture scoped, pending HTTP riêng giữ production gate thật. Hai Chrome qua Next production proxy và Uvicorn socket source hiện tại, không phải API Compose image. Ca Chrome revoke kiểm busy/double submit, old original 404 và known commit rồi proxy 504/UI unknown/no repost; Chrome publish kiểm helper runtime revoke tương thích.

Snapshot/replay trong selection dùng PostgreSQL TEMPLATE cô lập và journal hiện hành; không phải diễn tập backup/restore Compose mới. Race dùng row/advisory lock wait và barrier; worker/cleanup chỉ kiểm tương thích hiện hành, không chứng nhận chuyển sang runtime boundary mới.

## Artifact agent kiểm riêng

Đã kiểm SHA-256 của log/JUnit/selection, union đủ **529 unique pass** (528 ở `964b9f6` + 1 ở `30cb729`), không thiếu/trùng; collect **4.096**, not-run **3.567** khớp partition. 295 source hashes trước/sau trong mỗi lượt khớp; giữa hai lượt chỉ thay test Chrome publish, 294 file khác gồm production/shared fixtures giữ nguyên. Sau `30cb729` chỉ docs thay đổi.

Tám mutant cuối còn XML/log đúng hash và assertion failure, không setup error; node và restored source hash khớp. Reviewer kiểm artifact, **không chạy lại tám mutant hoặc toàn 529 ca**, không cộng số của agent vào 295 ca của mình. Web 66/typecheck/build và runner/baseline artifacts cũng đã đối chiếu hash.

**Giới hạn evidence được giữ nguyên:** raw XML/log của lượt lock observer yếu ban đầu đã bị rerun ghi đè; chỉ summary/hash gốc còn. Không thể kiểm lại raw assertion của lượt đó và không tính nó là mutant bắt lỗi. Tám lượt cuối có artifact riêng. Snapshot container đầu của agent không có StartedAt: chỉ chứng minh ID/image từ đầu, StartedAt từ inspect sau đó qua cleanup, không suy ngược.

## Evidence, cleanup và phần chưa kiểm

Reviewer evidence ngoài repo tại `%TEMP%\ras-pr49-review-20261009`: setup/runner/command/selection/JUnit/source hashes, web logs, verifier/results, cleanup và quality. Agent evidence theo inventory tại `%TEMP%\ras-c2d5c2-20261009`. Lượt chuẩn bị reviewer có selection prefix `api/` không khớp pytest root `tests/`; dừng ở collection, sửa file selection ngoài repo rồi chạy lượt cuối. Giữ log chuẩn bị riêng, không đổi app hoặc tính là assertion failure của sản phẩm.

Python đã kiểm trỏ Conda `C:\Users\ADMIN\.conda\envs\ras` (3.12.14); không tạo venv. HTTP ngoài bị chặn, **0 provider call**. Đã quét fingerprint credentials sinh cho reviewer, không có trong evidence. Sau kiểm exact ID/name/label/image/port/mount/sole consumer, xóa **2 container/2 volume riêng** và ba file credentials; **9 container có sẵn giữ nguyên ID/image/StartedAt**. Không đọc/sửa `.env`, query/migrate/restart dev hoặc đổi pricing/gate.

Không kiểm full business/cutover/C3/C4, API Compose deployment/image, provider live/charge, crash thật/mất điện, độ bền thiết bị hoặc nơi lưu journal production. Journal cùng mất/rollback với DB vẫn không phát hiện được. Không benchmark/SLO. PR vẫn mở để chủ dự án quyết định merge; bước sau theo roadmap là common-index/worker boundary.
