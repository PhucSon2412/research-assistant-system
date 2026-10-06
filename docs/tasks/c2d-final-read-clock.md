# Prompt: sửa final clock của read caller sau SQL

**Trạng thái:** đã merge PR #42 tại `99db5285dfa59dd3d26e86345e849393ed88ce33` (2026-10-06); [handoff và kết quả](../c2d-final-read-clock.md). Giữ nội dung giao dưới đây làm lịch sử yêu cầu ngày 2026-10-06. Task tiếp [C2d-4a notice/intent](m6c2b2c2d4a-upload-notice-intent.md) đã giao, chưa triển khai. PR #41 đã merge `5100ff67c5c61754e4dd29693cb670a22ce6cf83`. Source migration `0039`; safety **pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-pr-sửa-final-clock-sau-merge-pr-41-2026-10-06) · [P2 và chứng cứ](../reviews/pr41-c2d3-resource-writes.md#p2-ở-helper-đọc-clock-trước-predicate-sql) · [Bàn giao C2d-1](../c2d1-final-caller-reads.md) · [Roadmap](../roadmap.md)

Bạn là coding agent chính. Hoàn thành **một PR sửa hẹp cho final read clock**, gồm code, test, docs, commit/push và mở PR **tiếng Việt** để review; không merge. Sửa lỗi đã tái hiện trước khi tiếp tục nhóm writer/file/provider còn lại của C2d.

## 1. Base, workspace và nguồn yêu cầu

- Fetch lại, xác minh PR #41 MERGED và main chứa head `1bad143f439c69b4163baad23b7c54fac04aead5`. Lượt giao xác minh main/merge `5100ff67c5c61754e4dd29693cb670a22ce6cf83`. Nếu main tiến thêm, dùng base thực tế và báo khác biệt.
- Tạo nhánh `fix/c2d-final-read-clock` từ main đã xác minh. Workspace hiện ở nhánh C2d-3 với **docs review/task chưa commit**, gồm file review chưa tracked: kiểm diff, giữ và đưa nội dung hợp lệ vào PR này. Không reset/clean, bỏ docs hoặc checkout làm mất thay đổi. Nếu phải chuyển worktree, chuyển đủ docs và kiểm diff.
- Head migration mã nguồn hiện `0039`, không là chứng cứ revision DB dev. Task này **không cần migration** hoặc thay quyền runtime/migrator.
- Đọc [AGENTS.md](../../AGENTS.md), [quyền và dữ liệu](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2d-1](../c2d1-final-caller-reads.md), [C2d-2a](../c2d2a-resource-json-reads.md), [C2d-2b](../c2d2b-review-publication-json-reads.md), [C2d-3](../c2d3-library-collection-json-writes.md), [caller inventory](../c2d1-caller-inventory.json) và [testing](../testing.md).
- Đọc code thật: `api/app/identity/final_caller.py`, `identity/predicates.py`, `auth_safety.py`, `api/resource_reads.py`, `api/review_publication_reads.py`, `api/resource_writes.py`, các caller trực tiếp ở route auth/admin và test tương ứng.
- Trước sửa, báo phạm vi và mâu thuẫn docs/code. Nền C1/Origin đã chốt, không xin lại. Safety pending là trạng thái chủ đích; không mở gate để làm Compose healthy.

## 2. Lỗi và hợp đồng cần đạt

**P2 đã tái hiện:** GET `/collections` trả `200` khi SQL predicate session cuối hoàn tất đúng biên idle expiry, nơi hợp đồng yêu cầu `401 session_expired`.

Trong `final_caller`, clock được đọc sau journal/profile query nhưng **trước** `session_live`. Predicate nhận thời gian còn hợp lệ, có thể hoàn tất khi session đã hết hạn. Proof vẫn mang `checked_at` cũ. Các boundary resource/review gọi helper ở cuối chưa giải quyết latency của chính predicate ấy. Helper không đổi trong PR #41; writer `_actor` mới đã kiểm clock sau SQL.

Hợp đồng sửa:

