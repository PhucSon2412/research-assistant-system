# C2b: reset credential và disable/enable

**Bàn giao tiếp C2c-1:** [bootstrap runtime và initial receipt](c2c1-bootstrap-receipts.md) đang review, source `0038`; writer bootstrap đã chuyển trong nhánh đó, invitation lifecycle còn C2c-2. Các inventory/số đo C2b dưới đây vẫn gắn source/lượt lịch sử của C2b, safety pending tới C4.

[Mục lục](README.md) · [Thiết kế auth sau restore](auth-restore-safety.md) · [Bàn giao C2a](c2a-login-session-predicates.md) · [Hợp đồng reset token](reset-token-journal.md)

PR #34 đã merge tại `78e0880e8b25178d13cdd1bf374153041e5f9781`, gồm sửa P2 session listing và cập nhật Docker pending của orchestrator (`d2a4be5`). C2b bắt đầu từ main này; **PR #35 đã merge `1d3e55825901dc81feb892f27a436bbb57b511f2`, chứa P2 `f2ba336`, source head `0036`**. Safety vẫn **pending tới C4**. Source head không chứng minh revision DB dev. Các phần kiểm chứng dưới đây giữ trạng thái khi chạy từng lượt, không là phép đo mới sau merge. [Task tiếp C2c-1](tasks/m6c2b2c2c1-bootstrap-initial-receipts.md) giao bootstrap/initial receipt; invitation lifecycle tiếp ở C2c-2. Không nâng/restart/truy vấn DB hoặc journal dev, đọc/sửa `.env`, gọi provider hoặc tạo venv.

## Predicate và runtime routines

`0036` thêm predicate SQL invoker fixed search path:

| Helper | Điều kiện |
| --- | --- |
| `ras_auth_status_live(user)` | active/proven, User khớp latest status projection, receipt đầy đủ User/domain/generation/decision/reason/time; mirror confirmed và không mirror status mới hơn |
| `ras_auth_reset_eligible(user,fence,purpose)` | status live cho cả hai purpose; fence khác NULL bằng latest confirmed credential head, không mirror credential mới hơn. Normal thêm account live (usable + credential receipt); quarantine yêu cầu credential quarantined |
| `ras_auth_reset_token_live(token,user,at)` | đúng owner, chưa used/revoked, TTL nghiêm ngặt `expires_at > at`, clock hữu hạn và reset eligible |

INSERT token có fence dùng cùng eligibility; UPDATE không được đổi fence/purpose. Token legacy NULL vẫn không dùng được. `0033/0034/0035` giữ nguyên. Runtime/recovery có EXECUTE token lock/close và predicates; runtime không raw INSERT/UPDATE token/User/receipt/head/mirror. Definer NOLOGIN có UPDATE token cho routine, không trao quyền này cho runtime. Upgrade giữ các role đã provision qua ACL hiện có; PUBLIC không có EXECUTE. `ras_auth_token_lock(user,ids)` khóa đúng owner, UUID tăng dần; `ras_auth_token_close(id,user,kind,reason,at)` chỉ đóng hàng còn mở, deferred journal guard thật vẫn bắt thiếu/sai closure. Downgrade chỉ pristine.

## Writer và thời điểm kiểm

Request normal lấy UUID từ email đã chuẩn hóa, đóng transaction đọc rồi khóa User. Đọc latest head, kiểm eligibility, đóng mọi token cũ bằng routine/closure queue, tạo token mới bằng `ras_auth_token_create` có fence/purpose. Audit + token mới + token closure cùng `commit_auth`. Clock mới ở cấp token, cuối journal lock và sau commit. Link chỉ được dựng sau commit xác định và final token predicate.

Quarantine staging dùng helper **private** `_issue(...purpose='quarantine')`; chỉ direct fixture kiểm writer này. Public `request_reset` luôn normal. C2b không thêm endpoint, boolean approval flag, operator hay adapter production để mở quarantine. C3 phải kiểm chứng đúng User UUID/recipient qua thẩm quyền/kênh đã chốt trước khi expose đường issuance. Email hoặc quyền đọc dev outbox không là chứng cứ đó. Held/disabled không cấp/confirm reset web cho cả hai purpose.

