# C3: operator khôi phục credential và initial status

[Mục lục](README.md) · [Task](tasks/m6c2b2c3-operator-auth-recovery.md) · [Thiết kế auth sau restore](auth-restore-safety.md) · [Inventory](c3-test-inventory.json) · [Kế hoạch MVP](mvp-delivery-plan.md)

**Trạng thái:** đã sửa hai P2 review PR #59 (xem mục dưới), chờ review lại; nhánh review `feat/m6c2b2c3-operator-auth-recovery`, chưa merge. Base `origin/main` = `58d059ab7a383d5f97e1b04e909e9628a11bf78a` (merge PR #58; head review `aa9c8a2` là ancestor, tree khớp). Source migration `0048 → 0049`. Không suy revision DB dev. **Safety vẫn pending tới C4**; C3 không mở `cutover`, readiness hay đường đi vòng nào. Mọi kiểm chứng dùng hồ sơ/tài khoản/login **tổng hợp** trên project cô lập; chưa có danh tính, inventory User UUID hay ủy quyền người thật.

## Sửa review PR #59 (2026-10-12)

[Review tại `8fa504f`](reviews/pr59-c3-operator-auth-recovery.md) có hai P2; sửa trên cùng PR theo [prompt](tasks/fix-pr59-c3-final-authority-and-release.md), chỉ đổi `api/app/operator_recovery.py` (không migration/grant), head kiểm `0ee17f3`. Chờ review lại, chưa merge; safety pending tới C4.

- **Final authority sau mọi kết quả append:** login operator và cửa sổ case được kiểm lại bằng clock mới **sau** append dù ghi đủ, vắng, torn, prefix hay `fsync` lỗi, **trước** COMMIT. Mất quyền/hết hạn → rollback mutation/receipt, exit 4 `rolled_back_after_journal` (`committed=False`); file có thể đã giữ dòng nên replay chỉ hạn chế (credential quarantine/status held) và case đã tiêu nếu dòng bền. Append lỗi nhưng quyền/case còn hợp lệ vẫn commit cả batch false (exit 5) như hợp đồng cũ.
- **Release không còn bị nuốt:** helper `connection()` để lỗi của thân pha thắng; thân sạch nhưng rollback/close lỗi (kể cả khi invalidate/close dự phòng thành công) ném `connection_release_failed`. Prepare dừng trước TTY/hash/mutation (exit 3, không ghi gì); mutation phân loại theo `Attempt`: known commit → exit 4 `connection_release_failed` (hoặc exit 5 `auth_journal_unavailable` nếu batch chưa confirm), commit đã gửi chưa rõ → `auth_outcome_uncertain`, đã bắt đầu append nhưng chưa commit → `rolled_back_after_journal`; không chạy proof, không báo thành công, không submit lại. Proof sau known commit release lỗi → exit 4 `postcommit_proof_failed`. Connection luôn được trả/invalidate.

## Trust boundary

- **Tin cậy người:** recovery operator là chủ dự án hoặc người được chủ dự án chỉ định trực tiếp; cùng một người có thể duyệt hồ sơ và chạy CLI (MVP, không phê duyệt nhiều tầng). Người provision login/case bằng kết nối migrator cũng được tin cậy. Họ chịu trách nhiệm xác minh **ngoài app** người nhận đúng User UUID (qua kênh riêng) và giữ hồ sơ chứng cứ hạn chế ngoài journal/repo/backup.
- **Guard phần mềm:** login DB thật (`session_user`) phải ánh xạ tới operator UUID đang hoạt động; login không có membership hai chiều, không super/createrole/createdb/bypassrls/replication, không sở hữu object, không có quyền đọc/ghi bảng auth (loại runtime/recovery/owner); case đã đăng ký phải khớp file từng trường; case dùng một lần theo **journal hiện hành**; maintenance hold đã đối chiếu sạch; credential và status là hai quyết định riêng. Host/DB access, email, cookie cũ, mật khẩu snapshot hay UUID tự khai **không** là chứng cứ. Không chống owner/DBA/host độc hại hoặc người provision bị chiếm quyền; journal và DB cùng mất/rollback có thể không phát hiện.

## Schema, routine và grants (`0049`)

