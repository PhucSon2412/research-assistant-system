# Prompt C2c-2: invitation qua runtime, initial receipt và kiểm quyền cuối

[Mục lục](../README.md) · [Quyết định giao việc](../decisions.md#giao-c2c-2-sau-merge-pr-36-2026-10-04) · [Bàn giao bootstrap](../c2c1-bootstrap-receipts.md) · [Journal invitation](../invitation-journal.md) · [Thiết kế auth sau restore](../auth-restore-safety.md)

## Giao việc và base

Bạn là coding agent chính. Triển khai **C2c-2**, kiểm chứng, commit/push và mở PR **tiếng Việt** để review; chưa merge. Hoàn tất phần triển khai được giao, không chỉ trả một kế hoạch. Nền C1, Origin và cách chia C2/C2c đã được orchestrator chốt theo ủy quyền; không xin lại các quyết định này.

PR #36 đã merge tại `d1b53e653197af0d11f79cbdda0df514cb0af69a`, chứa `d222fb1`/`48137e4`. Head migration **mã nguồn** đã quan sát là `0038`; không suy revision DB dev. Fetch lại, kiểm ancestor và head thực tế trước khi tạo nhánh `feat/m6c2b2c2c2-invitation-runtime` từ main mới nhất. Thêm revision sau head thực tế; không sửa các revision đã merge, đặc biệt `0033`–`0038`.

Giữ các docs chưa commit của orchestrator, gồm review PR #36 và prompt này; đọc diff, đưa chúng vào PR cùng các cập nhật liên quan. Không reset hoặc ghi đè thay đổi ngoài task. C2c-1 đã merge không có nghĩa toàn bộ auth đã chuyển: C2d final callers, C3 operator/delivery/status recovery và C4 cutover còn riêng.

**Safety vẫn pending tới C4.** Chỉ thử cô lập. Không đọc/sửa `.env`, truy vấn/nâng/restart stack dev, thay healthcheck để làm Compose healthy, mở traffic hoặc thêm bypass production. Không gọi OpenRouter/nguồn bên ngoài hay đổi `pricing_verified`. Python và pip dùng Conda `C:\Users\ADMIN\.conda\envs\ras`; xác minh interpreter/pip trước khi chạy/cài, không tạo venv.

## Tài liệu và source phải đọc

- [AGENTS.md](../../AGENTS.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2c-1](../c2c1-bootstrap-receipts.md), [review PR #36](../reviews/pr36-c2c1-bootstrap.md), [journal invitation](../invitation-journal.md) và các test inventory liên quan.
- `api/app/identity/invitations.py`, `invitation_endings.py`, `session_commit.py`, `users.py`, `passwords.py`; `app/api/routes/admin.py`, `auth.py`, `api/deps.py`; `auth_safety.py`, `journal.py`, `recovery.py`, `db_roles.py`, `delivery.py`, models và SQL migration hiện hành.
- Kiểm toàn bộ caller/test/script đang tạo, kết thúc hoặc replay invitation, không chỉ route HTTP. Đọc fixture để phân biệt quyền migrator dùng chuẩn bị dữ liệu với runtime/recovery thật đang được kiểm.

## Phạm vi PR

Chuyển **create/reinvite/list/inspect/accept/revoke invitation và delivery compensation** sang hợp đồng auth mới. Accept tạo **User mới, role `user` cố định**, initial credential/status receipt và accepted closure trong cùng transaction/batch. Chuyển các routine/grants/replay cần thiết để invitation được bảo vệ ở DB; bổ sung Admin actor/session/CSRF và đồng hồ mới ở các lần kiểm cuối.

Giữ hành vi sản phẩm: invitation một lần, TTL cấu hình 72 giờ; hash token trong DB; mời lại kết thúc link cũ; accept không tự đăng nhập; không cấp Admin qua invitation. Giữ Origin/CSRF và thông điệp lỗi hiện hành, chỉ sửa khi hành vi thực tế cần phản ánh outcome chưa xác định. Chưa chọn kênh gửi email thật: adapter tắt mặc định và Dev outbox chỉ dùng development như trước.

Không triển khai rescue/verify User legacy, cấp Admin thứ hai, final checks cho mọi resource route, production delivery, cutover hoặc storage journal production. Nếu phần dùng chung phải thay để invitation hoạt động, giải thích phạm vi ảnh hưởng và kiểm hồi quy phần đó.

## Mâu thuẫn đã khảo sát cần sửa

1. Accept hiện INSERT User trực tiếp, không có initial receipt; runtime mới không được raw write User được bảo vệ. Không tái dùng bootstrap prepare: routine đó chỉ dành Admin đầu tiên trên installation `new_install`.
2. Invitation chưa thuộc tập protected của `db_roles.py`; cả writer và replay còn ghi ORM trực tiếp. Thay writer mà quên grant/replay sẽ tạo đường vòng hoặc làm recover lỗi.
3. Admin invitation chỉ kiểm ở dependency đầu request; helper chưa nhận actor session/CSRF/clock để kiểm trước commit, delivery và response. List cũng thiếu kiểm cuối.
4. `hash_password` hiện chạy trước rollback transaction đọc ban đầu. Đóng transaction trước Argon2, rồi đọc lại dưới khóa và kiểm TTL bằng clock mới; không chỉ nói “ngoài khóa”.
5. Bù `delivery_failed` hiện dùng cùng helper Admin revoke. Khi actor mất hiệu lực trong lúc gửi, vẫn cần kết thúc an toàn đúng link đã phát; không được biến cờ `compensation=True`/reason do caller chọn thành quyền bỏ qua Admin.

Ghi quyết định triển khai và mọi khác biệt mới vào docs cùng code. Những yêu cầu dưới đây là phạm vi orchestrator giao theo ủy quyền, không ghi rằng chủ dự án trực tiếp chọn tên routine hoặc chi tiết SQL.

## 1. Boundary DB và initial receipt

- Bảo vệ bảng invitation trong provisioning và upgrade cho các role đã provision: runtime không raw INSERT/UPDATE/DELETE/TRUNCATE hoặc sở hữu bảng/routine; không TEMP/DDL/BYPASSRLS, không fallback owner/migrator. Routine definer NOLOGIN, fixed `search_path`, EXECUTE theo allowlist, không PUBLIC; cấp quyền tối thiểu.
- Runtime chỉ tạo invitation và kết thúc nó qua routine có phạm vi rõ. Recovery chỉ được áp closure hạn chế/backfill đúng hợp đồng; không được tạo account, receipt, verifier, invitation mới hoặc status active. Chuyển replay hiện hành sang boundary phù hợp, vẫn hỗ trợ `hold` trên snapshot cũ chỉ đọc seq/hash.
- Routine chuẩn bị accept dành riêng **invitation còn hợp lệ**: kiểm đúng ID/token hash/email, expiry, trạng thái mở và account chưa tồn tại; tạo User UUID mới với role `user`, PHC/hash policy hợp lệ. Không nhận tùy chọn role Admin, không thay verifier/status của User có sẵn hoặc giải phóng quarantine/held.
- Ràng buộc DB NULL-safe buộc User mới, invitation/accepted FK, hai initial receipt `reason=invitation_accept`, operation/generation/domain/time, accepted closure `reason=invitation_accepted` và audit khớp đúng đối tượng/transaction. Cặp receipt phải được kiểm đủ trước append; thiếu/mismatch/null không được để journal nhận intent thành công rồi mới phát hiện khi commit. Chọn cách biểu diễn binding/marker phù hợp và giải thích, không chỉ thêm guard Python.
- Hai Change theo credential → status, rồi invitation closure, qua **một `commit_auth`**. Flush User/FK/audit/mirror trước journal. Toàn batch mirror false trước byte đầu, toàn batch chỉ confirm sau append/fsync thành công. Không journal email/tên/token/hash token/PHC/password; không thêm fingerprint verifier.
- Không nới `0038` để accept invitation: invitation hợp lệ phải dùng được trên installation đã có tài khoản; bootstrap new-install guard/singleton/history vẫn giữ nguyên.
- Upgrade giữ rows/history/grants hợp lệ, không tự xác minh legacy. Downgrade không được xóa bảo vệ trên DB đã đăng ký/có history. Giữ ORM/schema test xanh; kiểm quyền bằng runtime/recovery LOGIN thật, không coi migrator test là bằng chứng runtime đúng.

## 2. Khóa, clock và accept

- Vẽ lock map cho live writers và recovery/backfill trước khi sửa. Nhánh Admin gather/khóa các **User hiện có** cần thiết theo UUID `FOR NO KEY UPDATE` trước email advisory → invitation UUID → FK/audit trước journal. Không lấy User auth lock sau email/invitation/journal. Nhánh accept ẩn danh: email advisory → invitation → **User mới**/FK/audit → journal; không quay lại khóa User hiện có.
- Giữ thứ tự reset/session của C2b. Replay/backfill đang có thứ tự email → User và contract dừng writer: chứng minh sự cách ly bởi hold/gate hoặc điều chỉnh thứ tự phù hợp; không âm thầm để recovery chạy cùng writer với thứ tự đảo. Journal advisory luôn sau khóa hàng, không upgrade shared sang exclusive.
- Lookup ban đầu chỉ lấy dữ liệu cần cho validation/hash, rồi rollback/đóng transaction trước Argon2. Dùng DB hash policy/trusted hasher như C2a/C2c-1; đọc lại policy, token binding, trạng thái và TTL sau băm/chờ khóa. Không giữ transaction/khóa khi hash hay delivery.
- Clock callable mới ở kiểm dưới khóa, ngay trước commit theo guard của batch và hậu kiểm. Đúng biên `now >= expires_at` thì từ chối; thời gian lúc dependency/lookup không là thời gian cuối. Token không được hồi sinh do ORM cache hoặc reload nhầm ID/hash.
- Known commit mới được báo accept 201; hậu kiểm đúng hai operation/receipt/head và đúng accepted User của invitation. Đừng lấy `batch[0]` rồi mặc định đó là event cần kiểm. User mới phải thỏa predicate login và initial status proof của C2b; không dùng `admin_enable` làm initial proof.
- Một token accept đồng thời chỉ tạo một account. Accept thua reinvite/revoke/recovery closure không tạo account/receipt/session. Account đã tồn tại cùng email trả lỗi phù hợp, không sửa account. Không tái dùng UUID/generation đã có trên toàn journal, kể cả intent rollback hoặc User absent.

## 3. Admin, inspect và ranh giới trả kết quả

- Create/reinvite/revoke kiểm actor session đúng owner, generation, latest confirmed credential/status head + receipt, role Admin, CSRF và idle/absolute expiry bằng clock mới trước mutation/commit; kiểm lại sau known commit trước response. Reuse cơ chế actor của C2a/C2b, không thêm khóa User sau journal.
- Create lần đầu không có closure cũ vẫn phải kiểm writer/integrity/actor và known commit trước delivery; batch rỗng không là bypass. Gửi Admin ID hoặc `admin_required` dependency một lần không đủ.
- List invitation kiểm lại principal dưới journal shared lock cuối trước serialize, bằng clock mới; session/role mất hiệu lực không trả danh sách. Public inspect kiểm token/TTL/trạng thái/gate cuối trước lộ email/name. Không cần session Admin cho người giữ invite token; giữ Origin/CSRF theo route hiện hành.
- Chứng minh session hết hạn hoặc bị revoke, User disable/credential generation đổi, actor role đổi và CSRF sai sau dependency nhưng trước commit/response đều đóng quyền. Giữ lỗi 401/403/token không hợp lệ theo hợp đồng; không lộ token/PHC trong traceback/repr/log SQL kể cả DEBUG.

## 4. Delivery và bù an toàn

**Quyết định cho task:** tách Admin revoke khỏi bù hệ thống, bù chỉ đóng **đúng invitation vừa có known commit** trong luồng delivery. Context bù do backend tạo và kiểm binding của chính lượt phát link; không nhận quyền bù/reason tùy ý từ request. Không được dùng nó để tạo invitation/account, đổi quyết định đã đóng hoặc cấp quyền. Audit dùng actor/mã phù hợp, không chứa payload gửi.

- Trước adapter: kiểm lại integrity, actor, invitation còn mở và TTL bằng clock mới; kết thúc transaction đọc/kiểm rồi mới I/O. Không giữ row/journal lock hoặc transaction trong `delivery.send`.
- Không gọi adapter khi commit không xác định, batch chưa confirm, actor đã mất hiệu lực hoặc link không còn dùng được. Khi cần bù và link còn mở, dùng batch closure `delivery_failed`; actor mất session không được khiến bù bỏ dở chỉ vì thiếu quyền Admin ban đầu. Gate/storage/journal lỗi vẫn xử lý fail closed và báo đúng outcome, không bỏ lỗi bù.
- Adapter có thể gửi rồi báo lỗi. Bù lấy lại email/invitation lock và giữ mọi closure đã thắng: accept thắng thì giữ User/receipt/accepted, không đảo thành revoked/xóa account; revoke/reinvite/recovery đã thắng thì giữ nguyên. API không hứa “chưa gửi” hoặc báo delivery thành công khi outcome chưa xác định.
- Sau adapter, kiểm lại actor/gate trước response thành công; status trả về phản ánh trạng thái đã đọc mới, không dùng ORM pending cũ. Nếu actor mất hiệu lực thì không trả thành công, bù chỉ khi link vẫn mở; không đảo accept đã thắng.
- Ghi rõ giới hạn: đổi quyền/expiry sau lần kiểm cuối trước adapter không thu hồi được message đã gửi. Không giữ khóa qua adapter để hứa loại bỏ mọi khe đua. Test cả trước adapter, trong adapter và sau adapter; lỗi trả về không chứng minh message chưa tới người nhận.

## 5. Fault, replay và restore

Giữ hợp đồng C1/commit auth hiện có: journal có intent không chứng minh User/credential/status/delivery đã commit. Commit rollback/mất phản hồi luôn trả lỗi; mixed auth batch không được replay tích cực để giả lập accept thành công. Không sinh User/verifier/receipt/active trong recovery.

Kiểm accept batch ba event: credential, status, accepted closure. Fault gồm chưa ghi, torn, prefix sau từng event, full append nhưng fsync lỗi, commit rollback và commit thực đã xảy ra nhưng mất phản hồi. Toàn batch mirror false khi append/fsync lỗi; gate đóng tới recover đủ hậu điều kiện. Chứng cứ prefix chỉ áp được quyết định **thực sự có dòng hoàn chỉnh**: prefix chưa có invitation closure không chứng minh link đã đóng. Không mở lại link có closure bền, cũng không tự đóng mọi link từ credential intent không gắn invitation; nêu rõ giới hạn và retry hợp lệ.

Restore snapshot lúc invitation mở/chưa có User, giữ journal hiện hành: accepted closure đầy đủ replay thành `closed_by_recovery`, không dựng account/accepted FK; không nhận lại link. Nếu account/receipt thật còn trong snapshot, phải kiểm latest head/receipt trước usable/active, không đoán từ closure. User absent/old generation/legacy giữ cách ly theo C1. Recover/restore lần hai idempotent; lịch sử mất khỏi cả DB và journal không backfill được.

## 6. Kiểm chứng bắt buộc

| Nhóm | Ca và chứng cứ tối thiểu |
| --- | --- |
| Runtime/DB | Raw invitation/User writes bị từ chối; routine gọi trực tiếp bằng runtime; wrong/null/missing binding, wrong token/role/receipt/domain/generation, thiếu một receipt, mismatch accepted User/audit, UUID/history reuse; recovery không có creation grant; các ca dương không bị khóa nhầm |
| Accept hợp lệ | User mới role user, hai receipt `invitation_accept`, accepted closure/audit một batch; login C2a thật, initial proof C2b thật; disable/enable không vượt guard; bootstrap `0038` giữ nguyên trên new-install/existing |
| Clock/actor | Biên TTL 72 giờ và idle/absolute, sau hash/chờ khóa/list/inspect/delivery; session revoke/disable/generation/role/CSRF thay đổi; không payload/link thành công khi bị chặn |
| Race | Accept × accept/reinvite/revoke/bù; reinvite × reinvite; Admin mất quyền; recovery/hold; dùng barrier/lock wait tất định, không dựa chỉ sleep; kiểm không deadlock hoặc mutation sau lần kiểm thất bại |
| Delivery | Disabled, known commit, unknown commit, adapter gửi rồi lỗi, actor hết hạn khi gửi; bù khi actor mất quyền, accept thắng giữ account; không transaction/row/journal lock khi adapter bị chặn |
| Fault/restore | Prefix/torn/fsync/commit matrix ở trên; snapshot cũ trước accept, User absent; hai lượt recover/restore; giữ năm domain journal cũ và C1–C2b/bootstrap |
| Migration | Upgrade populated/provisioned từ `0038`, grants/owner/schema không drift, pending không mở; downgrade history fence và `hold` snapshot cũ |
| Bảo mật | Password/raw token/token hash/PHC/email/name không vào journal/audit/log/error ngoài allowlist; token hash chỉ ở auth table hợp lệ; không HTTP ra ngoài, không dữ liệu dev |

Chạy nhóm liên quan trên PostgreSQL/journal và role runtime/recovery **thật**, cấu hình test explicit trước pytest để fixture không fallback `.env`. Dữ liệu tổng hợp và project/container/volume/ports riêng; kiểm ownership trước cleanup. Không cần MinIO nếu phép thử không dùng object; drill restore dùng storage cô lập nếu cần, không đổi journal nguồn.

Test nghiệp vụ scoped dùng fixture C2 đúng boundary: chỉ thay cutover pending ở fixture thử, không mock receipt/latest head, integrity, writer, routine, actor hoặc clock guard cần kiểm. Giữ expectation nghiệp vụ của test cũ; không chuyển hàng loạt thành 503 để gọi là hồi quy pass. Test pending riêng với middleware/gate thật: health sống, readiness/HTTP nghiệp vụ/task bị đóng. Full HTTP/cutover toàn sản phẩm dành C4, không bỏ qua test caller invitation đã chuyển trong PR này.

Thêm đối chứng cho DB binding/grant, initial pair, final actor/TTL và bù/race; đưa từng lỗi vào riêng, chứng minh test fail rồi khôi phục bytes **trước** suite cuối. Không sửa source khi suite đang chạy. Thu inventory/JUnit theo node/source; phân biệt pass/fail/error/skip/not-run và các lượt lịch sử, không cộng lượt chọn trùng nhau thành full suite.

Nếu sửa web: chạy test/typecheck/build và smoke form invitation qua proxy với fixture cô lập, giữ backend auth thật ở boundary đã nêu. Nếu không sửa web, ghi không chạy lại và lý do. Test API/proxy thành công trong fixture không được gọi là stack dev hoặc production đã mở. Không biến số fixture/fault injection thành SLO hoặc bằng chứng bền khi mất điện.

## Đầu ra và tiêu chí hoàn thành

1. Migration mới + ORM/provisioning/routines; invitation runtime, initial pair và journal/recovery đúng hợp đồng. Không caller invitation active nào còn raw protected writes/fallback owner.
2. Test các ca trên và hồi quy liên quan đạt; fixture/race giữ bất biến, source cuối có JUnit/inventory kiểm được. Pending nguyên vẹn; không truy cập dev/secret/provider.
3. Thêm `docs/c2c2-invitation-runtime.md` và inventory: API/helper/routine, boundary/hash/delivery, lock order, fault outcomes, grant/provision/upgrade, số thực chạy và giới hạn. Cập nhật decisions/roadmap/README/dev-setup/testing/AGENTS liên quan; AGENTS dẫn tới hợp đồng, không chép toàn spec hoặc ghi lệnh chưa chạy.
4. Giữ review PR #36 của orchestrator trong PR; đánh dấu #36 merged, C2c-2 đang review khi mở PR. Bàn giao **C2d final callers/integration** bằng danh sách caller chưa chuyển, cùng C3/C4 còn mở. Không tuyên bố auth toàn hệ thống/restore production an toàn.
5. Compile/diff/link check sạch; commit không AI author/committer/trailer, không secret/artifact/journal trong repo. Push và mở PR tiếng Việt với base/HEAD/revision, file đổi, bảng pass/fail/not-run, source của từng phép đo, lỗi gặp và giới hạn; chưa merge.

Khi báo cáo, nêu rõ invitation có thể đã được gửi/account có thể đã commit dù response lỗi trong cửa sổ nào; tránh thông điệp “chưa có gì được ghi nhận” khi không chứng minh được. Operator/bằng chứng/initial status Admin/delivery production trước C3, journal storage/cutover trước production vẫn chưa chốt và không cản task runtime cô lập này.