Confirm chụp snapshot verifier/policy/credential/status operation/generation/state và token fence/purpose, lấy DB hash policy, rồi **rollback transaction đọc trước Argon2**. Quarantine từ chối mật khẩu giống verifier snapshot hiện còn trong DB; không tuyên bố kiểm được mọi mật khẩu lịch sử đã mất. Hash mới ngoài khóa, entry integrity chạy lại, User → tokens UUID → sessions UUID. So snapshot và kiểm lại token bằng clock mới; mismatch từ chối bounded, không retry verifier cũ. Consume/revoke token/session + credential Change/receipt/verifier + audit cùng một batch. Guard token chạy cuối sau journal exclusive trước stage credential; nó chỉ cho phép consumed của chính transaction với đúng ID/owner/digest/fence/purpose/reason/time, TTL và eligibility còn đúng. Không dùng token đã đóng bên ngoài transaction làm chứng cứ. Reset giữ status và không issue session.

Disable/enable nhận cả actor session và CSRF; khóa **actor + target Users theo UUID cùng lúc**, trước token/session/journal. Không dùng dependency writer một User để khóa actor trước rồi thêm target. Kiểm lại session/generation/TTL/account/role admin/CSRF, đăng ký actor cho commit kiểm cuối dưới journal exclusive và sau commit bằng clock mới. Disable luôn có status event/receipt, kể cả không token/session. Enable tạo quyết định mới nhưng không đổi credential/quarantine hoặc mở lại token/session. Enable cần receipt initial status active đúng User, reason `bootstrap`/`invitation_accept`/`verified_initial_status`, khớp đầy đủ journal mirror confirmed trong status history hiện hành. `admin_disable`/`admin_enable` không tạo chứng cứ initial status; journal intent không receipt cũng không đủ. Thiếu chứng cứ trả `409 initial_status_verification_required` dù đã disable trước đó. Approval/operator initial status vẫn thuộc C3. Tự đổi status vẫn bị chặn.

`commit_auth` trả batch records (callers cũ có thể bỏ qua), cho writer so exact operation sau commit. Queue `auth_changes`, token/session/invitation và actor được lấy trong một transaction. Append/fsync lỗi: toàn batch false, commit các thay đổi theo hợp đồng hiện có rồi lỗi, gate đóng. Commit mất reply luôn lỗi; mixed credential/status batch không replay từng event để dựng quyền. Recovery fold/restrict có thật, không receipt mới từ replay. Token-only replacement có restricted replay bằng runtime routines và vẫn không delivery/success. Replay reset closure mới dùng token lock/close + typed mirror/confirm; nhánh ORM chỉ cho schema lịch sử chưa có routine.

Background delivery mở transaction mới, final integrity shared + token predicate với clock mới, giải phóng transaction **trước** adapter I/O. Đây là ranh giới kiểm ngay trước send; thu hồi sau điểm đó không lấy lại link đã gửi. Public request giữ `202 accepted` chung khi middleware cho qua, không hứa delivery hoặc chống timing enumeration. Origin bắt buộc và CSRF vẫn do middleware; authenticated status còn kiểm CSRF hash của session.

## Chữ ký helper cho caller tiếp theo

- `request_reset(db, email, now, lifetime, clock=utcnow)` trả `IssuedReset | None`; `IssuedReset` thêm `token_id` để kiểm lại trước delivery. `now` giữ tương thích chữ ký, các quyết định TTL lấy từ **clock callable mới**, không dùng thời gian đầu request.
- `confirm_reset(db, raw_token, new_password, now, policy, clock=utcnow)` trả User UUID sau receipt/operation cuối; caller không tạo cookie/session. `policy` là giới hạn password config, tham số hash lấy DB policy.
- `set_user_status(... actor_session_id, csrf_token, clock=utcnow)` bắt buộc actor session/CSRF ngoài actor UUID. Gather đủ actor/target trước `lock_users`; route truyền `get_clock`, không khóa actor bằng dependency single-User trước.
- `commit_auth(db)` trả list `recovery.Journaled`; auth record đầu batch cho writer một Change. Caller có nhiều Change phải chọn đúng User/domain, không suy receipt từ `written` hoặc body journal.
- `reset_tokens.lock_open_tokens/end_token/revoke_open_tokens` dùng restricted routines và queue, không commit. User phải đã khóa, tokens trước sessions/journal. Không gọi closure helper từ transaction read đang giữ shared journal.

