# C2a: login/session, predicate và bàn giao

[Mục lục](README.md) · [Task gốc](tasks/m6c2b2c2a-login-session-predicates.md) · [Thiết kế](auth-restore-safety.md) · [C1](c1-auth-safety-foundation.md)

**Trạng thái: triển khai trên nhánh `feat/m6c2b2c2a-login-session`, chờ review, chưa merge; safety pending tới C4.** Base sau fetch là `c98bd3335e634e61c1c213e694fefe7eef0c107b`, chứa merge PR #33 và code cuối `c647d6e`. Head source trước task `0034`, sau task `0035`; không suy revision DB dev từ source. Giữ các docs bàn giao chưa commit của orchestrator trong PR này. Origin/cách chia C2 là quyết định orchestrator theo ủy quyền; các chi tiết triển khai dưới đây do coding agent chọn trong phạm vi task, không phải quyết định trực tiếp mới của chủ dự án.

## Predicate và ranh giới

`0035` thêm `ras_auth_account_live(user)` và `ras_auth_session_live(session,user,at,idle)` dùng chung cho login, issuance, resolve/touch, listing, CSRF và final read. Predicate account yêu cầu:

- Credential usable, status active/proven; User operation/generation/reason/time khớp projection.
- Receipt đúng User/domain/generation/decision/reason/time của cả credential và status; mirror confirmed không thay receipt.
- Projection trỏ mirror confirmed đúng User/domain và không có mirror mới hơn trong hai domain. Vì vậy một intent confirmed đã vào DB/journal nhưng chưa project/receipt cũng đóng account, dù snapshot User còn active.
- Session đúng owner, chưa revoked, generation không NULL và bằng credential hiện hành; `absolute_expires_at > at`, `last_activity_at > at-idle`. Clock mới sau khi chờ khóa; idle/absolute vẫn lấy từ config.

`require_writer(db)` giữ entry integrity bằng connection riêng trước journal lock. `require_final(db, writer=False, pending_endings=())` kiểm chain/hold/cutover trên **chính transaction đang giữ khóa**, lấy journal shared cho read hoặc exclusive trực tiếp cho writer. Không nâng shared → exclusive. Gate cuối không gọi connection riêng rồi tự chờ journal mình đang giữ.

Trong commit, chỉ trừ khỏi bộ đếm unjournaled closures các hàng đang nằm trong queue của chính transaction và chưa có mirror. Không bỏ qua unconfirmed, journal missing/ahead, hold, hoặc closure bên ngoài queue. Binding và deferred constraints được ép kiểm trước append. Đây là xử lý trạng thái trung gian của batch, không phải bỏ integrity.

`get_session_context` touch/commit, kiểm lại session + CSRF hash hiện hành rồi giải phóng transaction final read trước khi route lấy item/request locks. `get_auth_write_context` dùng riêng cho logout/revoke session: khóa User, kiểm session/CSRF mới và đăng ký actor để commit kiểm cuối. `session_is_live` không lấy User/session row lock khi caller đã giữ item lock. Original chung gọi helper này với clock callable; quyền file/version/checksum/epoch và kiểm byte giữ nguyên.

## Sửa P2 PR #34: caller của session listing

Review chỉ ra `/auth/sessions` có thể trả session B sau khi dependency xác thực A xong nhưng transaction khác đã thu hồi A và commit. Code cũ chỉ kiểm từng hàng trong `list_live_sessions`; mô tả listing đã chuyển không chứng minh actor cuối còn live. Sửa handler kiểm `session_live(db, ctx.session_id, clock(), idle, user_id=ctx.user_id)` **sau** đọc danh sách, dưới shared journal lock mà listing vẫn giữ tới cuối transaction. Predicate kiểm owner, latest credential/status/receipt, generation, revoked và TTL; clock lấy mới sau read. Sai → `401 session_expired`, clear cookie bảo vệ, không trả danh sách. Không lấy User/session row lock mới, không thay việc dependency rollback giải phóng transaction. Migration/role grants vẫn `0035`, safety pending tới C4.

Test `test_http_session_listing_rechecks_caller_after_dependency` gọi dependency thật rồi dừng bằng Event; xác nhận transaction dependency đã được giải phóng. Transaction runtime khác đóng A/commit journal hoặc đổi credential thật; clock kiểm soát idle/absolute boundary. Luôn giữ một session B hợp lệ để lỗi không bị che bởi danh sách rỗng. Trước sửa: **4 failed/1 passed / 11,12 s**, bốn lỗi đúng `200` thay vì `401`; sau sửa: **5 passed / 11,74 s**, gồm ca hợp lệ vẫn trả đúng hai session/current flag. Không patch predicate/guard/integrity hoặc giả User transition; chỉ boundary cutover scoped như C2a và wrapper dừng sau dependency thật.