1. Giữ integrity/recovery gate thật và journal shared cùng transaction; exact session ID + User ID, latest confirmed heads/receipts, credential generation, status active/proven, revoked state và current scalar profile/role.
2. **Sau SQL predicate cuối cùng có thể chậm**, lấy clock mới và kiểm lại idle/absolute expiry. Tại hoặc quá hạn trả `401 session_expired`, clear session theo contract hiện hành, không phát JSON/link của response đó. Sát trước hạn vẫn thành công.
3. Proof thành công có `checked_at` là mốc sau SQL kiểm quyền cuối; `idle_expires_at` vẫn capped bởi absolute expiry. Nếu thêm query để lấy dữ liệu TTL, clock cuối phải nằm sau query ấy. Không chỉ gọi lại predicate với một mốc rồi lại bỏ qua latency của query mới.
4. Không gia hạn `last_activity_at` trong helper, không thêm User/session/resource row lock sau journal, không thêm commit/rollback hoặc writer exclusive vào read helper. Proof không đi qua commit.
5. Current Admin role vẫn kiểm riêng, sai role trả `403`; ordinary ownership, review exact grant, thin closed history, pending/common-copy history là các quyền khác nhau. Không mở quyền private cho Admin, không đổi SQL ACL trước count/page/top-k.
6. Không hứa khóa mọi trạng thái mãi sau response boundary: response đã qua kiểm cuối và nhả khóa vẫn có thể tiếp tục gửi. Bytes đã gửi không thu hồi được.

Ưu tiên sửa trong shared helper; chỉ sửa boundary/caller khi có bằng chứng cần thiết để thỏa hợp đồng. Không thay API shape, expiry config, ranking, ingest, journal/receipts/grants hoặc semantics mutation đã commit. Giữ định nghĩa idle/absolute theo config, không hardcode 30 phút/8 giờ trong logic.

## 3. Caller và phạm vi hồi quy

Lập bảng đối chiếu mọi caller thật của helper; không chỉ kiểm GET collections:

- Ba caller C2d-1: `GET /auth/session`, `GET /admin/users`, `GET /admin/dev-outbox`; DevOutbox có commit audit và proof mới sau commit, rồi kiểm lại trước phát link/TTL.
- 13 GET resource C2d-2a qua `resource_json`.
- 9 GET review/publication C2d-2b qua `review_publication_json`, gồm empty pages và current Admin.
- JSON response sau known commit của C2d-3 tái dùng `resource_json`; giữ actor/CSRF hậu kiểm và proof mới. Không dùng sửa read helper làm lý do bỏ `_actor` post-SQL trước commit.
- Caller liên quan như `GET /auth/sessions` có final predicate riêng: đối chiếu clock sau SQL đã có và ghi kết quả; chỉ sửa nếu tái hiện cùng lỗi trong boundary đã chuyển. Writer/file/provider chưa chuyển vẫn thuộc task khác.

Phân biệt các giai đoạn: read denial không trả dữ liệu; trước business commit thì rollback mutation/audit thuộc transaction đó; sau known commit thì lỗi response không chứng minh mutation/audit chưa lưu. Không replay/retry metadata hoặc mở token/session từ journal.

## 4. Kiểm chứng bắt buộc

### Ca tái hiện trước sửa

Chuyển ca đã tái hiện ngoài repo thành regression trong repo. Evidence tham khảo `%TEMP%/ras-pr41-review-20261005/test_existing_read_clock.py`, `read-clock.xml/log` và report review. File tạm không phải dependency của suite.

- Bootstrap/invitation/login bằng runtime thật, PostgreSQL thật và journal/head/receipt/predicate thật.
- Baseline GET collections `200`; wrapper gọi **session_live SQL thật**, chỉ mô phỏng latency bằng cách tăng fake clock sau khi query thật trả kết quả ở lần kiểm cuối. Trước sửa phải fail đúng `200` thay vì `401`, không fail setup.
- Dùng hook/Event rõ giai đoạn; không dựa sleep hoặc số lần gọi clock cố định. Không thay predicate trả boolean, fake principal/receipt/head hoặc production bypass để làm test pass.

### Sau sửa

- **Idle và absolute:** đúng biên, vượt biên và sát trước hạn; cấu hình idle khác mặc định, absolute tới trước idle; expiry trong profile/resource/journal wait và trong chính predicate SQL cuối.
- **Proof:** checked_at sau predicate; timestamps/expiry đúng, không touch activity hay commit helper, không stale identity map; helper direct với exact session/User sai binding, revoked/disabled/quarantine/held/missing receipt vẫn đóng.
- **HTTP:** mỗi caller C2d-1; ít nhất route list/detail/count/empty page đại diện C2d-2a và cả review/common boundary C2d-2b. Test Admin không nhận payload/link khi mất authority ở cuối. Chạy các suite C2d-1/2a/2b để giữ toàn contract của nhóm đã chuyển.
- **DevOutbox:** link hết hạn trong latency predicate cuối không lọt; kiểm clock mới sau known audit commit, không dùng proof trước commit cho response. Log/JUnit không ghi token/link bí mật.
- **C2d-3:** nhóm affected response/clock/ACL/commit uncertain; caller hết hạn sau known commit không nhận DTO, mutation đã lưu không bị ghi lại/rollback giả. Ca trước commit vẫn rollback mutation; bảo vệ post-SQL của writer không bị bỏ.
- **Gates/transactions:** nhóm pending HTTP không patch vẫn `503`, health/readiness giữ contract; shared journal không nâng lock, không có khóa User/session mới, transaction/pool sạch trên success/denial/error.
- **Hồi quy nền:** login/session predicates và ORM/schema; các trường hợp quyền/expiry positive và negative. Chỉ broaden thêm khi có risk cụ thể.

