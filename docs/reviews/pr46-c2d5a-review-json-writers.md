# Review PR #46 — C2d-5a ba JSON writer review

Review ngày 2026-10-07 trên head `793815326d07efd29f4d05b5e98ffea0d7d314b9`; code `1666273eeef8ffb54da7f43d176d7cec1ce2a75f`, base `070cb37aeb35da62c088293eaff9c55dd58f1929`. PR #46 **OPEN tại lúc review**, sau đó chủ dự án merge `8ff5b4e` ngày 2026-10-07; không rerun tại merge. [Task](../tasks/m6c2b2c2d5a-review-json-writers.md) · [Handoff](../c2d5a-review-json-writers.md) · [Inventory agent](../c2d5a-test-inventory.json).

## Kết luận

**Không phát hiện điểm chặn merge trong phạm vi C2d-5a; có thể merge PR #46.** Ba POST submit/withdraw/reject chạy qua runtime, closure journal hẹp và proof response mới. **Safety vẫn pending tới C4**; kết luận không mở readiness, chứng nhận approval/publication/provider hoặc toàn bộ HTTP nghiệp vụ.

## Source đã rà soát

- [Boundary writer](../../api/app/api/review_writes.py): User NO KEY UPDATE trước resource; submit dùng item SHARE, closure request UPDATE rồi item SHARE khi owner cần. Reject dùng workflow, không đòi grant đọc file còn hiệu lực. Flush DML/FK/audit trước journal exclusive; không nâng shared hoặc khóa User mới sau journal. Role/CSRF/session/head/receipt và clock kiểm lại sau SQL.
- Pending chỉ trừ đúng một closure đã stage từ open trong chính transaction, khớp request/item/version/file/requester/actor/status/reason/time. Các closure khác, unconfirmed mirror, hold, file ahead và cutover pending vẫn chặn.
- [Migration 0040](../../api/migrations/versions/0040_review_closure_runtime.sql) giữ migration cũ; marker do trigger tạo, không nhận marker tùy ý. Routine mirror kiểm shape/type/NULL/UUID/decision/time và exact binding trước append. Mirror false, constraints được force trước byte đầu; routine confirm chỉ chuyển false → true cho cùng line/request/transaction sau append/fsync. PUBLIC/recovery không được cấp hai routine; runtime không có raw journal DML/DDL/TEMP/role fallback. Upgrade DB đã provision và reprovision được kiểm.
- Outcome thuộc operation, giữ intent và commit flags qua rollback/close lỗi. Append/fsync lỗi đi nhánh commit closure + mirror false, trả lỗi và đóng gate; không success DTO. Lỗi confirm/actor/commit không bị che bằng nhánh này. Commit exception luôn uncertain, không replay để nhận business success, SELECT suy commit hoặc retry POST.
- Known commit lấy proof/DTO từ transaction mới. Chủ request đọc lịch sử; Admin reject chỉ thin history, không title/file/declaration/email/note riêng. Clock/CSRF/current role được kiểm sau DTO SQL. Teardown rollback/close/invalidate không xóa outcome hoặc che lỗi chính.
- [Routes](../../api/app/api/routes/reviews.py) giữ Origin/session/CSRF thật; fixtures original bỏ quyền đặc biệt cho withdraw/reject. Approve/publish/revoke legacy chỉ còn dùng đặc quyền để chuẩn bị resource thử, chưa được chứng nhận runtime. Không chuyển production auth/recovery/approval/provider ngoài scope.

## Kiểm độc lập

| Reviewer thực chạy | Kết quả |
| --- | --- |
| Toàn `test_c2d5a_review_writes.py` | **219/219 pass** |
| Hai test migration mới, gồm upgrade populated/provisioned và pristine roundtrip | **2/2 pass** |
| `test_review_requests.py` | **8/8 pass** |
| Head, noop, pristine downgrade/upgrade và ORM/schema | **4/4 pass** |
| Selection API chính | **233/233**, 0 fail/error/skip; 604,999 s; 3 warning hiện có |
| Chrome qua socket Uvicorn và Next JSON proxy thật | **1/1 pass**, 7,639 s; 1 warning Starlette |
| Tổng API reviewer, unique JUnit | **234/234** |
| Web `npm test --prefix web` | **58/58 pass**, 0 fail/skip |
| Web typecheck/build | Đạt / đạt |

