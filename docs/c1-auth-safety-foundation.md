# C1: nền auth safety, kiểm chứng và bàn giao C2

[Mục lục](README.md) · [Thiết kế](auth-restore-safety.md) · [Nguồn quyết định](decisions.md#quyết-định-nền-c1-theo-ủy-quyền-2026-10-03)

**Trạng thái hiện tại:** PR #33 đã merge `c98bd33`, chứa code cuối `c647d6e`; C1 head `0034`. [C2a đã triển khai trên nhánh review](c2a-login-session-predicates.md), head source `0035`, chưa merge; safety vẫn pending tới C4. [Origin/cách chia C2 đã chốt theo ủy quyền](decisions.md#chia-c2-và-chốt-origin-theo-ủy-quyền-2026-10-03). Các phép thử C1 dưới đây là chứng cứ lịch sử; 236 hồi quy nền/migration/config đã chạy lại cùng 131 C2a, không chứng minh writer C2 đã hoàn chỉnh hoặc revision DB dev.

Base `a5c75b88da72eaf3d8a28c0c00d82d12099f28a4` chứa merge PR #32; head source trước coding là `0032`. Revision mới `0033`, không sửa revision đã merge. C1 chỉ thử trên project `rasc1d6fc401c`, PostgreSQL 16/pgvector, journal/credentials tổng hợp ngoài repo. Không đọc/sửa `.env`, migrate/restart hoặc dùng dữ liệu dev; không gọi OpenRouter, không đổi `pricing_verified`. Các sửa docs của orchestrator chưa commit được giữ và đưa cùng PR.

Review P2 của orchestrator trên PR #33 phát hiện token vẫn được cấp khi snapshot active/proven cũ nhưng status head đã disable hoặc enable mới thiếu receipt. Bổ sung revision `0034` sau `0033`, chỉ thay guard reset token; giữ nguyên mọi file `0033`. Chứng cứ triển khai hiện có chỉ ghi DB C1 tạm đã dọn, không đủ xác minh toàn bộ DB lâu dài đã áp `0033`; không truy vấn DB dev để suy ra. Dùng revision mới để môi trường đã áp `0033` nhận sửa qua upgrade. Lượt kiểm lại dùng project DB riêng `rasc1reset3136add3`, không API/worker/dev stack.

## Schema và ranh giới quyền

`users` có generation/operation/reason riêng cho credential/status, `credential_state=quarantined`, `status_safety=held` mặc định; giữ verifier, business status và role legacy. FK ghép deferred trỏ receipt đúng operation/User/domain/generation qua hai cột domain computed. Receipt append-only mang decision/reason/time, gắn event và mutation cùng transaction; replay không tạo receipt. `transaction_id` là guard giao dịch live bổ sung, không là bằng chứng xuyên restore.

`auth_safety_heads` không FK User, giữ latest seq/op/generation/reason/time mỗi domain và status decision; trigger kiểm chính xác mirror mới nhất. Mirror có typed UUID/domain/decision/reason/time, parser Python và trigger SQL độc lập kiểm exact fields/types/binding bằng `IS DISTINCT FROM`, UUID v4 của operation/generation, reason/decision/time. Confirm chỉ false → true khi toàn bộ cột khác bất biến. UUID cấp bằng OS CSPRNG, kiểm toàn chain dưới journal lock và cả reserved IDs trong batch; unique DB chỉ là lớp bổ sung.

Session ghim credential generation; reset token ghim credential operation fence và purpose `normal/quarantine`. Legacy NULL vẫn giữ để backfill đóng, routine cấp mới từ chối NULL. `auth_hash_policies` lưu cấu hình Argon2id theo version; version 1 là cấu hình identity hiện tại (m=65536,t=3,p=4,salt=16,hash=32), chỉ migrator quản lý. CAS rehash kiểm old verifier/generation/operation, usable/active/proven/latest và policy version tăng; giữ generation/password_changed_at. DB kiểm PHC/cấu hình, không chứng minh hai PHC là cùng mật khẩu; trusted identity phải verify trước khi gọi trong C2.

Guard cấp reset token `0034` áp chung cho **normal và quarantine**: User active/proven, status operation/generation khớp head cuối, head quyết định active và receipt khớp operation/User/domain status/generation/decision/reason/time. So row bằng `IS NOT DISTINCT FROM`, JOIN receipt bắt buộc có bằng chứng; mirror confirmed không thay receipt. Credential fence/purpose giữ nguyên. `CREATE OR REPLACE` giữ OID/owner/ACL/search_path của guard, không journal I/O hoặc đổi safety/progress. Downgrade chỉ cho DB pristine chưa có registration/User/journal/receipt/head/progress; DB có lịch sử từ chối hạ ngay về `0033`.

`auth_safety_state` singleton version 1, cutover chỉ nhận `pending`, projection seq/backfill monotonic. Hoàn tất backfill không mở cutover. `evaluate_integrity` giữ kiểm chain/hold độc lập; `evaluate` thêm pending. Legacy writer bị chặn ở entry, kể cả bootstrap/session issue/resolve/CSRF và `commit_auth` rỗng. `/healthz` sống; `/readyz`, route được bảo vệ, worker và beat đóng. Không có switch/bypass auth cũ.

Provisioning dùng `app.cli.provision_auth_roles` qua kết nối migrator riêng; login roles/passwords do môi trường cấp, không tạo default account. Runtime/recovery không owner, superuser, CREATE ROLE/DB, BYPASSRLS hoặc membership; từ chối role sở hữu bảng/routine/schema/DB. NOLOGIN definer sở hữu routine, không sở hữu bảng/schema, không membership/DDL. Thu hồi PUBLIC write/EXECUTE, schema CREATE và database CREATE/TEMP; fixed search_path `pg_catalog,public,pg_temp` (temp cuối). Runtime không tạo được temp table; fixture chống temp-shadow cấp TEMP riêng để chứng minh routine vẫn đúng khi deployment cấp nhầm. Runtime SELECT protected tables nhưng không raw INSERT/UPDATE/DELETE; bảng nghiệp vụ khác vẫn dùng quyền cũ. Recovery có quyền closure/mirror legacy để chạy hợp đồng cũ, chịu trigger, không raw User/receipt/head/progress. Bộ grants này chưa chuyển tất cả writer sang runtime; chúng bị pending chặn tới C2.

| Routine | Runtime | Recovery | Hợp đồng |
| --- | --- | --- | --- |
| `ras_auth_lock_users` | có | có | UUID tăng dần, NO KEY UPDATE; từ chối lấy sau journal advisory lock |
| `ras_auth_mirror`, `ras_auth_confirm`, `ras_auth_project` | có | có | mirror exact/auth-only, confirm-only, fold latest; identity/recovery tin cậy chịu trách nhiệm append/fsync |
| `ras_auth_change` | có | không | latest event → receipt mới + mutation tương ứng cùng transaction, PHC/policy cho credential |
| `ras_auth_rehash` | có | không | CAS, không nhận reason/GUC bypass, không generation mới |
| `ras_auth_session_create`, `ras_auth_token_create` | có | không | cấp mới đúng state/latest generation/fence/purpose |
| `ras_auth_restrict`, `ras_auth_progress` | không | có | chỉ hạn chế; tiến độ không mở pending |

Migrator là chủ schema, deployment giữ credentials riêng khỏi API/worker. Bootstrap PostgreSQL của test là tài khoản quản trị tạo DB fixture/roles, không được mô tả là cấu hình migrator production. Threat model không chống DBA/host hoặc identity code đã bị chiếm quyền; quyền routine không tự chứng minh verify hay fsync.

## Replay, backfill và batch

Replay hai pha: replay mọi record (cả đã present), mirror/fold toàn chain; sau đó kiểm receipt snapshot và áp hạn chế mỗi User. Latest dùng seq, không timestamp. Disable cũ không áp trước enable mới có receipt; rollback credential giữ verifier/generation snapshot nhưng quarantine, rollback enable held, latest disable áp disabled. Không dựng User/verifier/receipt/active, không tự giải phóng quarantine/held; User absent chỉ có mirror/projection.

File ahead phải replay trước backfill. Legacy disabled chỉ import `legacy_disabled` nếu chain chưa có status decision cho UUID; active held, mọi credential legacy quarantined. Batch đóng session/reset token NULL bằng `auth_safety_backfill` đồng bộ parser/enum/SQL. init từ chối DB có User; register-existing/confirm-db-is-current không làm baseline usable/proven. Hold trên snapshot 0030/0031/0032 chỉ đọc seq/hash trước upgrade. Downgrade chỉ cho DB pristine chưa có User/journal/auth/progress; DB đã hoạt động bị fence, không quay về auth cũ.

`auth_safety.stage` là nền nội bộ trực tiếp cho C2, không HTTP/CLI recovery account. Caller khóa toàn bộ hàng/FK/audit trước journal; stage cấp IDs, mirror false/project/mutation/receipt, kiểm binding deferred thành IMMEDIATE trước byte đầu rồi trả records. `record_auth_endings(auth_changes=...)` ghép auth → invitation → reset → session vào một batch; append/fsync thành công mới confirm toàn batch. Lỗi none/torn/prefix/full-fsync giữ false toàn batch. Commit/rollback/uncertain không được suy từ `journal_confirmed`; recover trả `reconciliation_complete=True`, `safety_ready=False`, `gate=auth_safety_pending`.

## Inventory C1

Suite chính sau review: `test_auth_safety_foundation.py` (154 case: 146 direct + 8 HTTP pending riêng), toàn bộ `test_migrations.py` (28) và `test_config.py` (54), **236 passed**. Kết quả lượt cuối được ghi ở [testing](testing.md#c1-auth-safety-foundation). Mỗi fence trọng yếu có ca hợp lệ thực thi thành công cùng ca sai bị từ chối; HTTP 503 không tính là bằng chứng writer/ACL/journal đúng.

Đối chứng reset token trước sửa `0033`: bốn ca (disable/enable thiếu receipt × normal/quarantine) fail đúng vì không có exception, hai ca status hiện hành hợp lệ pass. Sau `0034`, cả sáu pass bằng role runtime login thật, kiểm riêng credential fence/purpose sai bị từ chối đúng guard. Hai ca migration bổ sung kiểm upgrade DB `0033` có auth history/provisioned roles giữ nguyên User/receipt/head/progress/journal/OID/owner/ACL, runtime vẫn cấp token hợp lệ; downgrade populated bị chặn và pristine được phép/upgrade lại. Lượt targeted đầu 7 pass/1 fail do fixture tạo hai token mở cùng User; sửa fixture rollback mỗi insert trong riêng ca upgrade, giữ unique business contract; lượt targeted cuối 8/8, rồi suite 236 pass, không code/mutant khi suite chính chạy.

| Nhóm thiết kế | Kiểm thực tế |
| --- | --- |
| T15 schema/upgrade | ORM↔schema; upgrade 0030/0031/0032 có User/Admin, session/token, hold trước migrate; downgrade pristine cho phép và lịch sử từ chối |
| T16 parser/DB | missing/null/type/extra/secret; UUID/reason/decision/time; typed binding NULL/sai, confirm-only, receipt cross-binding và append-only |
| T17 replay/backfill | old disable/new enable; rollback chỉ hạn chế; User absent; file ahead; present hậu điều kiện; lỗi project/restrict/progress/verify rồi chạy lại; legacy không usable/active baseline |
| T18 transaction | none/torn/prefix/full-fsync × commit/rollback/uncertain; mất reply commit thật có/không landed; false toàn batch trước append; seq liên tục |
| T19 pending | integrity riêng; bootstrap/commit rỗng/worker bị chặn; health/ready/route và API/web proxy thật; Celery/beat thật |
| T21 quyền/rehash | login runtime/recovery thật; raw protected mutation/DDL/SET ROLE/GUC từ chối; CAS/PHC/profile/session/reset fence hợp lệ và sai; temp shadow |
| T22 collision/khóa | UUID đã dùng trong rollback/absent/cross-domain; cấp ID retry; User lock sau journal từ chối; mixed auth/invitation/reset/session batch |
| Hồi quy trực tiếp | common/private metadata/derivative ACL, review grant và closure thiếu/có journal; publish/download gate/revoke/cleanup; session/reset/invitation đóng hợp lệ và reopen bị chặn |

Đối chứng tuần tự ba guard: bỏ typed NULL binding, cho runtime raw User write, bỏ kiểm UUID history. Cả ba bị test nhạy phát hiện (pytest exit 1), script luôn khôi phục đúng SHA-256 bytes trong finally; chạy suite chính sau khôi phục, không code/mutant cùng suite chính.

HTTP nghiệp vụ giữ expectation cũ và **hoãn tới C4**, không skip/pass giả: login/CSRF/expiry/session logout; invitation create/inspect/accept/revoke/reinvite/delivery; reset request/consume/replay; enable/disable/bootstrap/recovery account; common publish/revoke/download/index/search/RAG/review/private ACL qua API và proxy. Direct C1 chỉ phủ contract nền nêu trên, không thay full HTTP regression 901 case của PR #31 hoặc chứng minh toàn writer C2. Không có CLI recovery User/Admin, production delivery/cutover, restore drill tổng thể C4 hoặc chứng cứ mất điện/production durability.

## Bàn giao C2

**Cập nhật C2a:** [bàn giao cụ thể](c2a-login-session-predicates.md) có routines/helper/clock/commit, inventory final caller và test chưa chạy. Các mục dưới đây giữ hợp đồng tổng thể C2; phần login/session đã chuyển trên nhánh review `0035`, còn reset/status C2b, User creation/invitation C2c và final caller C2d. Không dùng số pass C2a làm bằng chứng hoàn tất các phần đó.

1. Origin/client compatibility và cách chia C2 đã [chốt theo ủy quyền](decisions.md#chia-c2-và-chốt-origin-theo-ủy-quyền-2026-10-03); bắt đầu [C2a login/session/predicate](tasks/m6c2b2c2a-login-session-predicates.md), rồi C2b/C2c/C2d. Giữ pending production; test trực tiếp có thể thay riêng kết quả cutover trong fixture theo thiết kế, không patch integrity/hold/session/receipt/journal và không thêm bypass sản phẩm.
2. Chuyển `identity/{login,sessions,password_reset,invitations,users,bootstrap,session_commit}`: verify/hash đúng boundary → lock/check latest dưới cùng transaction → `Change`/routine → một `commit_auth` batch. Không raw User/receipt/head/session/token auth columns; bổ sung routine session activity/CSRF/closure, User creation và audit/FK tối thiểu theo flow. C1 chưa cung cấp đầy đủ writer đó. Cookie/link chỉ sau commit đã xác định; commit lỗi không thành success qua replay.
3. Predicate dùng chung: User usable + active/proven + receipt matching latest projection/journal head; session generation phải bằng credential generation hiện hành; normal reset fence = current credential operation và usable, quarantine reset fence = latest credential head và quarantined. **Cả hai purpose** cần status active/proven + operation/generation khớp latest status head active + receipt đúng toàn binding; không cấp khi head disable hoặc enable mới thiếu receipt dù snapshot còn active/proven. C2 chuyển cả issuance/consume predicates tương ứng; kiểm lại dưới khóa tại bước cuối/download/provider publish như các gate hiện có; không dùng session generation hoặc status business đơn lẻ.
4. Khóa: login User → session; reset request/consume User → reset token UUID → session UUID; disable User → tokens → sessions; invitation create/reinvite email advisory → invitation UUID; accept email advisory → invitation UUID → User mới/flush FK/audit trước journal. Nhiều User/token/session sort UUID. Journal sau tất cả row locks; không giữ journal rồi lấy User/item tiếp theo. `require_writer` kiểm integrity qua connection riêng ở entry **trước** journal lock; final head/predicate trong transaction vẫn bắt buộc.
5. Rehash không auth event/generation; trusted verify rồi `ras_auth_rehash` CAS policy mới; kết quả false phải xử lý race. Receipt/event chỉ cho transition thật, reason allowlist thiết kế; password reset không enable. Stage force binding trước append; mirror false toàn batch, confirm sau fsync, không loop commit từng event.
6. C3 nhận operator/inventory chứng cứ/initial status/Admin/delivery riêng; C4 nhận cutover và full API/proxy regression, storage/writer journal độc lập và runbook production. Quyết định này còn mở, không tự cấp từ receipt/journal hoặc lời ủy quyền C1.
