# C2c-2: invitation qua runtime và initial receipt

[Mục lục](README.md) · [Task](tasks/m6c2b2c2c2-invitation-lifecycle.md) · [C2b](c2b-reset-status.md) · [Bootstrap](c2c1-bootstrap-receipts.md) · [Journal invitation](invitation-journal.md)

**C2c-2 triển khai trên nhánh review, chưa merge; safety pending tới C4.** Fetch xác minh `origin/main=d1b53e653197af0d11f79cbdda0df514cb0af69a`, chứa merge PR #36 và source `0038`. Nhánh `feat/m6c2b2c2c2-invitation-runtime` thêm `0039`, giữ nguyên `0033`–`0038`. Các docs chưa commit của orchestrator, gồm review #36 và task, được giữ trong PR. Không đọc/sửa `.env`, truy vấn/nâng/restart stack dev hoặc gọi provider; Conda ras và PostgreSQL/journal tổng hợp cô lập.

## Boundary DB và binding

`invitations` thuộc `db_roles.PROTECTED`: runtime/recovery chỉ SELECT; không raw INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER. Routines SECURITY DEFINER thuộc NOLOGIN definer sau provision, fixed `search_path=pg_catalog,public,pg_temp`, PUBLIC không EXECUTE. Runtime không sở hữu table/schema/routine, không DDL/TEMP/membership/BYPASSRLS; không fallback migrator.

| Routine | Runtime | Recovery | Hợp đồng |
| --- | --- | --- | --- |
| `ras_auth_invitation_lock(ids)` | có | có | UUID tăng dần, trước journal; writer đã lấy email advisory |
| `ras_auth_invitation_create` | có | không | Actor Admin/session/CSRF live; normalized email, UUID v4, digest, TTL; tạo link và audit |
| `ras_auth_invitation_revoke` | có | không | Actor live; chỉ `admin_revoked`/`invitation_replaced`; chỉ link chưa đóng |
| `ras_auth_invitation_prepare` | có | không | Invitation ID/digest/email/TTL/open, policy PHC mới nhất; User UUID mới, role cố định user; không cập nhật account cũ |
| `ras_auth_invitation_compensate` | có | không | Đúng ID/digest/creation transaction/inviter; chỉ đóng link còn mở bằng `delivery_failed` |
| `ras_auth_invitation_recover` | có | có | Chỉ `closed_by_recovery`, không account/receipt/verifier/active hoặc accepted FK |

Runtime được restricted replay closure khi commit mất reply, vẫn trả lỗi. Recovery không có creation/accept/compensation grants. Helper invoker chỉ được EXEC bởi definer, không mở primitive auth mới cho caller. Upgrade chuyển grants đã provision theo ACL issuance/restrict, không đoán tên login role. Provision mới thêm SELECT/INSERT/UPDATE invitation và EXEC helper tối thiểu cho definer. Các grants/protection/bootstrap `0038` giữ nguyên.

`creation_transaction_id`/`acceptance_transaction_id` nullable chỉ để giữ lịch sử cũ. Routine create ghi marker creation; compensation context gắn chính marker đó. Chuyển accepted mới buộc acceptance marker `txid_current()`, không NULL; marker không được sửa lại. Deferred pair trigger kiểm User/email/role user/active/usable/proven, hai receipt đúng User/domain/generation/decision/reason `invitation_accept`/time và cùng transaction, credential → status → đúng invitation closure liên tiếp, reason `invitation_accepted`. Creation/password/update time phải khớp; audit success đúng actor User mới, invitation ID/time/allowlist. C1 vẫn kiểm receipt–mirror–head/PHC và UUID/generation; NULL không được làm predicate đi qua.

Prepare luôn INSERT User mới và audit trước journal; không tái dùng bootstrap prepare, không role input hay approve/quarantine flag. SQL chặn User UUID đã có mirror/history kể cả absent, gồm collision với auth operation/generation; final guard kiểm independent chain dưới journal lock. Stage giữ allocator operation/generation của C1 với toàn history và reserved batch. Pair/audit/FK/closure được ép `ALL IMMEDIATE` **trước byte đầu**; thiếu một receipt, wrong domain/reason/time/User/accepted FK/audit không được append. Unique violation mới được chuyển thành lỗi SQL chung, không DETAIL mang email/hash.

Downgrade chỉ pristine; User/invitation/registration/journal/auth/progress/history đều chặn. Upgrade không backfill legacy thành proven/usable hoặc mở safety. `hold` snapshot cũ vẫn chỉ đọc seq/hash; không đọc cột invitation mới ở hold.