| Thành phần | Hợp đồng |
| --- | --- |
| `auth_recovery_operators` | `operator_id` ↔ `login_role` duy nhất; chỉ migrator ghi; chỉ được thu hồi một lần (`revoked_at`), không xóa/đổi binding |
| `auth_recovery_cases` | case UUIDv4, action `set_credential`/`resolve_initial_status`, operator, approver, `journal_id` installation, User UUID, role, expected credential/status head (NULL là khai báo, so NULL-safe), mã phương thức, `approved_at`/`expires_at`; bất biến, chỉ migrator ghi |
| Kind journal `operator_recovery_case` | body đúng `case_id, operator_id, user_id, action, operation_id, recorded_at`; parser Python và trigger SQL độc lập; **một `case_id` chỉ xuất hiện một lần trong chain**; cột typed `case_*` (unique `case_id`, `case_operation_id`). Bản ghi live (`applied_via='close'`) chỉ hợp lệ khi `session_user` là operator của case đúng installation/cửa sổ; replay ghi `replay` |
| Guard receipt | Receipt reason `verified_recovery`/`verified_initial_status` cần mirror case **live, chưa confirm, cùng transaction (xmin)** của operator đúng login/action/operation/time. Runtime, recovery và owner không còn đúc được hai reason này qua `ras_auth_change` generic; receipt lịch sử giữ nguyên |
| Reason closure mới | `operator_recovery` cho session và reset token (enum, CHECK bảng, CHECK mirror, parser) |
| Routines SECURITY DEFINER (fixed search path, owner definer, PUBLIC revoke) | `ras_auth_operator_whoami/gate/inspect/case/snapshot/stage/confirm/outcome` + helper `assert` không cấp; chỉ provisioning grant cho login operator |
| Provisioning | `app.cli.provision_auth_operator operator|register-case|revoke-operator` qua kết nối migrator; operator chỉ có `USAGE` schema + EXECUTE các routine trên, không SELECT bảng, không TEMP/CREATE |

`provision()` tái chạy giữ grant operator, chuyển owner routine mới cho definer và từ chối nếu runtime/recovery bị ánh xạ làm operator. Downgrade `0049` chỉ cho DB pristine; installation có registration/User/journal/receipt/operator/case thì từ chối. Cột `case_*` khai báo trong `Table` nhưng loại khỏi mapper ORM để code hiện hành không chèn NULL vào snapshot chưa migrate; mirror case ghi bằng SQL tường minh (`recovery.insert_case_mirror`).

## Luồng CLI

Lệnh: `python -m app.cli.auth_recovery inspect --user-id U [--case-id C]`, `set-credential …`, `resolve-initial-status …` với `--user-id --case-id --case-file --expected-installation`. Không có `--password`, `--email`, `--enable`, `--role`, `--force`, `--confirm-db-is-current`; parser không echo giá trị. Exit: 0 thành công đã chứng minh, 2 input/TTY, 3 từ chối (không ghi gì), 4 không thành công và journal/DB có thể giữ lần thử, 5 đã commit nhưng batch journal chưa confirm.

