# Prompt coding C2d-1: kiểm lại người gọi ở các route đọc ngắn

[Mục lục](../README.md) · [Quyết định chia C2d](../decisions.md#chia-c2d-sau-merge-pr-37-2026-10-05) · [Thiết kế auth/T14](../auth-restore-safety.md#ma-trận-kiểm-thử) · [Bàn giao C2c-2](../c2c2-invitation-runtime.md)

## 1. Nhiệm vụ và base

Bạn là coding agent chính. Triển khai **C2d-1**: helper kiểm lại người gọi trên transaction hiện tại và áp dụng cho ba route đọc dưới đây. Làm code, kiểm thử, cập nhật docs, commit/push và mở PR tiếng Việt để review; **không merge**. Không dừng ở kế hoạch khi đã đủ điều kiện thực hiện.

PR #37 đã merge: `origin/main=ca4a623ea9cd4d2f8454659a1d66c48dbf8093d0`, chứa `9f26535` và head migration mã nguồn `0039`. Đây là chứng cứ khảo sát ngày 2026-10-05; fetch lại, kiểm ancestor và head thực tế trước khi tạo nhánh `feat/m6c2b2c2d1-final-caller-reads`. Không suy revision DB dev từ source. Không sửa migration đã merge; task này ưu tiên dùng schema/routine hiện có. Nếu cần đổi schema, giải thích khoảng trống và thêm revision mới sau head thực tế.

Giữ các docs review/handoff chưa commit của orchestrator, đưa vào PR sau khi kiểm nội dung; không reset hoặc bỏ chúng. Lượt orchestrator này chỉ viết tài liệu, chưa triển khai helper hoặc chạy test C2d-1.

## 2. Tài liệu và code phải đọc

- `AGENTS.md`, [docs README](../README.md), [quyết định nền C1](../decisions.md#quyết-định-nền-c1-theo-ủy-quyền-2026-10-03), [Origin/cách chia C2](../decisions.md#chia-c2-và-chốt-origin-theo-ủy-quyền-2026-10-03).
- [C1](../c1-auth-safety-foundation.md), [C2a helper/lock order](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2c-1](../c2c1-bootstrap-receipts.md), [C2c-2](../c2c2-invitation-runtime.md), [review PR #37](../reviews/pr37-c2c2-invitation.md).
- `api/app/api/deps.py`, `api/app/auth_safety.py`, `api/app/identity/{predicates,sessions,session_commit}.py`, `api/app/api/routes/{auth,admin}.py`, `api/app/delivery.py`, User/session models và routine liên quan.
- Fixture scoped/runtime trong `api/tests/conftest.py`, test C2a/C2b/C2c-2, `test_invitations.py` và [hướng dẫn thử cô lập](../dev-setup.md#c2c-2-invitation-cô-lập).

Nguồn yêu cầu là brief, các quyết định đã chốt và phạm vi này. C1/Origin/runtime boundary đã chốt theo ủy quyền; không xin lại. Operator, xác minh legacy/initial status và delivery C3; cutover/storage C4 vẫn riêng.

## 3. Phạm vi triển khai

| Thành phần | Khoảng trống đã thấy tại base | Đầu ra cần có |
| --- | --- | --- |
| Helper final caller dùng chung | Dependency kiểm session rồi rollback để nhả journal lock; principal/context còn là snapshot đầu request | Kiểm session–owner, latest head/receipt, thời hạn, integrity/cutover và role hiện tại khi route chuẩn bị trả dữ liệu |
| `GET /auth/session` | Trả trực tiếp profile/expiry trong `SessionContext`, không có kiểm cuối ở route | Profile và thời hạn lấy từ dữ liệu hiện hành, caller còn hợp lệ; giữ shape API |
| `GET /admin/users` | `require_admin` ở đầu, sau query không kiểm caller/role cuối | Chỉ Admin còn hợp lệ nhận danh sách; không dùng role cũ để cấp quyền |
| `GET /admin/dev-outbox` | Audit commit nhả khóa; đọc entries bằng `now` đầu request, không kiểm lại caller sau commit | Kiểm Admin trước đọc link, commit audit có xử lý lỗi, kiểm lại sau commit trước response; TTL dùng clock mới |
| Inventory C2d | Caller còn trải ở nhiều module và tài liệu C2a phản ánh source lịch sử | Bảng caller hiện hành, điểm kiểm/commit/I/O/lock order, phần đã xong và PR tiếp cần làm |

Không mở rộng task này sang toàn bộ resource routes, storage/stream, review/promotion/publication, search/RAG/provider hoặc worker. Không đổi ranking, payload provider, ACL kho chung/riêng, budget, auth writer đã chuyển hoặc quyền operator. `/auth/sessions` và `/csrf` đã có kiểm cuối: giữ các sửa P2, chỉ tái dùng helper nếu thật sự cần và có hồi quy.

## 4. Hợp đồng helper và transaction

1. Nhận danh tính đã được dependency xác thực: **đúng session ID và User ID của caller**. Không tin role/email cũ trong Principal/SessionContext, không nhận User ID từ body để thay caller. Helper không thay entry session/CSRF/Origin dependency hoặc SQL resource ACL.
2. Dùng `auth_safety.require_final` trên **cùng transaction/connection** với route, sau các khóa tài nguyên nếu có. Giữ shared journal lock tới khi caller commit/rollback. Không kiểm integrity bằng connection thứ hai, không cache proof qua transaction/request.
3. Sau khi chờ journal lock, lấy **clock mới**, kiểm `session_live(..., user_id=caller_user_id)` với SQL predicate latest confirmed credential/status head + receipt và session generation. Không tự mở quarantine/held/legacy, không chỉ kiểm `User.status` hoặc `revoked_at`. Đây không phải predicate dành cho quarantine reset.
4. Tải lại profile/role/session fields bằng dữ liệu DB hiện hành, tránh ORM identity map cũ. Nếu đọc thêm dữ liệu/chờ công việc có thể kéo dài, lấy clock mới ở điểm kiểm cuối ngay trước quyết định trả response; không dùng `get_now` cố định. Kiểm cuối không touch session/đẩy idle expiry hoặc issue session mới.
5. Session hết hạn/mất hiệu lực hoặc sai binding → `401 session_expired` và clear cookie theo convention hiện có. Session hợp lệ nhưng current role không là Admin → `403 forbidden`; gate/integrity/pending lỗi → `503 service_recovering`. Không đổi quy tắc resource trái quyền trả `404` ở các PR sau.
6. Helper không commit/rollback transaction của route; trả kết quả có trường allowlist tối thiểu, không trả verifier/PHC, token, CSRF digest hoặc User ORM để serializer tự chọn. Không lấy khóa hàng User/session sau item/journal; không dùng `get_auth_write_context` như final guard của resource route.
7. Proof trước commit không có hiệu lực cho transaction sau commit. Route có commit phải kiểm lại trước phản hồi, bằng dữ liệu và clock mới. Lỗi hoặc outcome commit chưa rõ không được chuyển thành response thành công.
8. Không giữ khóa journal/tài nguyên khi chờ provider, storage hoặc delivery adapter. Snapshot DevOutbox trong bộ nhớ phải ngắn và có giới hạn hiện hành; không gửi message/khởi động adapter từ helper.

Helper đọc này không phải giải pháp drop-in cho writer cần exclusive journal lock hoặc append batch: không giữ shared lock rồi nâng khóa hoặc lấy thêm User. Inventory phải ghi riêng chỗ cần final actor dưới exclusive lock của transaction ghi. Các writer đã chuyển vẫn giữ User UUID → token/session/invitation → journal như hợp đồng hiện hành.

## 5. Chi tiết ba route

- **Session:** giữ `user` và `session` theo shape hiện có; profile/role và idle/absolute expiry phản ánh DB hiện hành. Thay đổi sau dependency không được che bởi context cũ. Giữ quy tắc idle/absolute phía server và không gia hạn ở final check.
- **Admin users:** dựng DTO allowlist, kiểm caller sau query và trước response. Admin có thể cần nhìn account legacy/held để quản trị; không lọc các target User bằng `account_live` để làm danh sách biến mất. Proof là của Admin đang đọc, không phải của từng account trong danh sách.
- **Dev outbox:** vẫn chỉ `development` + adapter `DevOutbox`, mode khác giữ `404`. Không đọc entries khi caller đã mất quyền trước điểm kiểm. Audit chỉ ID/mã, không recipient/link/token; commit audit lỗi thì không trả messages. Sau audit commit phải có final check mới trước đọc/response; nếu có bước đọc trước commit, cũng kiểm trước đọc và kiểm lại sau commit. Expired entries dùng clock mới. Không hứa response bị từ chối đồng nghĩa audit chưa commit hoặc message chưa từng được gửi bởi task khác.

Ranh giới thực thi: thay đổi auth/gate thắng trước điểm kiểm cuối phải chặn dữ liệu. Final journal lock bảo vệ các thay đổi đi đúng journal protocol tới khi transaction kết thúc. Response đã qua kiểm cuối rồi nhả khóa có thể tiếp tục được gửi; bytes đã gửi không thu hồi được. Không hứa hủy hồi tố response hoặc xử lý sửa SQL ngoài trust boundary. Ghi và kiểm giới hạn này, không biến nó thành miễn kiểm cuối.

## 6. Kiểm thử bắt buộc

Chỉ PostgreSQL/runtime/recovery LOGIN và journal tổng hợp cô lập. Cấp `RAS_TEST_DATABASE_ADMIN_URL`/`RAS_DB__*` thử riêng **trước import fixture**; không fallback `.env`. Dùng fixture C2 đã có: chỉ cutover được scoped trong test HTTP nghiệp vụ; không mock integrity/receipt/head/session predicate/fresh clock để làm test pass. Có nhóm HTTP pending riêng qua gate thật.

| Nhóm | Ca và kỳ vọng |
| --- | --- |
| Luồng hợp lệ | Admin/User có receipt thật dùng được session endpoint; Admin đọc users/outbox; User bị 403, không outbox entries; API shape và các expectation nghiệp vụ giữ nguyên |
| Sau dependency | Revoke session, disable, generation cũ, credential quarantine, active held, thiếu/sai latest receipt/head, session khác owner trước final check → không payload thành công |
| Current state | Context role/profile cũ không cấp Admin; session response lấy profile/expiry hiện hành; role thay đổi setup bằng fixture đặc quyền, không cấp raw write cho runtime |
| Thời hạn | Idle thiếu 1 giây/đúng 30 phút; absolute thiếu 1 giây/đúng 8 giờ; clock tiến sau query và khi chờ final lock. Dependency có touch: đo biên từ `last_activity_at` thực, chứng minh helper không touch thêm |
| Recovery/gate | Hold, mirror unconfirmed hoặc journal ahead sau middleware/dependency nhưng trước final → 503; shared lock dùng cùng transaction, không proof từ connection khác |
| Outbox commit | Actor/gate thay đổi sau audit commit → không messages; lỗi commit/commit reply chưa rõ → controlled error, không success payload; entries TTL dùng clock mới, audit/log/journal không chứa link/token |
| Race/khóa | Barrier tất định: thay đổi auth thắng trước final bị chặn; final shared lock thắng thì writer journal chờ, sau nhả vẫn hoàn tất. Helper dùng được sau khóa tài nguyên thử, không lấy thêm User/session row lock hoặc để transaction treo |
| Pending thật | Không patch gate: route bảo vệ 503, `/healthz` sống, `/readyz` 503; không mở ready C4 |
| Hồi quy | C2a `/csrf` và `/auth/sessions` P2, login/session; C2b status; C2c-2 invitation/outbox; ORM/schema nếu có thay đổi model/routine |

Kiểm binding/receipt bằng runtime thật; tạo account hợp lệ theo fixture C2c-1/C2c-2 đã có. Không mock Principal thành Admin rồi coi là end-to-end auth. DTO của users không chứa PHC/session digest; link DevOutbox là dữ liệu **được phép** trong response của Admin dev hợp lệ, không được log hoặc trả cho caller bị chặn.

Thực hiện đối chứng phù hợp: bỏ final caller binding, fresh clock, current Admin role hoặc kiểm sau audit commit phải làm test liên quan fail; khôi phục source trước lượt suite cuối. Không sửa code/migration/mutant khi suite đang chạy. Dùng barrier/event và timeout hữu hạn, tránh chỉ sleep để đoán race.

Chọn suite theo rủi ro và báo inventory: source SHA, collect node IDs, nhóm pass/fail/error/skip/not-run, JUnit không thiếu/trùng và các lỗi lượt chuẩn bị. Không cộng các lượt chạy chồng nhau thành full suite. Không đổi kỳ vọng nghiệp vụ thành 503 hàng loạt để bỏ phần chưa chuyển; core guard của ba route phải kiểm ngay ở C2d-1, full cutover C4 còn riêng. Nếu web không đổi, ghi web not-run; nếu phải đổi contract, chạy web tests/typecheck/build tương ứng.

## 7. Inventory và bàn giao các phần C2d còn lại

Tạo `docs/c2d1-final-caller-reads.md` và inventory kiểm thử theo convention hiện có. Bảng caller phải ghi route/function, identity source, resource SQL ACL, thời điểm I/O/provider/commit/response, thứ tự khóa và final guard hiện có/cần thêm; dẫn code cụ thể. Đối chiếu **toàn source thực tế**, không sao chép bảng C2a cũ như trạng thái hiện hành.

Tối thiểu rà soát các nhóm sau, chỉ ghi kế hoạch ở task này:

| Nhóm sau | Điểm cần kiểm và giới hạn |
| --- | --- |
| JSON resource | Library metadata/chunk/coverage/references/candidates/patch, collections CRUD/membership, discovery topics/papers/sources/citing-items; final caller + resource ACL, riêng read/commit |
| File/upload | Intent, PUT/upload original, private/common download, review original; parser/storage ngoài khóa, kiểm trước lưu/job/byte đầu; giữ checksum/slot/disconnect và đúng version/file |
| Resource writes/journal | Review submit/withdraw/reject/approve, promotion, publication/revoke, Admin common index; actor role/owner cuối trước commit, journal batch/lock order, copy/cleanup khi actor mất quyền |
| Search/RAG | Scope/hydrate/viewer, query embedding/rerank/generation và final response; caller sau reservation commit và trước mỗi dispatch, ACL trước top-k; giữ uncertain accounting và rights/epoch gates |
| Jobs | Recovery gate + durable job/item rights/cancel; không buộc job hợp lệ vào session trình duyệt, không cấp Principal/session giả cho worker |
| Tổng hồi quy | Port fixture runtime và giữ expectation các contract đã merge, xác nhận caller inventory đầy đủ; không dùng kết quả lịch sử làm chứng cứ source mới |

Đề xuất PR tiếp đủ nhỏ từ inventory này; C2d-1 hoàn tất không có nghĩa toàn T14/C2 hoàn tất. C3 operator/delivery/status recovery và C4 cutover/integration vẫn riêng.

## 8. Môi trường, docs và hoàn thành

- Nếu chạy/cài Python, kiểm interpreter/pip rồi dùng `C:\Users\ADMIN\.conda\envs\ras\python.exe`; không tạo venv. Project/container/port thử riêng, xác minh ownership trước dọn, không đụng dev volume hoặc journal thật.
- Không đọc/sửa `.env`, migrate/restart stack dev, gọi OpenRouter, đổi model/pricing flag hoặc mở safety ready. Unhealthy/readiness 503 trong C1–C3 là trạng thái pending đã thiết kế, không là lý do thêm bypass.
- Cập nhật decisions, handoff, testing/inventory, docs README, roadmap và trạng thái mở đầu nếu cần; chỉ thêm lệnh **đã chạy** vào dev-setup/AGENTS. Phân biệt yêu cầu đã chốt, lựa chọn triển khai và đề xuất chưa đo; báo mâu thuẫn docs/code cùng bằng chứng.
- Kiểm link nội bộ, control characters và `git diff --check`. Commit/push, mở PR tiếng Việt cho chủ dự án review, không thêm AI tác giả/trailer hoặc dòng attribution tự động; không merge.
- Báo base/head/branch/commit, migration nếu có, file thay đổi, API/web/mutant/pending results thực chạy, not-run, mọi lỗi lượt trước và tài nguyên thử đã dọn; nêu giới hạn response đã qua final boundary.

**Tiêu chí hoàn thành:** helper có binding/receipt/gate/clock đúng transaction; ba route kiểm caller ở đúng điểm và giữ contract; outbox không lộ khi auth/gate đổi sau commit; test runtime/race/pending và đối chứng đạt; inventory C2d đủ nhóm, docs nhất quán. Safety vẫn pending tới C4, chưa tuyên bố auth toàn hệ thống hoặc restore production an toàn.
