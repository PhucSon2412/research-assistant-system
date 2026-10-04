# Prompt C2c-1: bootstrap Admin bằng runtime và receipt ban đầu

[Mục lục](../README.md) · [Quyết định chia C2c](../decisions.md#chia-c2c-sau-merge-pr-35-2026-10-04) · [C2b](../c2b-reset-status.md) · [C2a](../c2a-login-session-predicates.md) · [Thiết kế auth sau restore](../auth-restore-safety.md)

## Giao việc và base

Bạn là coding agent chính. Bắt đầu triển khai **C2c-1**, hoàn tất code, kiểm chứng, commit/push và mở PR **tiếng Việt** để review; chưa merge. Không dừng ở kế hoạch hoặc xin lại nền C1, Origin và cách chia C2 đã chốt.

PR #35 đã merge tại `1d3e55825901dc81feb892f27a436bbb57b511f2`, chứa bản sửa P2 `f2ba336`. Head migration **mã nguồn** đã quan sát là `0036`; không suy revision DB dev từ đó. Fetch lại và xác minh base mới nhất chứa PR #35 trước khi tạo `feat/m6c2b2c2c1-bootstrap-receipts`. Giữ các docs bàn giao chưa commit của orchestrator, đưa chúng vào PR cùng thay đổi liên quan; không reset hoặc ghi đè thay đổi ngoài task.

Orchestrator chia C2c thành hai PR theo phạm vi được ủy quyền:

1. **C2c-1, task này:** tạo account ban đầu qua boundary runtime hạn chế và chuyển bootstrap Admin một lần sang journal/receipt.
2. **C2c-2, task sau:** chuyển create/reinvite/inspect/accept/revoke invitation và delivery compensation; dùng nền tạo account đã review.

C2c tổng chưa hoàn tất sau PR này. C2d final callers, C3 operator/approval/delivery và C4 cutover vẫn riêng. **Safety pending tới C4**; chưa mở HTTP nghiệp vụ hoặc đổi readiness.

## Đọc trước khi sửa

- `AGENTS.md`, `docs/README.md`, `docs/decisions.md`, `docs/auth-restore-safety.md`, `docs/c1-auth-safety-foundation.md`, `docs/c2a-login-session-predicates.md`, `docs/c2b-reset-status.md`, `docs/invitation-journal.md` và các phần liên quan trong `docs/dev-setup.md`/`docs/testing.md`.
- `api/app/identity/{bootstrap,session_commit,invitations,invitation_endings,passwords}.py`, `api/app/cli/bootstrap_admin.py`, `api/app/{auth_safety,recovery,journal,db_roles}.py`, models identity/auth safety và migrations `0033`–`0036`.
- Test bootstrap lịch sử, foundation/roles/recovery, C2a/C2b và ORM/schema. Lệnh, fixture và số test cũ là chứng cứ lịch sử; không tính thành pass của source mới.

**Mâu thuẫn thực tế cần giải quyết:** bootstrap còn dùng ORM INSERT User/singleton và transaction commit riêng; runtime không được INSERT User. `bootstrap_state` hiện chưa nằm trong tập bảng protected của `db_roles`. Writer cũ chưa tạo initial credential/status receipts. Gate pending đang che các đường này; không được dùng owner/migrator để làm demo bootstrap rồi gọi đó là runtime đã hoạt động.

## Phạm vi

### 1. Routines và schema cho account bootstrap

- Thêm revision mới sau head thực tế, dự kiến `0037`; giữ nguyên mọi revision đã merge. Đồng bộ ORM, provisioning grants và test ORM/schema.
- Tạo routine cố định dưới NOLOGIN definer, quyền tối thiểu, fixed `search_path`, revoke PUBLIC. Runtime chỉ EXECUTE các routine cần thiết; không raw INSERT/UPDATE/DELETE User, receipt/head/mirror hoặc singleton bootstrap, không owner/DDL/TEMP/BYPASSRLS và không chuyển sang migrator.
- Bảo vệ `bootstrap_state` và tính một lần ở boundary SQL phù hợp: không xóa/ghi đè singleton để bootstrap lại, không gắn singleton sang User khác. Recovery role không được tạo User/bootstrap/receipt hay nhận quyền routine creation mới chỉ để replay chạy được.
- Account bootstrap có UUID mới, email chuẩn hóa, role cố định **Admin**, PHC/policy DB hợp lệ; không cho caller chọn tùy ý role, reason, cờ verified hoặc đối tượng User cũ. Không cập nhật account đã tồn tại bằng creation routine.
- Có thể insert trạng thái trung gian quarantine/held **trong transaction** trước journal để chuẩn bị FK, nhưng không commit account do routine bootstrap tạo khi thiếu cặp initial receipt hợp lệ và singleton trong chính transaction đó. Ràng buộc DB phải bắt thiếu một receipt, sai User/domain/generation/reason/time hoặc chỉ có intent không receipt; không dựa riêng vào if trong CLI.
- Nếu chuẩn bị primitive chung cho C2c-2, ghi rõ interface và whitelist nguồn/role; PR này chỉ mở đường bootstrap đã chốt. Không tạo public registration, đường tạo Admin thứ hai hoặc nhận invitation bằng writer cũ.
- Upgrade phải giữ nguyên account legacy, receipt/head/journal/singleton hiện có. Không backfill thành usable/proven từ verifier, role hoặc singleton cũ. Downgrade có lịch sử auth/journal phải bị chặn theo hợp đồng hiện hành.

### 2. Bootstrap writer và transaction

- Chỉ bootstrap cài mới đã có journal hợp lệ: không User, không singleton và không lịch sử chứng minh installation đã có account. Refuse khi journal có **bất kỳ bootstrap intent hoàn tất nào**, kể cả prefix chỉ có credential event, dù snapshot/rollback làm mất User và singleton.
- DB trống sau restore không tự chứng minh cài mới. Kiểm journal độc lập hiện hành; missing/corrupt/ahead/unconfirmed/hold vẫn đóng theo gate thật. Bootstrap không tự init/register/recover journal để hợp thức hóa snapshot.
- Không dùng bootstrap để cứu Admin quarantine/held/disabled, tạo lại User vắng hoặc đổi role. Không thêm `--force`, `--enable`, bypass hoặc cờ xác nhận DB để mở đường này.
- Đọc hash policy DB và đóng transaction đọc trước verify/hash chậm. Validate/hash ngoài khóa bằng Conda/runtime boundary hiện có; không giữ khóa bootstrap/journal trong lúc băm hoặc chờ input.
- Khóa advisory bootstrap hiện hành → chuẩn bị User/singleton/audit và FK → journal advisory cuối. Recheck điều kiện một lần sau khi chờ khóa. Không lấy thêm User/FK/audit row lock sau journal, không nâng shared journal lock lên exclusive.
- Hai `Change` cho cùng UUID User: credential `reason=bootstrap`, status `decision=active, reason=bootstrap`. Operation/generation mới theo toàn chain, không tái dùng UUID đã có của User vắng/transaction rollback. Dùng `commit_auth` và stage/mirror/project/receipt hiện có; không thêm commit/append riêng ở CLI.
- User, verifier, hai receipt, singleton và audit thuộc **một transaction**, hai event cùng **một batch append/fsync**. Flush/kiểm toàn bộ mirror false và binding trước byte đầu; chỉ xác nhận toàn batch khi fsync thành công. Giữ thứ tự credential → status và thứ tự closure cũ nếu có; không cấp gap seq.
- Thành công chỉ sau commit xác định, batch confirmed và kiểm gate/account/receipt/singleton cuối bằng transaction mới đúng lock order. Chọn record theo đúng User/domain từ batch, không suy operation từ `batch[0]` nếu helper xử lý nhiều Change.
- Append none/torn/prefix/full-fsync-error, rollback hoặc commit mất reply tuân bảng lỗi hiện hành. Không coi replay/mirror confirmed là bootstrap thành công; không tự dựng credential, receipt, Admin hoặc singleton từ journal. Báo lỗi khi outcome không rõ, kể cả DB thực sự đã commit.
- Nếu journal đã nhận bootstrap intent nhưng DB rollback mất User, lần bootstrap sau vẫn bị từ chối. Ghi rõ đánh đổi này và trạng thái cần xử lý qua quy trình vận hành được duyệt sau; PR này không có đường force/recreate để cứu installation đó.

### 3. CLI và đầu vào an toàn

- Giữ lệnh `python -m app.cli.bootstrap_admin`, getpass nhập hai lần không echo và `--password-stdin` **đã có**; không áp nhầm hạn chế riêng của CLI recovery C3 để bỏ contract bootstrap hiện hành.
- Không mật khẩu mặc định/hardcode, argv/env hoặc log. Thiếu TTY an toàn mà không có stdin mode thì từ chối; không fallback sang nhập có echo. Sai password/confirmation không tạo account.
- Giữ argparse không echo đối số lạ, gồm gõ nhầm `--password <secret>`, viết tắt hay `--password=<secret>`. Journal/audit/log/exception/repr không chứa password, PHC/verifier, email hoặc payload riêng ngoài allowlist hiện hành. PHC chỉ ở bảng credential; không tạo fingerprint verifier trong journal.
- CLI dùng đúng runtime connection/provisioned role; không fallback migrator/owner khi thiếu quyền. Bootstrap không phát session/cookie/link. Exit 0/stdout thành công chỉ sau các hậu kiểm; phân biệt input invalid, refused và failure/uncertain theo contract được ghi lại.
- Origin không áp cho CLI; mọi HTTP auth route vẫn giữ Origin/CSRF hiện hành. Không thêm endpoint bootstrap hoặc thay delivery provider.

## Kiểm thử bắt buộc

Chỉ dùng PostgreSQL/journal/Compose project và dữ liệu tổng hợp **cô lập**, explicit test admin URL trước pytest, runtime LOGIN thật. Python/cài thư viện dùng `C:\Users\ADMIN\.conda\envs\ras`; xác minh interpreter/pip, không venv. Không đọc/sửa `.env`, DB/journal hoặc container/volume dev, không gọi OpenRouter hay thay `pricing_verified`.

Fixture writer chỉ được thay kết quả `auth_safety.cutover` tại boundary pending từng test như C2a/C2b. Giữ integrity, hold, routine, receipt, actor/ACL, journal và commit thật; không thêm bypass production. Nhóm HTTP/CLI pending riêng **không patch**: health sống, readiness/HTTP được bảo vệ và bootstrap pending vẫn đóng. Không chữa `docker compose up --wait` bằng readiness giả hoặc đổi gate.

| Nhóm | Điều kiện và kết quả cần kiểm |
| --- | --- |
| Positive | Cài mới hợp lệ → đúng một Admin/singleton, hai initial receipt/journal binding hợp lệ; account live và login trực tiếp hợp lệ sau fixture cutover. `_initial_status_verified` C2b nhận receipt bootstrap thật; CLI không tự issue session |
| Một lần/concurrency | Bất kỳ User/singleton/lịch sử account có trước → refuse; nhiều bootstrap đồng thời chỉ một thành công. Dùng barrier/quan sát khóa, không chỉ sleep |
| Runtime/SQL | Runtime raw protected writes và đổi singleton bị từ chối; recovery không EXEC creation. Direct routine thiếu/null/sai UUID/PHC/policy, receipt một phía hoặc sai binding không commit/append được; không để User trung gian mồ côi |
| Hash/lock/final gate | Không transaction/khóa trong lúc Argon2; journal/hold thay đổi trong lúc hash hoặc chờ bootstrap lock → từ chối. Kiểm entry và final integrity trên đúng transaction, không deadlock |
| Fault matrix | Append chưa ghi/torn/prefix/dòng đủ nhưng fsync lỗi × commit xác định/rollback/mất reply. Toàn batch false khi sync lỗi; không success/exit 0 khi uncertain; inspect/recover chỉ giữ hoặc hạn chế theo receipt thật |
| Restore/absent/retry | Snapshot trước bootstrap + journal mới; recover hai lần không tạo User/verifier/receipt/singleton. Credential prefix `bootstrap` một mình cũng chặn bootstrap lại; không dựa riêng vào status event, DB mirror hoặc singleton |
| CLI/secret | Interactive/pseudo-TTY, stdin, thiếu TTY, mismatch/weak/empty/overlimit, argv typo; không echo. Quét journal/log/audit/stdout/stderr với marker password/verifier; DB verifier không là leak ngoài bảng auth |
| Schema/hồi quy | Upgrade từ `0036` có dữ liệu giữ rows/journal bytes và grants; pristine round-trip nếu được cho phép, downgrade history từ chối; ORM/schema. C1 binding/replay/pending và C2a/C2b, đặc biệt P2 initial status không bị nới |

- Chạy CLI thật trên project/DB cô lập với đúng runtime, không chỉ gọi helper bằng owner. Chuẩn bị journal/schema/provisioning bằng migrator trong bước setup tách biệt rồi kiểm bằng runtime. Smoke chưa ready chỉ chứng minh pending; full proxy/cutover hoãn C4.
- Một vài đối chứng có ý nghĩa cho journal-history guard, cặp receipt và quyền singleton: chạy tuần tự, khôi phục đúng bytes trước suite cuối. Không sửa source trong lúc suite đang chạy.
- Chạy đủ nhóm liên quan; nếu full inventory nghiệp vụ còn hoãn C4, báo node pass/fail/not-run và lý do. Không gọi mọi response 503 là nghiệp vụ đã pass, không tính số cũ thành pass mới, không sửa expectation an toàn để xanh.
- Không cần web suite/build khi web không thay đổi; ghi rõ. Không đặt số hiệu năng mới hoặc tuyên bố durability mất điện từ fault injection.

## Docs và bàn giao

Cập nhật trạng thái PR #35 **đã merge** trong docs cần thiết; giữ lịch sử số đo tách source. Tạo tài liệu bàn giao C2c-1: chữ ký routine/helper, creation/receipt/singleton constraints, grants runtime/recovery/definer, lock order, CLI exit codes, bảng cửa sổ lỗi và các caller chưa chuyển.

C2c-2 cần tiếp nhận cách chuẩn bị User mới/FK trước journal, initial credential/status reason `invitation_accept` và một batch cùng accepted closure; không được dùng `admin_enable` làm initial proof. Create/reinvite/revoke cần kiểm Admin session/role/CSRF cuối mà không tạo chu trình User → email → User; hash/delivery ngoài khóa. Compensation không đảo accepted hoặc xóa account đã tạo. Ghi hợp đồng này, **chưa triển khai các writer invitation ở PR này**.

Giữ AGENTS ngắn, chỉ thêm lệnh đã chạy thật và link bàn giao; không biến chữ ký/routine đề xuất chưa triển khai thành bất biến chung. Các quyền operator/approval/status recovery C3, journal production/cutover C4 vẫn chưa được quyết định trong task này.

## Tiêu chí hoàn thành và báo cáo PR

PR kiểm chứng được việc bootstrap qua runtime tạo một Admin hợp lệ với cặp initial receipt và singleton, không thể bootstrap lại từ snapshot thiếu account khi journal đã ghi intent. Binding/grants, rollback/uncertain/fsync/replay và các test hồi quy liên quan đạt; safety vẫn pending. Không gọi C2c hoặc toàn auth đã hoàn chỉnh.

Báo base/commit/source migration, file thay đổi, lệnh thực chạy và inventory pass/fail/not-run, đối chứng và giới hạn. Phân biệt kiểm trực tiếp/scoped fixture với full HTTP/cutover; chưa đo/không chạy phải ghi rõ. Dọn đúng tài nguyên thử sau xác minh project/volume ownership. Commit không thêm AI vào author/committer/trailer; mở PR tiếng Việt cho người review, chưa merge.