1. **Chuẩn bị (transaction ngắn, rồi trả connection):** whoami = operator của case; journal shared → maintenance hold; installation; case chưa dùng trong journal hiện hành; case đăng ký khớp file; cửa sổ thời gian bằng clock mới và trần `auth_recovery.case_max_lifetime`; snapshot đúng User UUID (vắng → `user_absent`), role, head journal = projection DB = case.
2. **Nhập/băm ngoài DB:** TTY thật hai lần không echo (`getpass`, `GetPassWarning` → lỗi, stdin không TTY bị từ chối trước khi chạm DB); policy độ dài/blank/email (so digest, CLI không đọc email); từ chối mật khẩu trùng verifier snapshot (không tuyên bố kiểm được lịch sử đã mất); Argon2 theo policy DB. Engine CLI `NullPool`: không còn session DB nào trong lúc nhập/băm. Routine chỉ trả verifier snapshot cho case `set_credential` còn hiệu lực của chính operator; nó chỉ nằm trong bộ nhớ CLI để so, không in/log (giới hạn: login operator đọc được PHC của đúng User đang được khôi phục).
3. **Mutation:** User NO KEY UPDATE → token mở (UUID) → session mở (UUID) → so snapshot (đổi verifier/head/role/status → `target_changed`, không retry) → journal exclusive → kiểm lại hold/case/head/registration → clock mới → stage (case → credential/status event → receipt + mutation → đóng mọi token/session `operator_recovery` → audit chỉ ID/mã) với mirror false và ép mọi constraint IMMEDIATE trước byte đầu → append/fsync → **luôn** kiểm lại login/case expiry bằng clock mới (mọi kết quả append) → nếu đã ghi đủ: confirm đúng các dòng của transaction → kiểm lại → commit.
4. **Outcome:** append lỗi → commit batch false, exit 5, gate đóng tới `recover`; lỗi sau khi bắt đầu append mà rollback → exit 4 `rolled_back_after_journal` (replay chỉ hạn chế, case đã tiêu); commit mất reply → exit 4 `auth_outcome_uncertain`; lỗi trả connection (rollback/close) là kết quả không thành công riêng theo pha, connection vẫn được invalidate/trả (xem mục sửa review). Thành công chỉ sau known commit + fsync + **proof mới** (authority, hold, bản ghi case confirmed, event confirmed, receipt đúng, pointer/head đúng operation, clock không lùi). `inspect --case-id` đọc outcome theo receipt, không chạy lại mutation.

`set-credential` giữ role, business status và `status_safety`; disabled vẫn disabled, held vẫn held. `resolve-initial-status` là **quyết định active mới**: chỉ khi business `active`, `status_safety=held`, latest status head không phải `disabled`, chưa có status sống hợp lệ; không thay credential, không phát session, đóng token/session cũ. Không có operator-enable, đổi role hay bootstrap lại: Admin duy nhất disabled vẫn khóa; bootstrap vẫn bị từ chối khi đã có User.

## Maintenance hold và runbook

Hold hợp lệ = so sánh gate đạt mọi điều kiện (không torn/missing/diverged/behind/unconfirmed/unjournaled) và chỉ còn recovery mở, **và** recovery đó đã đối chiếu xong (`backfill_complete`, `projection_seq ≥ seq recovery_begin`). Chỉ `recover --hold-for-operator` tạo trạng thái này; `hold` journal-only hoặc `recover` bị gián đoạn đều bị từ chối. Hold/pending không bỏ qua lỗi chain.

Trình tự đã chạy thật (writer dừng):

1. `python -m app.cli.recovery hold` (khi cần) → migrate → `python -m app.cli.recovery recover --hold-for-operator` (owner/migrator, như `recover`).
2. Migrator: `python -m app.cli.provision_auth_operator operator --login <login> --operator-id <uuid>`; tạo case file JSON ngoài repo (schema `ras.auth_recovery_case.v1`), `… register-case --case-file <path>`.
3. Operator (RAS_DB__* của login operator): `python -m app.cli.auth_recovery inspect --user-id <uuid>` → `set-credential …` và/hoặc `resolve-initial-status …` (case riêng mỗi quyết định).
4. `python -m app.cli.recovery recover` kiểm lại toàn bộ và đóng recovery; gate trả `auth_safety_pending` (C4 mới mở).

Hồ sơ người thật (ai là operator, danh tính người nhận, kênh xác minh) phải được lập trước vận hành; tài liệu này không chứa và không tạo inventory thật. File case trên POSIX phải chỉ owner đọc/ghi; trên Windows CLI không đọc ACL, deployment phải giới hạn thư mục.

## Replay và restore

Replay ghi mirror case (`replay`) và event auth, **không** tạo receipt/verifier/User/active; thiếu receipt thì credential quarantine, status held. Case đã tiêu bị nhận diện từ journal hiện hành kể cả khi DB restore không có hàng case/mirror; quyết định mất phải lập case mới với head mới. Snapshot trước C3 + journal mới không nhận credential cũ. Bảo vệ review/session/reset/invitation/common revocation và `hold` (chỉ đọc seq/hash) giữ nguyên.

## Kiểm chứng

Kết quả suite cuối, lệnh và node: xem [inventory](c3-test-inventory.json).