Hồi quy trên source sau sửa: **372/372 pass / 411,31 s**, không failure/error/skip; **136 C2a** (68 session/login, 66 Origin, 2 migration) + **236 foundation/migration/config**, ORM/schema xanh. Collect **1191 nodes**, JUnit đủ **372 unique** pass và **819 chưa chạy**. Compileall/diff check xanh. Web không sửa/không chạy lại trong P2, số 43 pass/typecheck/build ở `a8cb586` là lịch sử. Journal/DB/roles thật ở container riêng `ras-c2a-p2-test-20261004`, port `58436`, Conda ras; runner/log/XML ở `%TEMP%/ras-c2a-p2-20261004`, không `.env` hoặc provider. Đã kiểm label rồi dọn đúng container/volume; các container dev ghi trước lượt P2 giữ nguyên ID/image/StartedAt qua đối chiếu chỉ đọc. Không sửa source/chạy mutant khi suite chính đang chạy. [Lệnh thực chạy](dev-setup.md#sửa-p2-listing-pr-34).

## Login và writer runtime

Login đọc snapshot tối thiểu, đóng transaction trước verify/hash thật. Sau công việc chậm, kiểm entry lại, khóa tất cả User liên quan theo UUID, reload và so snapshot + predicate. Mismatch từ chối bounded, không retry bằng verifier cũ. Generic error giữ nguyên; audit chỉ UUID/mã. Body giống nhau chưa chứng minh loại timing enumeration.

Policy hash đọc từ bảng migrator quản lý. Trusted verify rồi CAS `ras_auth_rehash`; policy/PHC và account/head kiểm trong routine, không GUC/raw UPDATE. Rehash giữ generation, operation và `password_changed_at`, không event/receipt mới. Session mới ghim generation đã kiểm; previous cookie chỉ đóng đúng session/owner còn live. Cookie chỉ phát sau commit xác định và final gate/session với clock mới. Commit exception luôn lỗi kể cả replay closure thành công.

`0035` bổ sung routines SECURITY DEFINER fixed `search_path=pg_catalog,public,pg_temp`, revoke PUBLIC:

| Routine | Runtime | Recovery | Công dụng |
| --- | --- | --- | --- |
| `ras_auth_account_live`, `ras_auth_session_live` (invoker) | có | có | Đọc predicate chung |
| `ras_auth_session_lock(user,ids)` | có | có | Khóa session đúng owner, sort UUID |
| `ras_auth_session_activity(id,user,at,idle,csrf)` | có | không | Touch/rotate nếu session live, activity không lùi |
| `ras_auth_session_close(id,user,at,reason)` | có | có | Đóng unrevoked đúng owner; deferred journal fence thật |
| `ras_auth_closure_mirror(line,via,result)` | có | có | Typed session/reset/invitation mirror false, không nhận cột tự do |

Routine owner là NOLOGIN definer sau provision; runtime không có raw INSERT/UPDATE protected tables, TEMP/DDL/owner/membership/migrator. Upgrade giữ ACL bằng các role đã có EXECUTE issuance/restrict, không đoán tên role. Không mở quyền activity/issuance cho recovery. Definer có UPDATE sessions để routine thực hiện mutation. Downgrade chỉ pristine; không hạ khi có User/registration/journal/receipt/head/progress/backfill. `0033/0034` giữ nguyên.

`commit_auth` nhận `db.info['auth_changes']` là list `auth_safety.Change`, cùng queues invitation/reset/session cũ. Một batch auth → invitation → reset → session; toàn mirror false + flush + kiểm constraint trước byte đầu, confirm cả batch chỉ sau append/fsync. Audit/FK cùng transaction. Mixed auth uncertain dành reconciliation đầy đủ cho recovery, không replay từng event để dựng quyền; session-only lost reply có restricted replay bằng runtime routines nhưng vẫn trả lỗi. Nhánh ORM lịch sử chỉ dùng khi routine `0035` chưa tồn tại để recovery schema cũ; không cấp raw grant runtime.

## Hợp đồng helper cho C2b/C2c/C2d

| Helper | Hợp đồng caller |
| --- | --- |
| `lock_users(db, ids)` | Tính đủ actor/target/previous owner trước, UUID tăng dần; không thêm User sau session/item/journal. Cache chỉ trong transaction hiện hành |
| `account_live(db, uid)` | Đọc SQL predicate; không thay entry/final integrity |
| `session_live(db, sid, at, idle, user_id=...)` | Đọc không khóa; caller cung cấp clock mới và integrity ở ranh giới |
| `session_is_live(db, sid, now=..., idle=..., clock=...)` | Final integrity cùng transaction rồi lấy clock; dùng khi đã khóa item, không khóa User sau item |
| `list_live_sessions(...user_id, now, idle, clock)` | Lọc từng session được liệt kê, giữ journal shared lock; **không tự xác thực session gọi request**. HTTP caller phải kiểm session/owner với clock mới trước response |
| `issue_session(...now, lifetimes)` | Entry + User lock + runtime create; caller commit_auth, kiểm cuối trước cookie |
| `resolve_session(...now, idle, clock)` | Runtime touch với clock sau User lock; caller commit_activity và final check |
| `rotate_csrf(...user_id, clock, idle)` | Runtime activity; caller commit_activity + final check trước Set-Cookie |
| `revoke_session` / `revoke_user_sessions` | User trước session; closure queue, chưa commit; session IDs tăng dần |
| `commit_auth(db)` | Flush, final exclusive gate, actor check, stage/mirror/append/fsync/confirm, một commit; exception không success/delivery |
| `commit_activity(db)` | Touch/CSRF không tạo event; commit lỗi → uncertain, không cookie mới |

C2b chuyển reset request/consume và disable/enable sang routine + Change/commit_auth; khóa User(s) → token UUID → session UUID → journal. Dependency `get_auth_write_context` hiện dành cho session writer một User; writer nhiều User phải tính actor + target rồi khóa cùng lúc theo UUID, không lấy actor trước rồi thêm target thấp hơn. Normal reset cần current usable credential operation; quarantine purpose cần latest credential head + quarantined, **không dùng `account_live` để mở quarantine**. Cả hai cần status active/proven + latest status receipt đầy đủ. Actor disable/enable phải được kiểm cuối; reset không tự enable. Endpoint và token closure writer hiện chưa chuyển hoàn chỉnh.

C2c cần routines User creation/bootstrap/invitation accept, PHC/policy/receipt ban đầu và FK/audit trước journal; email advisory → invitation UUID → User mới. Không dùng session issuance thay chứng cứ initial status, không raw INSERT User để vượt boundary. CLI bootstrap, invitation writer và delivery vẫn thuộc C2c/C3.

## Inventory caller chưa chuyển đầy đủ

Tất cả protected routes đi qua principal mới ở `api/deps.py`, nhưng principal ở đầu request chưa là T14 final check. Các resource ACL hiện hành vẫn bắt buộc và không được thay bằng auth predicate.

| Caller/file | C2a hiện có | Bàn giao |
| --- | --- | --- |
| `api/routes/auth.py`, `identity/login.py`, `identity/sessions.py` | Login/session/CSRF/actor commit + final gate đã chuyển | C2b/c reuse nền; C4 HTTP production |
| `common_original.py::prepare_common_original` | Final session helper mới, clock callable dưới item lock; original ACL giữ nguyên | C2d/C4 kiểm tích hợp storage/stream |
| `api/routes/uploads.py::download_original` private branch | Principal đầu + private owner ACL | C2d final session sau object I/O, trước bytes |
| `api/routes/uploads.py::upload_original`, upload intent | Origin + principal đầu; intent/rights hiện có | C2d final auth trước storage/job/publication sau upload I/O; không đổi xác nhận upload |
| `api/routes/reviews.py::admin_original` | Review grant kiểm lại dưới request lock, không final session | C2d session trước bytes, giữ đúng version/file review |
| `reviews.py`, `promotion.py` submit/withdraw/reject/approve | Resource locks/journal rights, principal đầu | C2d actor cuối sau copy object/trước commit; không thêm Admin private bypass |
| `search.py::_scope/_hydrate_scope/hydrate/_lock_hits/open_chunk/search` | ACL/version/generation trước top-k, rerank, response | C2d session trước embedding/rerank và final chunk/hit response |
| `rag.py::_checked_hits/_fresh/answer` | ACL evidence/provider/final item/collection locks | C2d session trước generation và final response; không coi quyền paper là auth session |
| `api/routes/library.py` metadata/chunk/coverage/references/candidates/patch | Principal đầu, SQL resource ACL | C2d final auth đọc/ghi dưới thứ tự khóa phù hợp |
| `api/routes/collections.py` CRUD/membership | Principal đầu, SQL owner/membership | C2d actor/session cuối; membership journal riêng chưa có |
| `api/routes/admin.py` status/invitation/dev outbox | require_admin đầu | C2b status writer; C2c invitation; C2d/C3 final caller/delivery |
| `publication.py`, `common_index.py` và routes Admin | Resource rights/live principal đầu | C2d actor cuối trước publication/revoke/index commit |
| `identity/password_reset.py`, `users.py` | Writer cũ, pending thật chặn | C2b routine/token/credential/status và predicates |
| `identity/bootstrap.py`, `invitations.py` | Writer cũ, pending thật chặn | C2c User creation/initial receipt/invitation |
| `ingest/indexer.py`, tasks/worker/beat/provider jobs | Recovery gate + job/item rights, không interactive session | C2d rà soát semantics caller; C4 integration, không truyền session giả cho job |

Không tuyên bố T14 toàn hệ thống hoặc toàn C2 hoàn chỉnh. Không đổi search/RAG common/private gates, pricing hay provider config.

## Kiểm chứng và giới hạn

Các số đo trong phần này là lịch sử tại `a8cb586`, trước sửa P2 listing; kết quả source sau sửa được ghi ở [mục P2](#sửa-p2-pr-34-caller-của-session-listing) và inventory hiện hành. Web không đổi trong P2, không chạy lại hay tự tính lịch sử là kiểm mới.

PostgreSQL 16/pgvector thật trong container riêng `ras-c2a-test-20261004`, port `127.0.0.1:58435`; roles LOGIN runtime/recovery riêng, journal/secret/evidence tổng hợp ngoài repo tại `%TEMP%/ras-c2a-20261004`. Python/pip xác minh thuộc Conda ras. Runner đặt admin test URL tường minh trước fixture và chặn HTTPTransport sync/async. Không đọc/sửa `.env`, dùng paper dev, gọi provider hoặc restart/migrate dev.

Nhóm writer chỉ fixture scoped thay `auth_safety.cutover`; integrity/hold/chain/DB guard thật. Có HTTP ASGI scoped fixture để kiểm login/CSRF/logout và cookie outcome; không gọi đó là HTTP production đã mở. Nhóm C1 pending không patch vẫn 503/health sống/ready đóng. Race dùng barriers/Event, hai kết nối runtime thật; ca chờ User lock quan sát `pg_stat_activity.wait_event=transactionid`. Verify/hash vẫn gọi Argon2 thật. Fault commit wrapper thực hiện commit hoặc rollback thật rồi ném lỗi mất reply; đây là injection ở Python boundary, chưa là sự cố mạng DB/mất điện thật.

Bộ C2a + C1 foundation + migrations + config **367 passed**, gồm **131 C2a** (63 session/login, 66 Origin, 2 migration) và **236 hồi quy được chạy lại trên source mới**. Sau lượt đầy đủ cuối chỉ bổ sung import enum dùng trong annotation, chạy lại session/login **63/63 / 123,72 s**; compileall và diff check xanh. Web **43/43**, typecheck/build thành công; warning Starlette/httpx, computed default và MODULE_TYPELESS_PACKAGE_JSON hiện có. [Inventory theo file/node](c2a-test-inventory.json) phân biệt pass với chưa chạy; không tính suite business chưa chạy là pass và không sửa expectation cũ thành 503/skip.

Đối chứng tuần tự **3/3** bị đúng assertions bắt: bỏ latest mirror fence (3 failed), thiếu Origin được phép (4 failed/32 passed), runtime raw UPDATE sessions (2 failed/5 passed). Khôi phục đúng bytes/SHA-256 bằng finally trước suite chính; không mutant/code edit song song suite. Lượt đối chứng đầu dừng vì runner replacement chưa xử lý CRLF, chưa đổi source; sửa harness rồi chạy đủ ba. Một lệnh sửa harness lỗi quoting cũng chưa đổi source.

Các lượt chuẩn bị không bị che khỏi báo cáo: smoke C1 thiếu NOLOGIN migrator test role; lượt C2a 19 pass/4 fail vì final gate đếm closure của chính transaction; 35 pass/1 fail vì replay runtime còn ORM write; 73 pass/4 fail vì cấu hình test cookie production/dummy DB Origin; chọn lại 49 pass; 117 pass/1 fail vì fixture bắt lỗi trước deferred commit. Đã sửa boundary/fixture tương ứng, không tắt integrity/guard. Sau tự review thêm ca latest confirmed mirror chưa project/receipt và hash-phase race; nhóm chọn 6 pass. Hai lượt đầy đủ được ghi trong runbook/inventory, không bỏ fail/flaky khỏi tổng.

Chưa chạy full business API/proxy/browser với cutover thật, Celery/beat Docker smoke mới, MinIO download/upload stream drill, backup/restore C4 tổng thể hoặc power-loss/production durability. Những thử nghiệm này không thể coi là xanh dựa trên direct scoped fixtures. C3 operator/delivery/initial status và C4 cutover/storage độc lập vẫn mở. Không phát sinh AI summary paper hoặc quyền private mới.

Đã kiểm label sở hữu và dọn đúng container/volume PostgreSQL cô lập. Năm container dev đã ghi trước task giữ nguyên ID/image/StartedAt qua đối chiếu chỉ đọc; không truy vấn DB dev. Evidence/journal tổng hợp được giữ ngoài repo.