Fixture nghiệp vụ trước C4 chỉ được scoped patch cutover pending theo hợp đồng sẵn có. Không mock integrity, journal, auth SQL hoặc resource rights. Giữ pending HTTP riêng không patch.

**Đối chứng:** tạm phục hồi clock trước predicate hoặc bỏ post-SQL expiry check, regression idle/absolute phải fail đúng assertion; khôi phục bytes/hash trước suite cuối. Các mutation này chạy riêng, không sửa source lúc suite đang chạy. Không đếm lỗi setup là guard được bắt.

Thu thập inventory toàn API hiện tại, chọn suite theo ảnh hưởng, JUnit unique/matching selection và SHA source trước/sau. Báo pass/fail/error/skip/not-run theo node; lịch sử 270/1095 của PR #41 không là kết quả task này. Không cần chạy toàn bộ inventory để gọi task hoàn tất; phải giải thích các nhóm chưa chạy và giới hạn.

Nếu không đổi web/proxy/Compose, ghi không chạy lại và lý do. Nếu phải đổi, chạy test/typecheck/build hoặc smoke tương ứng. Không gọi provider thật.

## 5. Môi trường và cleanup

- Python/cài thư viện chỉ qua `C:\Users\ADMIN\.conda\envs\ras`; xác minh interpreter/pip trước dùng. Không tạo/cài venv.
- PostgreSQL/container/project/journal/credentials thử **cô lập**, runtime/recovery role thật. Cấp test admin URL và DB parts explicit **trước import fixture**, không fallback đọc `.env`.
- Không đọc/sửa `.env`, tác động stack/dữ liệu/journal/volume dev, đổi `pricing_verified` hoặc safety ready. Không dùng key/provider.
- Không query/migrate/restart dev để suy head. Không destructive reset; kiểm ID/labels/mount/consumer trước dọn đúng tài nguyên thử, giữ evidence ngoài repo và xóa credentials.
- Không giữ secrets trong fixture log/DB report/docs; dùng dữ liệu tổng hợp.

## 6. Docs, PR và tiêu chí hoàn thành

- Tạo handoff ngắn, inventory theo source/node và bảng caller thật: vị trí clock cũ/mới, predicate/TTL/current role, transaction/locks, response boundary, ca kiểm.
- Cập nhật docs index/decisions/testing/dev-setup/roadmap và C2d-1/2a/2b/3 khi hợp đồng bị ảnh hưởng; AGENTS chỉ dẫn tới handoff đúng. Giữ PR #41 đã merge `5100ff6`, source/dev revision tách biệt.
- Giữ report P2 và phép đo trước sửa như lịch sử; thêm trạng thái khắc phục theo code/JUnit mới, không biến FAIL cũ thành PASS hoặc ghi production/cutover đã an toàn.
- Kiểm liên kết Markdown, control characters và `git diff --check` cho file tracked/new. Không thêm lệnh chưa chạy vào AGENTS.
- Commit/push và mở PR **tiếng Việt**, mô tả phạm vi/bug/root cause/lock map/results/not-run/limits; không merge. Không đổi tác giả/committer hoặc thêm AI co-author/trailer; không thêm dòng Generated with Claude Code.
- Hoàn thành khi: ca P2 fail trước/pass sau; TTL clock cuối đúng cho idle/absolute; caller/ACL/lock/commit contracts giữ nguyên; suite affected và pending thật đạt; source/JUnit/inventory đối chiếu; docs nhất quán, diff sạch, tài nguyên thử đã dọn.

Báo cuối: base/head/branch/PR, file thay đổi, regression trước/sau + đối chứng, kết quả theo node/not-run, mọi failure của lượt chuẩn bị, docs đưa vào PR, cleanup và bàn giao. **Safety vẫn pending tới C4; writer/file/provider/C3/C4 còn riêng.**