## Writer, clock và lock map

| Luồng | Thứ tự khóa live |
| --- | --- |
| Admin create/reinvite/revoke | Gather actor User → User UUID NO KEY UPDATE → email advisory → invitation UUID → FK/audit → journal exclusive |
| Accept ẩn danh | Email advisory → invitation UUID → INSERT **User mới**/FK/audit → journal exclusive; không lấy auth lock User có sẵn |
| Compensation | Email → đúng invitation → system audit → journal exclusive; không lấy lại quyền Admin đã mất |
| Post-accept | Email → invitation → journal shared → exact User/receipt/head reads; không thêm User auth lock |
| List/inspect/delivery read | Journal shared và fresh reads/clock; không User lock sau journal |
| Recovery/backfill | Replay từng domain trước row/journal như C1; backfill đổi sang Users → emails → tokens → sessions → invitations → journal, vẫn trong hold/gate đóng |

Admin chỉ cần actor hiện có; FK inviter/accepted và audit chuẩn bị trước journal, tương thích NO KEY UPDATE của auth. Reset/session thứ tự C2b không đổi. Routines từ chối lock/prepare sau journal shared/exclusive; không upgrade shared → exclusive. Recovery không còn lấy email trước User trong backfill; tránh inversion ngay cả với writer đã qua entry trước hold.

Accept lookup tối thiểu cho validation/policy rồi rollback **trước Argon2**. Trusted hasher đọc policy DB, sau hash kiểm entry integrity lại, lấy email/invitation, reload digest/ID/email/open/policy/TTL bằng clock mới. `now >= expires_at` từ chối. Một token chỉ tạo một User; thua accept/reinvite/revoke/recovery/compensation không tạo receipt/account/session.

Hai Change credential/status `invitation_accept` rồi accepted closure dùng **một `commit_auth`**. Toàn mirror false trước append; confirm toàn batch chỉ sau append/fsync. Guard invitation/actor chạy dưới journal lock trước stage, và clock/binding/actor kiểm lại **sau append/fsync, ngay trước commit**. Kiểm actor lần bổ sung áp cả caller C2a/C2b có actor, giữ các hợp đồng batch/replay; các caller này phải được chạy hồi quy. Late refusal có thể để intent bền trong file nhưng DB rollback: không diễn giải là account đã tạo.

Known commit mới được accept 201. Hậu kiểm operations chọn theo **User UUID và từng domain**, không `batch[0]`; User đúng accepted FK/time, account live, hai receipt/transaction đúng marker. Không issue session/cookie. User mới có initial status proof thật của C2b, không dùng `admin_enable` làm initial proof.

Create/revoke nhận `actor_session_id`, raw CSRF từ request và clock callable ngoài actor UUID. `commit_auth(postcheck=False)` chỉ dùng create để backend giữ được known-commit context trước hậu kiểm/bù; writer vẫn kiểm cuối và commit xác định. Create rỗng batch vẫn qua integrity/actor; không delivery sau unknown commit. List kiểm actor/session/role với clock mới sau read dưới shared journal lock; inspect kiểm token/open/TTL/gate cuối trước email/name. Route vẫn đi principal/Origin/CSRF hiện hành; inspect không đòi Admin.

## Delivery và bù

`CreatedInvitation` giấu token/ORM/binding khỏi repr; private `_DeliveryBinding` do backend tạo từ **known committed issuance**, chứa ID/digest/creation transaction/inviter, không request flag/reason. Trước adapter: kiểm lại binding, gate, Admin/session/generation/latest receipts/role/CSRF, invitation open và TTL; xây message, rollback transaction rồi mới `send`. Adapter tắt không được gọi. `OutgoingMessage` giấu link và recipient khỏi repr. SQL engine giấu bind parameters **và DEBUG result rows** (hide_parameters một mình không giấu Row); public responses/outbox/adapter giữ allowlist hiện hành.

Sau adapter, gate/actor và row được đọc lại bằng clock mới. Actor mất hiệu lực không success; expired link không success. Accepted/revoked/recovery status sau race lấy từ row mới. Bù transaction riêng khóa email/invitation, kiểm binding và chỉ đóng link còn mở; không cần actor Admin còn live. Accept/closure khác đã thắng giữ nguyên User/receipt/accepted hoặc revoke/replacement/recovery. Lỗi bù không bị nuốt; gate/journal/commit error trả outcome auth tương ứng.