Đã chạy actor/role/binding/head/receipt/CSRF mất hiệu lực; idle/absolute ở đúng biên và sau SQL/lock wait/fsync; exact tuple và cùng bytes khác owner; pending HTTP không patch; raw privilege denial; SQL NULL/missing/type/time/decision; lỗi append none/torn/full-fsync, confirm, commit rollback hoặc landed-uncertain, proof sau commit và teardown. Các race tất định hai thứ tự gồm submit/closure, hai closers, auth revocation, GET history/original và legacy approval (chỉ setup có đặc quyền).

Bốn ca snapshot open/absent → runtime withdraw/reject → bản sao journal hiện hành → hold/migrate/recover xác nhận grant không sống lại, URL original cũ 404, idempotence, không dựng common/rights. Đây là PostgreSQL snapshot thử, không diễn tập backup operator đầy đủ.

Chrome cài sẵn chạy headless kiểm checkbox ban đầu rỗng/nút submit disabled, submit 201 → withdraw 200 → submit mới → reject 200 thin history; consent reset, owner reject 403 và expired cookie 401/Set-Cookie/no-store qua proxy. Đây là reviewer chạy lại test browser, không chỉ đối chiếu artifact. Không gọi provider.

234 node là tập con đúng nhóm writers 234 trong inventory agent; 273 API Python/SQL/web source hash trước/sau mỗi lượt không đổi. Không sửa source trong khi chạy suite. JUnit SHA-256:

- Selection chính: `f71d3d44af9d11c31e26bb23b3e599f9198d27ad3889ad566f80984082d407c9`.
- Chrome: `6ea58f5eac5d7a1b83d7b70b115c8823c5e3d42111f589cbb085872768c4d5a3`.

## Đối chiếu evidence agent

Đã xác minh 6 JUnit artifact với checksum, **1.371 unique node**, 0 fail/error/skip, đúng selection; collect 3.468 và not-run 2.097 phân hoạch đầy đủ sau đúng hai alias UUID v1 ở tham số foundation. **272 hash source agent khớp hiện tại**, hash trước/sau giống nhau; commit sau code chỉ đổi docs. Đây không phải reviewer chạy lại 1.371.

Đối chiếu **7/7 mutant artifact**: mỗi ca một assertion failure (gồm expectation không ném lỗi), không setup error/skip; ba file sửa tạm khôi phục đúng byte hash. Reviewer không chạy lại mutation. Ca outcome chứng minh flags không bị teardown xóa, không tự chứng minh có DTO trái quyền. Reviewer còn **1.137 node selection agent** chưa chạy lại; **3.234 node inventory hiện hành** chưa chạy độc lập. Không cộng số lịch sử PR #45 hoặc lượt preparation vào pass.

## Docs, cleanup và giới hạn

Reviewer tạo report này, liên kết từ README/docs/handoff/testing/decisions/AGENTS; làm rõ head `0039` và trạng thái “đã giao, chưa kiểm” là mốc lịch sử trước PR #46 thêm `0040`. Không đổi code, migration hoặc inventory của agent. Report và cập nhật docs chưa commit để coding agent giữ lại; reviewer không push/comment/merge. Chủ dự án đã merge PR sau review.

Evidence reviewer ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr46-review-20261007` (scripts, XML, logs, hash checks và cleanup). Wrapper kiểm thử đặt credentials thử tường minh trước import fixture, chặn external HTTP/storage; dùng Conda `ras`, không venv/cài mới hoặc fallback `.env`. Hai container/anonymous volumes thử đã được đối chiếu exact ID/name/label/image/port/mount/sole consumer rồi xóa; credentials generated đã xóa. **24 container có sẵn giữ nguyên ID/image/StartedAt.** Journal/evidence thử giữ ngoài repo. Không query/migrate/restart dev, gọi OpenRouter, đổi pricing/readiness.

Tooling review từng dùng giả định log UTF-8 và message mutant bắt đầu bằng `assert`; log PowerShell có BOM UTF-16 và một số expectation là `DID NOT RAISE`. Đã đối chiếu XML trực tiếp và sửa script kiểm artifact; các lỗi tooling không là API failure hoặc pass mới. Không có fail/error/skip trong hai lượt pytest reviewer.

Approval/copy/publication/index, provider callers, C3/C4 và HTTP closure legacy ngoài selection chưa được chứng nhận. Không full repository/cutover/production benchmark, crash process hoặc mất điện thật; fault injection và journal cùng máy không chứng minh storage độc lập/durability. Không suy revision DB dev từ head source `0040`.
