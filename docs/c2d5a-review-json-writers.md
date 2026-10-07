# C2d-5a: ba JSON writer review qua runtime

[Task](tasks/m6c2b2c2d5a-review-json-writers.md) · [Caller map](c2d1-caller-inventory.json)

Base fetch/xác minh `070cb37aeb35da62c088293eaff9c55dd58f1929`: PR #45 MERGED,
main chứa head `b23ff82` và code `4c49919`. Nhánh
`feat/m6c2b2c2d5a-review-json-writers` giữ toàn bộ docs orchestrator chưa commit.
Head Alembic source trước sửa được kiểm bằng ScriptDirectory: `0039`; thêm
`0040`, không sửa migration cũ và không truy vấn revision DB dev.
**Safety pending tới C4; approval/copy, publication/index, provider và C3/C4 riêng.**

## Boundary và quyền

Ba route gọi riêng `api/review_writes.write`. Entry giữ session/Origin/CSRF thật,
`require_writer` trước row locks. `_current` dùng scalar `_actor`: exact
session/User, active/proven/usable, latest confirmed head/receipt, generation,
raw CSRF và role hiện tại. Clock lấy sau chính predicate SQL; helper không touch
idle. Dependency activity hiện hành vẫn chạy.

| POST | Row locks/DML trước journal exclusive | Proof response sau known commit |
| --- | --- | --- |
| `/review-requests` | User NO KEY UPDATE → exact private owner item SHARE → INSERT request/audit, flush FK | request SHARE → owner item SHARE → journal shared → DTO đọc mới/current User/CSRF/clock |
| `/review-requests/{id}/withdraw` | User NO KEY UPDATE → owned request UPDATE → private owner item SHARE → closure/audit, flush | request SHARE → owner item SHARE → journal shared → history/current caller/CSRF/clock |
| `/admin/review-requests/{id}/reject` | Admin User NO KEY UPDATE → workflow request UPDATE → closure/audit, flush; không khóa private item | request SHARE → journal shared → thin history/current Admin/CSRF/clock |

Submit kiểm đúng item/version/file/checksum/epoch, acknowledgement và notice
version hiện hành, luôn `verified=false`; không tạo rights/common/job/provider.
Savepoint duplicate INSERT giữ caller/item locks để kiểm caller/gate/clock trước
`409 review_request_open`; không khóa request cũ sau item. Missing/foreign/
malformed/closed/notice denial cũng qua final boundary. Withdraw vẫn cho owner
đã đổi role rút; reject không cần grant đọc file còn sống. Admin response không
chứa title/email/declaration/file/URL/note riêng. Closed note không nói đang chờ duyệt.

## Migration/routine và pending closure

`0040` thêm `review_requests.closure_transaction_id` nullable. BEFORE trigger
`ras_review_closure_transaction` chỉ mint marker khi **open → closed**, bằng
`txid_current`; không nhận marker tùy ý lúc INSERT hoặc đổi marker sau closure.
Hàng lịch sử giữ NULL. Đây là binding transaction live, không receipt xuyên
restore và không chứng minh POST thành công.

`Closure` frozen giữ exact request/item/version/file/requester/actor/status/
reason/time do operation vừa khóa open rồi stage. `_gate` lấy journal exclusive
trực tiếp sau flush; chỉ trừ **một** closure chưa mirror nếu toàn bộ tuple và
marker khớp chính transaction. Closure khác, unconfirmed, hold, file ahead/
out-of-sync và pending cutover vẫn đóng. Không nâng shared hoặc khóa hàng mới
sau journal; counter gate read/auth thông thường không thay đổi.

`ras_auth_review_closure_mirror(text)` là SECURITY DEFINER NOLOGIN, fixed
`search_path=pg_catalog,public,pg_temp`, chỉ nhận withdraw/reject. SQL độc lập
kiểm exact envelope/body fields/types/UUID/status–reason/time và exact request
đang đóng trong transaction; derive `review_request_id` từ line, mirror false.
Force constraints trước append. `ras_auth_review_closure_confirm(text)` chỉ
false → true cho cùng line/request/transaction. Python chỉ gọi sau append và
fsync trả về. Routine không ghi file hoặc tin flag/GUC fsync của caller.

Provision mới và upgrade roles đã provision đều cấp đúng hai EXECUTE cho
runtime, không PUBLIC/recovery và không raw journal DML/DDL/TEMP/membership.
Recovery tiếp tục đường legacy có kiểm binding, nhận request absent, replay
`closed_by_recovery` không actor/note/rights/common/receipt. `hold` không đọc
marker mới trên snapshot chưa migrate. Approval giữ helper legacy; không
chứng nhận approval runtime-ready.

Các lựa chọn marker/helper/routine này triển khai quyết định scoped của
orchestrator theo ủy quyền trong task; không là luật kiến trúc vĩnh viễn hay
quyết định trực tiếp mới của chủ dự án.

## Journal, outcome và lỗi

`Outcome` do operation sở hữu, độc lập Session teardown: closure/record,
append_started/written, commit_started/committed và teardown_failed không bị
rollback/close xóa. Không suy outcome từ SELECT/replay, không retry POST và API
không dùng credentials recovery/migrator. `_release` thử rollback + close;
fault thì invalidate, giữ mã lỗi chính và không phát DTO khi teardown lỗi.

| Cửa sổ | Kết quả |
| --- | --- |
| Known precommit fail trước intent | rollback business/audit, 401/403/404/409 hoặc `503 review_write_unavailable` |
| Append none/torn/full-fsync-error, known closure commit | mirror false, grant kết thúc, gate đóng, `503 review_journal_unavailable` |
| Durable intent rồi actor/confirm/DB fail trước commit | không DTO; rollback có thể để file ahead; recovery có thể đóng grant; không hứa request vẫn mở/chưa lưu |
| Commit exception, remote landed hoặc rollback | `503 review_write_outcome_uncertain`, không success/replay/retry |
| Known commit, proof mới/DB/teardown thất bại | auth/resource error hoặc `503 review_write_response_unavailable`; dữ liệu có thể đã lưu |
| Known commit + proof response mới đạt | submit 201, withdraw/reject 200, trạng thái đọc mới |

Nhánh fail-closed chỉ bắt lỗi append/fsync, sau khi actor/resource hợp lệ;
không dùng normal gate để vô tình rollback mirror false. Lỗi confirm/actor/
commit không được che thành journal-write-failed. Durable intent không chứng
nhận mutation/audit của actor đã commit. Runtime không tự replay; CLI recover
đối soát khi gate đóng. Web mã uncertain yêu cầu kiểm tra trạng thái, không tự
gửi lại. Response submit có thể đọc request đã closed sau commit, không ép open.

## Kiểm chứng và giới hạn

Đang kiểm trên PostgreSQL/MinIO/journal cô lập; kết quả cuối và inventory sẽ
ghi riêng sau khi đóng băng source. Không cộng lịch sử PR #45 (991/350/57).
Conda `C:\Users\ADMIN\.conda\envs\ras` đã xác minh interpreter/pip; không
venv/cài mới, `.env`, provider/key, dev DB hoặc readiness/pricing thay đổi.

Fault injection Python/driver và snapshot PostgreSQL thử không chứng minh
mất điện, storage journal production độc lập hoặc durability/SLO. Full
business/provider/cutover/C3/C4 còn riêng. PR để review, không merge.