Adapter có thể gửi rồi báo lỗi; actor/TTL có thể đổi sau final check trước send hoặc trong adapter. Không giữ transaction/row/journal lock qua I/O để hứa loại mọi khe đua. Message đã gửi không lấy lại được. `delivery_failed` hay response 401/403/503 không chứng minh message chưa tới người nhận. Unknown commit có thể đã tạo invitation/account nhưng không có link/cookie/success response.

## Fault và restore

| Cửa sổ | DB/journal và outcome |
| --- | --- |
| Prepare/binding/gate/actor/TTL lỗi trước append | Rollback, không account/receipt/link delivery mới |
| None/torn/prefix/full nhưng fsync lỗi, DB known commit | Toàn batch mirror false; User/receipt/closure có thể đã commit; lỗi auth journal, gate đóng |
| Journal intent bền nhưng DB rollback/lost reply | Luôn lỗi; không dựng User/verifier/receipt từ replay, không suy accepted/delivery thành công |
| Clock hết hạn trong append/fsync | Late check rollback; complete closure còn trong journal có thể đóng link qua recover, không dựng account |
| Prefix chỉ credential hoặc credential/status, DB rollback | Không có invitation closure nên không tự đóng link; absent User projection hạn chế, retry với UUID mới sau recover hợp lệ |
| Full accepted closure, snapshot trước accept | `closed_by_recovery`, không accepted FK/account/receipt; không nhận lại link |
| Adapter gửi rồi lỗi/actor mất quyền | Bù đúng link còn mở; accept/closure thắng giữ nguyên; lỗi không chứng minh chưa gửi |

Matrix kiểm none/torn/credential prefix/status prefix/full-fsync/ok × commit/rollback/uncertain landed/uncertain rollback. Live mirror ahead chỉ sửa bằng xác nhận DB live; snapshot restore không dùng cờ này. Recovery sync/replay/postconditions/idempotency giữ C1 và năm domain cũ. User/receipt còn trong snapshot chỉ live khi khớp latest head, không suy từ invitation closure. Lịch sử mất khỏi cả DB và journal không backfill được. Fault Python/fsync và dump nhỏ không là bằng chứng mất điện hay durability production.

## Kiểm chứng và bàn giao

Kết quả cuối và node/source inventory sẽ được cập nhật trước push/mở PR. Các lượt chuẩn bị được giữ riêng; không cộng lượt chọn trùng nhau thành full suite. PostgreSQL pgvector 16 `ras-c2c2-db-20261004`, label `ras.task=c2c2-20261004`, port 58442; journal/JUnit/credentials/dump tổng hợp ngoài repo tại `%TEMP%/ras-c2c2-20261004`. Fixture scoped chỉ thay `auth_safety.cutover`; integrity, runtime/recovery LOGIN, SQL, receipt/head/clock/commit giữ thật. External HTTPTransport sync/async bị chặn, explicit admin URL trước pytest không fallback `.env`.

Hồi quy HTTP invitation cũ dùng runtime thật và tiền điều kiện receipt thật theo C2a/C2b; giữ assertions nghiệp vụ. Fixture SQL schema thêm transaction marker để tiếp tục kiểm guard account/field cũ, không nới pair fence. Full HTTP/proxy/MinIO/cutover toàn sản phẩm thuộc C4; test invitation đã chuyển không hoãn tới C4. Web không đổi, không chạy lại test/typecheck/build/browser. Các drill và tests `0032` phụ thuộc storage/raw fixture/closure-only matrix là **lịch sử**, chưa chạy lại trên C2; không dùng số 901/59 cũ làm kiểm source này.

**C2d final callers/integration còn mở:** `/admin/users`, `/admin/dev-outbox`, `/auth/session`; private download/upload/intent; review submit/withdraw/reject/approve/original và promotion; library metadata/chunk/coverage/references/candidates/patch; collections CRUD/membership; search scope/hydrate/chunk/provider/response; RAG evidence/generation/final response; publication/common-index Admin commit và worker/task semantics. Giữ ACL đúng item/version/file/generation, không thêm Admin private bypass, không lấy User sau item/journal; common original đã có final auth helper nhưng drill integration storage/stream còn riêng. Đối chiếu inventory [C2a](c2a-login-session-predicates.md#inventory-caller-chưa-chuyển-đầy-đủ), không coi principal đầu request là T14 hoàn chỉnh.

C3 operator/approval/initial status/Admin recovery/production delivery và C4 storage/cutover/full drill vẫn mở. Không rescue legacy, Admin thứ hai, tự enable held/quarantine, phát summary paper hoặc mở readiness. Không tuyên bố auth toàn hệ thống hoặc restore production an toàn.