C1 backfill legacy NULL fence/generation trong `recover` vẫn dùng role migration/recovery và hợp đồng hạn chế riêng; không chạy bằng runtime API, không phải đường issuance/đổi verifier/status. C2b không cấp raw quyền mới cho runtime hay mở production recovery. C2c/C2d phải giữ integrity/hold và thứ tự khóa, không coi patch pending của fixture là cơ chế sản phẩm.

## Bàn giao và giới hạn

| Phần | C2b | Tiếp theo |
| --- | --- | --- |
| `identity/password_reset.py`, `reset_tokens.py`, `users.py` và auth/admin status routes | writer/predicate/routine/receipt/clock/actor đã chuyển | C3 authorize quarantine delivery; C4 cutover HTTP thật |
| `identity/session_commit.py`, `recovery.py`, `db_roles.py` | batch trả records; reset final guard, actor role; runtime reset replay và grants | C2c reuse cùng batch; không raw User creation |
| `identity/bootstrap.py`, `invitations.py` | writer cũ vẫn đóng bởi pending | C2c initial User/credential/status receipt + invitation closure, delivery compensation |
| Outbox/list Users, file/review/search/RAG/collections/publication/worker callers | inventory C2a vẫn còn các final checks chưa chuyển | C2d giữ ACL và lock order; không lấy User sau item/journal |
| Operator/approval/initial status và production delivery | chưa triển khai/chưa chốt qua C2b | C3; không bootstrap để cứu Admin quarantine/held/disabled |
| Ready/backup/restore/proxy/storage/full business HTTP | chưa mở/chưa drill mới | C4, không suy ready từ direct fixture |

## Kiểm chứng

Đã kiểm trên PostgreSQL pgvector 16 riêng `ras-c2b-test-20261004`, port `127.0.0.1:58437`; Conda ras, runtime LOGIN thật, journal/evidence tổng hợp ngoài repo `%TEMP%/ras-c2b-test-20261004`. Runner đặt admin URL tường minh trước fixture, không fallback `.env`, chặn HTTP provider transport sync/async. Fixture chỉ thay `auth_safety.cutover`; hold/integrity/receipt/finality guards/commit thật. Lượt cuối **471/471 pass / 595,41 s**, không failure/error/skip: **99 C2b** (97 reset/status, 2 migration) + **372 C2a/C1/migration/config**; ORM/schema xanh. [Inventory theo file/node](c2b-test-inventory.json) có **1290 nodes**, **819 chưa chạy**. Compileall/diff check xanh; 195 file source API giữ nguyên SHA-256 trong lượt cuối. Test HTTP ở đây là scoped fixture, không phải production cutover.

Có lifecycle normal reset/replace/consume/receipt/closure, quarantine latest fence và từ chối mật khẩu snapshot; held/disabled/legacy/missing latest receipt; clock hết hạn sau hash và khi chờ journal advisory lock thật (quan sát `pg_stat_activity.wait_event=advisory`), actor revoke/expiry giữa dependency thật và handler HTTP, đúng CSRF/role. Hai confirm cùng token chỉ một credential transition; hai Admin disable nhau chỉ một người thắng, không deadlock. Delivery final token gate và thử lấy User + exclusive journal ngay trong adapter chứng minh đã giải phóng khóa trước I/O. Public request 202 chung, commit lost reply không delivery và confirm không issue cookie/session.

Fault matrix append none/torn/prefix/full-fsync-error + commit biết kết quả hoặc mất reply với DB thật landed/rollback cho confirm/disable/enable; batch false/gate đóng và recover fold/restrict/receipt đúng. Token-only replacement lost reply replay thật bằng runtime, vẫn lỗi. Upgrade populated/provisioned 0035 giữ bytes journal/User/head/receipt, grants và pending; downgrade history từ chối, pristine round-trip đạt.

Đối chứng tuần tự **2/2**: bỏ actor check trước stage gây 1 failure (target bị disable dù actor hết hạn); bỏ status gate của quarantine eligibility gây 3 failure (disabled, held, latest status thiếu receipt). Khôi phục đúng bytes/SHA trước suite cuối. Không code/mutant song song suite cuối.