**Bản sửa review tại `0ee17f3`: 1.009/1.009 pass**, 0 fail/error/skip, 34 phút 28 giây; JUnit 1.009 unique khớp selection; 358 file source giữ nguyên SHA-256 trước/sau; collect toàn API 5.107, **4.098 not-run**. Selection như lượt đầu + 12 regression mới; loại cùng 281 node debt. 12 regression mới (fault injection thay `journal.os.fsync` và `Connection.rollback/invalidate`, **không** phải lỗi thiết bị/mất điện): trên code review `8fa504f` **9 fail / 3 pass** (4 ca authority/expiry × credential/status, 5 ca release; 3 ca vốn đúng: fsync lỗi với quyền hợp lệ, refusal không bị che, journal unavailable sau commit), sau sửa 12/12. Đối chứng tuần tự **8/8** (6 cũ + đưa recheck về sau `if written` + nuốt lại lỗi release), khôi phục bytes trước suite. Smoke HTTP không patch qua API/proxy ở head: 28/28 kiểm. ConPTY 4/4 nằm trong suite. Kết quả reviewer giữ riêng trong report review.

**Lượt nộp đầu (lịch sử, không là pass của head hiện hành):**

**Suite cuối tại `cb9518b`: 997/997 pass**, 0 fail/error/skip, 34 phút 30 giây; JUnit 997 unique, khớp selection; **358 file source** (`api/app`, `api/migrations`, `api/tests`, `scripts`) giữ nguyên SHA-256 trước/sau. Collect toàn API 5.095 node, **4.098 not-run**. Selection: toàn bộ test C3 (core 70, CLI/ConPTY/HTTP pending 10, migration 2) + hồi quy C1/C2a/C2b/C2c1/C2c2/C2d1/final-read-clock/invitation, journal session/reset/invitation/review, recovery, migrations/ORM/config/db_connection, migration C2d-5a/5b/5c2/5e1/5e2, schema C2d-5d2b và ca downgrade C2d-7 đã điều chỉnh. Loại **281 node debt** = 279 node **đã chạy lại tại base `58d059a`** (worktree ngoài repo, cùng cluster) và fail/error y hệt — 273 node của năm file journal/recovery cũ (fixture legacy gặp gate C1 đóng), 5 ca migration upgrade từ `0033`–`0037` (`UndefinedColumn review_requests.closure_transaction_id`: code recovery hiện hành trên schema cũ) và `test_db_connection::test_special_password_works_for_migration_api_and_bootstrap`. Hợp với 14 node của danh sách debt PR #55 nằm trong selection (12 trùng), tổng 281. Không node nào pass ở base mà fail ở head.

- **ConPTY Windows thật:** 4/4 (thành công nhập hai lần không echo; mismatch; policy; Ctrl-C). Ca EOF/GetPassWarning chỉ là prompt giả in-process, **không** tính là bằng chứng TTY. Stdin pipe và cờ cấm bị từ chối exit 2 trước khi chạm DB, không echo giá trị.
- **Đối chứng tuần tự 6/6** (lượt 2, tại `62cbbbf`; các commit sau chỉ đổi test fixture/script smoke): bỏ guard receipt `verified_*`, bỏ binding `session_user`, bỏ guard business disabled (Python + SQL), bỏ kiểm hold đã đối chiếu, coi append lỗi là đã ghi, bỏ kiểm tái dùng case theo journal — mỗi mutant làm đúng test nghiệp vụ fail bằng assertion; bytes/SHA khôi phục trong `finally`. Lượt 1 có 5/6: mutant status bị guard `latest_status_disabled` che (khớp chuỗi con), đã thêm ca cô lập và khớp mã chính xác.
- **Smoke HTTP không patch** (`scripts/c3_pending_smoke.py`, API uvicorn bằng login runtime + Next standalone trên loopback): sau quyết định `resolve-initial-status` thật bằng login operator qua CLI, trong hold và sau `recover`, `/healthz` 200; `/readyz` và `/auth/login`, `/auth/csrf`, `/library/items`, `/admin/users`, `/search`, `/rag/ask` đều 503 `service_recovering` qua API **và** proxy `/api/*`; `recovery status` báo `auth_safety_pending`.
- **Restore:** hai vòng `pg_dump`/`pg_restore` thật trong container cô lập, journal hiện hành giữ ngoài dump và nối qua hai vòng: credential g3/g4 mất không dựng lại, case đã dùng bị từ chối dù DB khôi phục không biết, phải lập case mới; journal nguồn không bị ghi.

