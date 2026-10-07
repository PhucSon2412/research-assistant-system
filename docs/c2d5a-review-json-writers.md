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

Code đóng băng `1666273eeef8ffb54da7f43d176d7cec1ce2a75f`. [Inventory](c2d5a-test-inventory.json)
giữ đầy đủ node ID collect/selected/not-run, từng JUnit SHA-256, unique/no
missing/no duplicates và SHA-256 source trước/sau **trùng nhau**.
Collect **3,468**; suite cuối **1,371/1,371 pass**, **0 fail/error/skip**,
**2,097 not-run**. Không cộng preparation, đối chứng hoặc lịch sử PR #45 (991/350/57).

| Nhóm suite cuối | Pass | Phạm vi |
| --- | ---: | --- |
| Writers/schema/proxy | 234 | Đủ 222 ca mới (219 writer + 2 migration + 1 Chrome proxy), 8 review runtime contract, 4 head/ORM/downgrade |
| GET/download/clock | 655 | 260 C2d-2b GET, 330 C2d-4c original, 65 final clock |
| Auth/journal | 479 | Foundation, C2a session/login, C2b reset/status, C2c2 invitation/restore/migration |
| Legacy closure migration | 3 | Pristine downgrade `0028`, matching/mismatch binding `0029` |

Web **58/58 pass**, 0 fail/skip; typecheck và build đạt với cùng web source.
Chrome **154.0.8037.98** headless thực hiện form qua **Next start → socket Uvicorn**:
checkbox ban đầu trống/submit disabled, submit 201, withdraw 200, reject 200
thin history; owner reject 403, expired session 401/clear-cookie/no-store.
Log/JUnit ở thư mục evidence ngoài repo, không paper/credentials trong PR.
PNG được tạo trong temp đã bị pytest retention của lượt sau dọn; không giữ
ảnh Chrome lâu dài hoặc tuyên bố có PNG trong artifact cuối.

**7/7 đối chứng guard** bắt đúng assertion failure, 0 setup errors; byte/hash
khôi phục trước suite: role, final clock, exact tuple, pending counter,
independent SQL time binding, fresh response proof, outcome qua teardown.
Đối chứng outcome kiểm contract state độc lập, không tuyên bố đã phát DTO trái quyền.
Preparation lỗi observer Engine, storage fixture, ready/open assertion, lock
barrier/timeout và Chrome label đã sửa; toàn bộ JUnit chuẩn bị vẫn giữ riêng
(kể cả lỗi thiếu C1 runtime password và các lượt fail). Docker init cũng từng
gặp readiness/default DB lỗi trước khi hoàn tất setup cô lập.
Lượt reads đầu bị dừng sau **30 setup error** do wrapper thiếu `RAS_STORAGE__*`
(đã có `MINIO_*`); constructor từ chối trước S3 client/network, không chạm dev.
Lượt dừng không có JUnit/không tính pass. Sửa chỉ wrapper ngoài repo, kiểm lại
6 download pass rồi chạy lại đủ 655 ca trong hai nhóm; source không đổi.
Lượt auth đầu có 478 pass/1 fail (thiếu identifier của fixture restore C2c2
cố định port/label cũ), giữ trong preparation và không cộng pass. Dựng thêm
PostgreSQL mới ở port trống `58442`, nhãn owner C2d5a + nhãn fixture yêu cầu;
chạy lại 478 auth và riêng pg_dump/pg_restore C2c2 **1 pass**, không fallback
vào container có sẵn hoặc sửa source. Credential values của evidence lỗi đã
redact trước ghi inventory.

Snapshot PostgreSQL request open/absent → runtime withdraw/reject → journal
copy riêng → hold/migrate/recover kiểm old review URL 404, replay idempotent,
không common/rights/receipt mới. Đây không phải diễn tập backup operator đầy đủ.
Các HTTP legacy `test_review_closure_journal` còn gắn setup/expectation replay
cũ không được tính pass; scope đó được kiểm lại bằng test runtime/fault/restore
mới và regression foundation/direct migration. Các suite business/provider
ngoài selection giữ not-run, không tuyên bố toàn repository xanh.

Cleanup chỉ ba container thử theo exact ID/name/label/image/port/mount và
sole consumer; anonymous volumes + credentials generated ngoài repo đã xóa.
**24 container có sẵn giữ nguyên ID/image/StartedAt** trước/sau. Logs/XML/node
IDs/scripts giữ ngoài repo tại `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5a-20261007`.
Journal/dump/PNG thử từng nằm trong pytest temp ngoài repo, có thể bị retention dọn.
Chi tiết cleanup/selection/fault và hash nằm trong inventory.
Conda `C:\Users\ADMIN\.conda\envs\ras` đã xác minh interpreter/pip; không
venv/cài mới, `.env`, provider/key, dev DB hoặc readiness/pricing thay đổi.

Fault injection Python/driver và snapshot PostgreSQL thử không chứng minh
mất điện, storage journal production độc lập hoặc durability/SLO. Full
business/provider/cutover/C3/C4 còn riêng. PR để review, không merge.
