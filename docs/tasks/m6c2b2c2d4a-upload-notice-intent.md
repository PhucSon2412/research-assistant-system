# Prompt C2d-4a: notice và upload intent với caller hiện tại

**Trạng thái:** đã triển khai trên nhánh review, chưa merge; [bàn giao/kết quả theo source](../c2d4a-upload-notice-intent.md). Prompt bên dưới giữ hợp đồng giao việc. Main xác minh ngày 2026-10-06: `99db5285dfa59dd3d26e86345e849393ed88ce33`; head migration mã nguồn `0039`, không suy revision DB dev. Safety **pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-4a-sau-merge-pr-42-2026-10-06) · [Roadmap](../roadmap.md) · [Review PR #42](../reviews/pr42-final-read-clock.md) · [Caller inventory](../c2d1-caller-inventory.json)

Bạn là coding agent chính. Hoàn thành một PR **C2d-4a**, gồm code, kiểm thử, docs, commit/push và mở PR **tiếng Việt** để review; không merge. Phạm vi chốt là hai JSON route trước bước truyền file:

| Route | Đầu ra cần kiểm chứng |
| --- | --- |
| `GET /uploads/notice` | Caller hiện tại còn hợp lệ tại boundary trả notice; giữ response `{version, text}` |
| `POST /uploads/intents` | Xác nhận riêng cho một upload, actor/quyền/clock trước commit và proof mới trước trả token sau commit |

## 1. Base, tài liệu và môi trường

- Fetch lại, xác minh PR #42 MERGED và main chứa head `8f924c079b4e68a5d480c2c57d3b3da9708bd253`. Tạo nhánh `feat/m6c2b2c2d4a-upload-notice-intent` từ main đã xác minh. Nếu main tiến thêm, báo base/head và đối chiếu phạm vi trước sửa.
- Workspace có docs review/giao việc chưa commit, gồm `docs/reviews/pr42-final-read-clock.md` chưa tracked. Kiểm diff, giữ và đưa các cập nhật hợp lệ vào PR; không reset/clean hoặc checkout làm mất chúng. Nếu dùng worktree, chuyển đủ docs rồi đối chiếu.
- Đọc [AGENTS.md](../../AGENTS.md), [quyền và dữ liệu](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2d-1](../c2d1-final-caller-reads.md), [C2d-3](../c2d3-library-collection-json-writes.md), [sửa final clock](../c2d-final-read-clock.md), [testing](../testing.md) và caller inventory.
- Đối chiếu code thật: `api/app/api/routes/uploads.py`, `models/upload.py`, `api/deps.py`, `identity/final_caller.py`, `identity/predicates.py`, `api/resource_writes.py`, `auth_safety.py`; test upload/runtime và form upload/proxy hiện có. Nêu phạm vi, stale caller/raw commit và các mâu thuẫn docs/code trước sửa.
- Chỉ thử PostgreSQL/journal/project cô lập với runtime role thật, credentials thử riêng. Không đọc/sửa `.env`, query/nâng/restart stack dev, gọi OpenRouter hoặc đổi `pricing_verified`. Dùng Conda `C:\Users\ADMIN\.conda\envs\ras`, xác minh interpreter/pip; không tạo venv.
- Không mở safety pending để làm Compose healthy. Nền C1/Origin đã chốt; operator/delivery/C3/C4 và nơi lưu journal production vẫn riêng. Task không cần chốt lại các quyết định đó.

## 2. Hợp đồng JSON và xác nhận

- Notice vẫn cần xác thực, đọc được bởi User và Admin hợp lệ. Nội dung phải nói rõ text trích xuất gửi qua OpenRouter/provider để embedding, candidate chunk có thể gửi để rerank, câu hỏi và chunk được chọn có thể gửi khi RAG. Notice đơn thuần không tạo acknowledgement hoặc cho phép upload.
- Intent vẫn chỉ dành cho **User theo role hiện tại**; Admin bị `403 forbidden` theo hợp đồng API đang có. Không mở quyền upload cho Admin trong task này. Dependency principal là định danh đầu vào, không phải bằng chứng role/session còn hiệu lực tại commit/response.
- Giữ JSON riêng không có file, shape thành công `{intent_token, expires_at}` và validation filename/kind, byte size, SHA-256 khai báo, notice version/config. SHA-256 khai báo chưa chứng minh bytes thực; PUT kiểm file ở bước sau.
- `acknowledged` phải là JSON boolean `true`, cho đúng lần upload; thiếu/false/null/số/chuỗi hoặc fields ngoài schema bị từ chối. Notice cũ trả lỗi `notice_changed` như hợp đồng hiện hành. Một upload mới có acknowledgement và intent riêng, không dùng lại xác nhận từ lần trước.
- UI chỉ gửi file sau khi POST intent thành công. Không hứa server ngăn được mọi bytes từ một client bất tuân; cơ chế là UI tạo intent không kèm file trước, còn PUT phải kiểm intent trước lưu/tạo job. PR này giữ thứ tự UI đó, chưa chuyển boundary của PUT.
- CSRF gắn đúng session và Origin theo helper tin cậy hiện hành, gồm Origin thiếu/sai/lặp. Không tạo nhánh bỏ qua cho test hoặc giữ một kiểm Origin yếu hơn đường auth đã chốt.
- `ProcessingAcknowledgement` phải gắn đúng User/session/notice và mốc xác nhận. `UploadIntent` gắn cùng User/session/notice/acknowledgement, đúng metadata file đã xác nhận; token ngẫu nhiên chỉ lưu hash trong DB. Không log token/CSRF/cookie, filename hay payload upload trong audit; audit chỉ ID, action/reason code và notice version.
- Lifetime lấy config, mốc server mới cho acknowledgement/intent. Không lấy `now` từ đầu request cho toàn giao dịch, không gia hạn intent sau commit. Intent phải còn hạn tại kiểm cuối trước commit và trước trả token; chậm SQL/khóa không được biến thành lifetime mới.

## 3. Transaction và boundary

### GET notice

Đặt final read caller trên transaction thật sau các truy vấn cần thiết, dùng exact session/User, latest confirmed head/receipt, generation, current scalar profile/role và clock **sau predicate SQL cuối**. Caller mất hiệu lực trả `401 session_expired`, clear cookie, không trả notice. Gate thật đóng trả `503`; không touch activity, thêm row lock hoặc commit trong helper đọc. Giữ mọi boundary đã sửa ở PR #42.

### POST intent trước commit

1. Entry writer gate thật; khóa caller User `FOR NO KEY UPDATE` qua routine hiện hành trước dữ liệu upload. Kiểm actor/session/CSRF/current role từ dữ liệu hiện tại, không dựa identity map cũ.
2. Tạo acknowledgement, intent và business audit. Flush toàn bộ INSERT/FK/audit **trước journal exclusive**. Không lấy shared rồi upgrade; không lấy khóa User/resource mới hoặc phát sinh FK write sau journal.
3. Dùng writer final gate trực tiếp và kiểm lại actor/quyền, exact binding, notice, metadata, unused/TTL bằng dữ liệu DB, với clock mới sau SQL cuối. Giữ khóa tới commit. Có thể tái sử dụng helper C2d-3 nếu đúng hợp đồng; nếu cần boundary riêng cho secret/TTL, viết helper hẹp và giữ hành vi caller khác.
4. Guard từ chối trước commit phải rollback acknowledgement + intent + business audit. Activity của dependency auth là transaction khác, không dùng nó để kết luận mutation đã commit. Task không tạo auth journal event chỉ vì có upload acknowledgement/intent, không dùng `commit_auth` làm shortcut cho business writer.

### POST intent sau commit

- Chỉ sau **known commit** mới mở transaction/proof mới. Kiểm exact caller/current role/CSRF/gate, đọc lại intent + acknowledgement đúng binding và metadata, token hash khớp token vừa sinh, chưa dùng/chưa hết hạn. Sau mọi SQL cần cho response, kiểm clock mới cho cả caller và intent; không dùng ORM/proof trước commit.
- Đối chiếu thứ tự khóa của PUT hiện tại khi thiết kế recheck intent. Nếu cần khóa intent/acknowledgement cho response, lấy trước journal và không thêm khóa hàng sau journal. Không sửa PUT để che xung đột trong task này; ghi lock map và giới hạn boundary hiện hành.
- Mất quyền/hết hạn sau known commit: không trả token hoặc DTO thành công. Hàng có thể đã lưu và chờ hết hạn; không tuyên bố đã rollback mutation. Không tự xóa hàng để suy commit outcome.
- Exception từ commit, kể cả mô phỏng commit đã xảy ra nhưng mất phản hồi: trả lỗi có kiểm soát, **không token**, không tự retry/replay mutation hoặc cấp token mới. Phân biệt với DB/guard lỗi trước commit và lỗi recheck sau known commit; ghi mã lỗi, trạng thái DB có thể còn và UI message đúng thực tế. Không trả lỗi 500 lộ dữ liệu.
- Response đã qua boundary cuối có thể tiếp tục truyền sau khi nhả khóa; kiểm thử không đòi thu hồi bytes đã gửi. PR này không bổ sung journal cho intent hoặc cam kết bảo vệ vòng đời intent qua restore.

## 4. Ngoài phạm vi và bàn giao

- Không chuyển `PUT /uploads/original`, parser/storage/S3, job creation hoặc bất kỳ route original download nào trong PR này. Chúng tiếp tục theo hợp đồng đang có, còn task final caller riêng; không ghi là toàn upload đã an toàn nhờ C2d-4a.
- Không mở search/RAG/provider, đổi ACL, quota/model/notice về chính sách mới, thêm reindex, journal membership/xóa riêng, operator recovery hoặc cutover. Chỉ chỉnh web error mapping nếu mã lỗi mới cần hiển thị chính xác.
- Không dự kiến migration hoặc thay grants. Nếu chứng minh schema cần sửa cho đúng hai route, báo lý do và thêm revision sau head thực tế; không sửa revision đã merge, không cấp runtime thêm quyền để test qua.
- Bàn giao riêng: C2d-4b cho PUT file, kiểm sau parser/storage và trước save/job/commit, intent claim/lock/I/O và commit uncertain/orphan; C2d-4c cho private/common/review download, kiểm sau I/O/trước byte đầu, checksum/slot/disconnect. Đây là hướng chia tiếp, chưa phải prompt triển khai trọn các nhóm đó.

## 5. Kiểm thử bắt buộc

Tài khoản nghiệp vụ phải qua bootstrap/invitation/login/runtime với journal/head/receipt/predicate thật. Chỉ scoped cutover trong fixture nghiệp vụ theo C2d; giữ kiểm HTTP pending riêng **không patch**. Privileged fixture chỉ tạo dữ liệu/corruption cần thiết, không thay principal, latest-head predicate hay production grants.

| Nhóm | Ca và chứng cứ cần có |
| --- | --- |
| Contract | User/Admin đọc notice; User tạo PDF/Markdown intent `201`; Admin intent `403`, anonymous `401`. Validate xác nhận, notice cũ, filename/kind/size/hash theo config và hai upload có ack/token riêng |
| Binding/secret | Ack/intent đúng User/session/notice/metadata, DB chỉ hash; không override owner/session qua JSON. Lỗi/denial/uncertain không trả token; DB/log/audit không có token thô |
| Actor trước commit | Sau dependency, trong SQL/wait và trước final guard: revoke session, disable, đổi credential generation/head/receipt/quarantine/held, current role User→Admin, sai binding/CSRF. Rollback đủ business rows; giữ guard thật |
| Clock | Idle/absolute tại và quá hạn, sát trước hạn; clock tăng **trong predicate SQL cuối**. Intent expiry trong SQL trước commit và trong response proof; không gia hạn intent. Timestamp/lifetime lấy config |
| Sau commit | Known commit rồi auth/role/TTL thay đổi hoặc DB recheck lỗi: không secret response dù rows còn. Commit rollback/commit landed nhưng mất reply: lỗi có kiểm soát, không retry hoặc suy đã lưu/chưa lưu |
| Transaction/lock | Runtime role thật, flush FK trước exclusive journal; không shared upgrade/khóa hàng sau journal. Race bằng event/lock wait có kiểm soát, không chỉ sleep. Proof trước commit không dùng sau commit; pool/transaction được giải phóng |
| Pending và isolation | Cả hai route `503` khi safety pending thật; health/readiness giữ hợp đồng. A/B/Admin không có intent/token của người khác; không tạo item/version/file/job/chunk/index, không đọc/lưu storage hoặc gọi provider |
| Hồi quy | Final helper PR #42, C2d-3 writer/response, login/session/pending và migration/ORM liên quan. Giữ expectation upload/consent hiện hành; cập nhật fixture theo runtime thật, không nới assertion để che lỗi |

- Dùng fault injection cho flush/commit/postcommit và đối chứng bỏ một guard quan trọng ở từng lớp. Ghi rõ lỗi giả lập, không coi là crash/mất điện thật. Khôi phục source bytes/hash trước suite cuối, không chỉnh code trong lúc suite chạy.
- Chọn suite theo rủi ro hai route và helper thực sự đổi. Collect toàn inventory, selection/JUnit đủ node không thiếu/trùng, source SHA/dirty và hash trước/sau. Báo pass/fail/error/skip/not-run riêng; không cộng 124 reviewer hoặc 1094 agent của PR #42 vào kết quả mới. Nếu không chạy toàn inventory, ghi nhóm còn lại và lý do.
- Nếu đổi web, chạy test/typecheck/build liên quan và smoke form qua proxy với dữ liệu tổng hợp; kiểm checkbox ban đầu rỗng, không POST khi chưa xác nhận, lỗi intent không bắt đầu PUT. Nếu không đổi web/proxy, ghi not-run và lý do; không cần truyền file thật để chứng minh hai JSON route mới.
- Tài nguyên thử phải có label/port/volume riêng. Kiểm ID/mount/consumer trước cleanup; giữ stack/container có sẵn và dev nguyên trạng. Test không đọc key/provider từ `.env`.

## 6. Docs và tiêu chí hoàn thành PR

- Thêm handoff và inventory theo source thực tế; cập nhật caller inventory, testing, decisions, roadmap, dev-setup và các mục lục. Trong docs phân biệt **đã merge**, **đang review** và nhóm **chưa chuyển**; giữ lịch sử review/failure, không sửa số đo cũ thành kết quả mới.
- Nêu lựa chọn helper/lock map, binding/clock trước và sau commit, mã lỗi và ranh giới khi mutation đã lưu nhưng không trả token. Thông điệp UI không hứa “chưa có gì được lưu” ở nhánh commit uncertain/postcheck.
- Kiểm ORM/schema nếu đổi model/schema hoặc helper DB liên quan; kiểm Markdown links và `git diff --check`. Không ghi lệnh build/test chưa chạy thành lệnh đã kiểm.
- Hoàn thành khi hai route dùng caller/gate thật và các ca contract, runtime, auth/clock, pre/postcommit, uncertain, pending cùng hồi quy chọn đạt; không secret/provider/storage leak. PUT/download, writer/provider khác và C3/C4 vẫn có inventory rõ, **safety pending tới C4**.
- Commit không thêm AI vào author/committer hoặc trailer. Push nhánh, mở PR tiếng Việt với base/commit, file, kết quả theo source, các lượt lỗi/đối chứng, not-run và giới hạn; **không merge**. Báo lại link PR và bàn giao C2d-4b. Không xin lại quyền commit/push/mở PR đã được giao trong task này.