Lượt lỗi chuẩn bị (fixture, migration downgrade thiếu DROP hai hàm, ORM chèn `case_*` NULL vào snapshot cũ, cửa sổ case cho tiến trình đồng hồ thật, một hồi quy thật `test_c2c2_migration::test_0038…` đã sửa, lượt suite 2 không chạy vì vượt giới hạn dòng lệnh Windows) ghi trong inventory; không tính vào kết quả cuối.

## Thay đổi fixture/test cũ (có chủ đích)

- Fixture C1/C2b/invitation từng dùng `verified_recovery`/`verified_initial_status` qua `stage` generic để dựng account sống; nay dùng reason runtime generic (`password_reset`, `invitation_accept`), giữ nguyên kỳ vọng nghiệp vụ. Ca C2b `verified_initial_status` đi qua đường operator C3 thật (`operator_initial_status`). Đếm credential transition của C2b chỉ tính bản ghi sau fixture.
- `test_0048_preserves_historical_rows_and_refuses_lossy_downgrade` (C2d-7): 0049 chặn downgrade installation đã đăng ký trước khi tới guard 0048, nên ca kiểm refusal `auth safety history prevents downgrade` và ghi hàng lịch sử all-NULL ở head bằng owner (có nhãn). Guard riêng của 0048 không còn tới được trên DB đã đăng ký.

## Phát hiện và debt

- **Generic initial reasons (debt, chưa sửa trong C3):** runtime vẫn có thể đúc receipt status `bootstrap`/`invitation_accept` cho User đã có qua `ras_auth_change`, vì trigger cặp chỉ chạy khi INSERT `bootstrap_state`/accept invitation. Test C2b `add_other(initial_reason=...)` dùng đúng đường này. C3 chỉ đóng `verified_*` theo phạm vi task; gói B/C4 nên ràng buộc hai reason này với transaction cặp tương ứng ở mức receipt.
- **Debt hồi quy xác nhận tại base:** 273 node journal/recovery cũ, 5 ca migration upgrade từ `0033`–`0037` và một ca `test_db_connection`, cộng danh sách 27 node PR #55. Không được miễn nghiệm thu; gói B/C4 phải sửa hoặc thay theo hợp đồng mới.
- `test_0038_populated_provisioned_upgrade` (C2c-2) so `row_to_json` toàn hàng; 0049 thêm cột `case_*` NULL nên ca này nay bỏ đúng các cột đó khi so (hồi quy thật phát hiện ở suite lượt 1, đã sửa).
- `test_c2c2_restore.py` gắn cứng cổng/label môi trường lịch sử, không chạy trong C3 (dump/restore C3 riêng thay thế phạm vi C3, không thay ca C2c-2).

## Bàn giao C4

- **Writer/consumer đã chuyển:** C2a–C2d (login/session, reset/status, bootstrap/invitation, toàn bộ final caller/writer/worker/RAG) + C3 operator. Không còn đường recovery account nào ngoài CLI operator.
- **Yêu cầu C4:** image đúng revision `0049`; login runtime/recovery/definer/migrator + login operator riêng (mật khẩu do deployment cấp, không default); journal độc lập ngoài DB/object/backup và hiện hành; storage/backup/retention; hồ sơ operator/inventory người thật.
- **Trình tự cutover:** dừng writer → `hold` → migrate → `recover --hold-for-operator` → provision operator/case → operator inspect/set-credential/resolve-initial-status cho Admin cần thiết → `recover` hậu kiểm → mở ready (task C4 riêng) → HTTP/browser qua proxy không patch, hai vòng restore/recover.
- **Debt hồi quy:** toàn inventory (4.098 node chưa chạy ở C3), 281 node debt nêu trên, các ca HTTP nghiệp vụ hoãn tới C4, generic initial reasons. C3 chưa chạy full inventory.