Các lượt chuẩn bị được giữ trong inventory: 49 pass; lượt mở rộng 49 pass/1 fail vì fixture chưa khai báo Origin allowlist, sửa fixture rồi 51 pass; lượt chọn đầy đủ đầu 467 pass/1 fail vì PostgreSQL cô lập thiếu role NOLOGIN `c1_migrator`, tạo prerequisite chỉ ở DB thử rồi chạy lại đủ 471. Một lệnh sửa docs dừng ở assert do encoding stdin PowerShell, chưa ghi file; chạy lại UTF-8 thành công. Lệnh đọc label Docker lần đầu lỗi Go-template quoting, dừng trước xóa; đọc JSON labels, xác minh rồi dọn đúng container/volume. Không che các lỗi chuẩn bị thành pass mới.

Đã dọn đúng PostgreSQL cô lập sau kiểm label. Chín container dev giữ ID/image qua snapshot đầu/cuối; năm container trọng yếu giữ StartedAt qua các lần inspect chỉ đọc. Không truy vấn DB/journal dev. Evidence tổng hợp vẫn ngoài repo. [Lệnh thực chạy](dev-setup.md#c2b-resetstatus-cô-lập).

Fault injection ở boundary Python/driver thực hiện commit hoặc rollback thật rồi mất reply; không là thử mất điện hoặc durability production. Web không sửa; số 43 pass/typecheck/build của C2a là lịch sử, không tự tính thành kết quả C2b.

## Sửa P2 PR #35: initial status độc lập

Điều kiện cũ chỉ cần có status operation nên disable rồi enable có thể mở account legacy credential-only mà chưa xác minh initial status. `_initial_status_verified` nay đọc receipt lịch sử active với reason initial hợp lệ, đúng User/domain/generation/decision/reason/time, mirror confirmed và seq không vượt status head hiện hành. Receipt bất biến dưới khóa User đã có; commit vẫn kiểm journal/integrity cuối. Không thêm User lock sau journal, migration/grant hoặc operator C3.

Fixture positive C2b trước đây dùng `admin_enable` làm status đầu; nay tạo event/receipt initial hợp lệ bằng journal/DB thật. Đây là dựng tiền điều kiện kiểm writer C2b, không là triển khai hay kiểm authority/operator C3. Ca credential-only recovery → held → disable → enable vẫn trả `409 initial_status_verification_required`, không thêm receipt/audit/journal hoặc session. History chỉ `admin_enable` và initial intent đã fsync nhưng rollback receipt/replay cũng không đủ. Ba reason `bootstrap`/`invitation_accept`/`verified_initial_status` đã commit vẫn disable/enable và login được.

Trước sửa **3 failed/3 passed / 12,55 s**; sau sửa **201/201 passed / 384,23 s**, 0 fail/error/skip: 103 reset/status + 2 C2b migration + 68 C2a login/session + 28 migration/ORM. Ba warning hiện có (Starlette và computed defaults). Compileall/diff check xanh. Collect hiện tại **1296 nodes**, **1095 chưa chạy lại trong lượt P2**; suite 471 là lịch sử `bdec9ea`, không tính thành pass mới. [Inventory từng node](c2b-test-inventory.json) giữ hai lượt riêng. Lệnh đầu dùng nhầm tên file `test_c2b_migrations.py`, không chạy test; đã sửa và chạy đủ nhóm cuối. Assertion inventory bắt khác biệt prefix node collect/JUnit trước mọi ghi file; chuẩn hóa rồi kiểm lại thành công.

Conda ras, PostgreSQL/journal cô lập `ras-c2b-p2-test-20261004` port `58438`, runtime LOGIN thật, giữ guard/hold/integrity và chặn provider transport. Đã dọn container/anonymous volume sau kiểm label. Năm container dev giữ ID/image/StartedAt qua so sánh chỉ đọc; không đọc `.env`, truy vấn DB/journal dev hoặc restart stack. PR #35 giữ mở để review, source `0036`, safety pending tới C4. Web/full business HTTP/cutover và C2c/C2d/C3 chưa chạy mới.
