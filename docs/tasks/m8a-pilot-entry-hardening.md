# M8a — bản triển khai nhóm thử: HTTPS, delivery, rate limit và CI

Bạn là coding agent tiếp theo. Triển khai **một PR hoàn chỉnh** để có một bản cài cho nhóm thử kín với đường vào HTTPS, cookie Secure, invite/reset qua kênh dùng được và chống lạm dụng auth cơ bản. Đây là phần đầu **gói E** trong [kế hoạch MVP](../mvp-delivery-plan.md), thực hiện trong lúc chủ dự án duyệt phiếu M7b. Không đợi phiếu để coding, không tự điền hoặc chấm thay người đọc.

## 1. Base và phạm vi ủy quyền

- PR #65 đã merge: đã fetch/xác minh `origin/main = 3b59a1db4e784df0891a844ba968ba85e2797746`, head review `8799216` là ancestor và tree khớp. Source migration **0052**, không suy revision DB dev hoặc cộng pass mới tại merge.
- Fetch lại khi bắt đầu, xác minh base mới nhất và tạo `feat/m8a-pilot-entry-hardening`. Giữ tất cả docs reviewer/orchestrator modified/untracked; đưa vào PR, không reset/discard. Các mốc OPEN/P2 cũ trong docs là lịch sử.
- Đọc AGENTS, kế hoạch MVP, hợp đồng C4, C2 auth/invitation/reset/delivery và các route/proxy liên quan. Dùng source/runtime hiện hành, không quay về fixture owner/generic receipt hoặc đặt lại safety pending để tránh tích hợp.
- Các lựa chọn dưới là **orchestrator chọn theo ủy quyền hoàn thành MVP**, không ghi như chủ dự án đã trực tiếp chọn nhà cung cấp/host hoặc xác minh account thật. Coding triển khai ngay, không dừng ở proposal hoặc xin xác nhận lại những lựa chọn này.
- Chưa có domain, host hoặc SMTP account production được giao. Dùng cấu hình mẫu và tài nguyên tổng hợp cô lập, chuẩn bị artifact/runbook cụ thể; không tự mua dịch vụ, thay DNS, gửi mail cho người khác hoặc deploy/cutover hệ thống thật.

## 2. Cấu hình triển khai riêng và HTTPS

Tạo bộ Compose/deploy riêng cho nhóm thử; **không đổi file Compose dev thành production bằng biến môi trường**. Reuse image, role provisioning, bootstrap, readiness và recovery của C4. Ưu tiên reverse proxy **Caddy** làm điểm vào duy nhất; không thêm Kubernetes/IAM/SSO hay hạ tầng nhiều tầng.

- `RAS_ENVIRONMENT=production`, cookie Secure/HttpOnly/SameSite và tên `__Host-*` theo hợp đồng hiện hành. Public base URL và allowed Origin phải là HTTPS, tường minh; không suy từ Host/X-Forwarded-Host chưa tin cậy. Token link vẫn trong fragment, không query string.
- Chỉ edge mở cổng ra ngoài. API, web nội bộ, PostgreSQL, Redis và MinIO không có cổng public; không public console/outbox/docs quản trị nhạy cảm. Tách runtime, migrator, recovery/operator và secret theo hợp đồng hiện tại, không nhúng credential owner vào runtime.
- Chọn và ghi rõ chuỗi proxy tin cậy edge → web → API. Xóa/ghi đè header IP/forwarded do client gửi tại ranh giới tin cậy; không lấy tùy ý phần tử X-Forwarded-For hoặc tin mọi proxy. Kiểm cả đường qua Next và kết nối trực tiếp ngoài topology dự kiến.
- Journal path phải được người vận hành cấp tường minh, không âm thầm dùng volume dev. Trong smoke có thể dùng thư mục thử riêng; phải ghi rõ cùng máy **không** chứng minh độc lập miền sự cố. Thiếu/hỏng journal, hold hoặc integrity mismatch vẫn đóng gate thật như C4.
- Test HTTPS local bằng CA thử riêng được trình duyệt tin cậy trong context thử; không coi `ignore HTTPS errors`/tắt xác minh cert là bằng chứng TLS đạt. Domain thật/ACME và nơi lưu journal độc lập chỉ là bước cấu hình/cutover sau này, chưa được báo đã hoàn tất.
- Runbook có new install và upgrade có dữ liệu: build đúng image → migrate/provision/recovery theo C4 → kiểm hậu điều kiện → mở traffic. Không `register-existing --confirm-db-is-current` tự động để vượt lỗi.

## 3. Delivery thật qua SMTP/TLS

MVP chọn **adapter SMTP qua TLS** (SMTPS hoặc STARTTLS bắt buộc, xác minh certificate); dùng interface `DeliveryAdapter`/`OutgoingMessage` sẵn có. Không bắt buộc nhà cung cấp email trả phí hoặc tài khoản bên ngoài để hoàn thành coding. SMTP giả/capture chỉ đặt trong project thử, không là dev-outbox trá hình trong bản production.

- Cấu hình endpoint/sender/TLS/timeout/secret tường minh. Production từ chối cấu hình plaintext hoặc bỏ xác minh TLS; không tự fallback. Secret lấy từ cơ chế file/secret hoặc config đã được bảo vệ, không đưa vào argv, Git, log, exception hoặc artifact CI.
- Reuse luồng tạo token, final caller/binding/clock, journal và compensation hiện hành. Không giữ DB connection/transaction/row/journal lock qua network delivery. Không gửi trước known commit hoặc sau proof đã mất hiệu lực; giữ expiry và generation/fence của token.
- Timeout/SMTP reject/TLS lỗi phải có kết quả được kiểm. Server nhận mail không đồng nghĩa người nhận đã nhận; thông báo UI không hứa giao thành công. Không tự retry mutation/delivery không xác định hoặc tạo lại token rồi gửi trùng. Giữ semantics compensation invitation và generic reset hiện hành; chỉ thay hợp đồng khi có lý do và regression rõ.
- Reset request cho email có/không có account giữ cùng status/body không tiết lộ tồn tại account hay link. Không tạo route Admin đọc raw reset token trong production. Không ghi email/token/password/link vào audit/log mặc định; audit dùng ID/code như trước.
- Test transport production với SMTP server cô lập, gồm TLS/certificate, chậm/timeout, reject và connection drop; kiểm hộp thư capture chỉ ở test. Không gửi tới mailbox thật trong task. Đưa SMTP settings/version mới vào docs/config mẫu, không đọc `.env` đang có.

## 4. Rate limit auth với Redis dùng chung

Thêm giới hạn cấu hình được cho login, reset request và các đường consume secret công khai (invite accept, reset confirm). Tận dụng Redis sẵn có, không thêm bảng auth hoặc lock account vĩnh viễn chỉ vì request sai.

- Có bucket theo nguồn và theo định danh phù hợp; key account dùng giá trị chuẩn hóa đã băm/HMAC, không ghi email/raw token/cookie/IP thô vào log hay response. Tránh để mọi user cùng NAT bị chặn bởi một quota quá nhỏ; chọn default ban đầu và ghi là giá trị vận hành khởi đầu, chưa benchmark/SLO.
- Phép admit/TTL phải nguyên tử, chia sẻ giữa process API. Test song song/hai process và hết cửa sổ; không chỉ dùng dict trong một process, không khóa DB khi chờ Redis.
- Request bị giới hạn trả **429** với mã JSON ổn định và `Retry-After`; UI hiển thị được và không tự submit lại. Key tồn tại/không tồn tại có cùng policy/response; giới hạn reset không được lộ account. Không dùng sleep dài giữ worker để mô phỏng giới hạn.
- Khi Redis limiter lỗi, các thao tác auth bị bảo vệ trả lỗi 503 có kiểm soát; không lặng lẽ bỏ limiter và không khóa/xóa account. Health vẫn sống, recovery/hold gate giữ ưu tiên và hợp đồng của nó.
- IP do client giả header không được đổi bucket để vượt giới hạn; reset thành công không được xóa tùy ý bộ đếm brute force. Không nới Origin/CSRF/session/ACL hoặc outcome uncertain để tích hợp limiter.
- Chưa thêm CAPTCHA, global WAF, hệ thống chống abuse nâng cao hoặc rate limit mọi route/provider trong PR này.

## 5. CI tối thiểu, chạy lại được

Repo hiện chưa có workflow CI. Thêm GitHub Actions cho PR/push main, dùng dependency/lockfile hiện có và cấu hình test tường minh; không đọc `.env`, dùng account thật hoặc cần secret provider để xanh.

- Jobs cho API theo selection có lý do (config, auth/provenance/session, invitation/reset/delivery, limiter, migration/ORM, C4/deploy contract và hồi quy liên quan), web tests/typecheck/build, kiểm config triển khai. Ghi rõ những gì CI chưa chạy; không gọi selection là full suite.
- PostgreSQL/pgvector, Redis, MinIO/journal nếu selection cần phải cô lập và runtime thật; không thay bất biến bằng mock owner/bypass safety. SMTP capture/fake provider chỉ là test transport, không là mode production.
- Chặn outbound tới provider/arXiv và không chạy inference trong CI. Artifact chỉ gồm bằng chứng đã quét/redact; không upload token/link/secret/paper riêng hoặc full environment.
- Trên failure, workflow thu kết quả rồi kết thúc bằng exit thật; runner không chờ file vô hạn khi process đã chết. Không dùng `continue-on-error`/rerun pass để xóa failure lịch sử.
- Không cần tạo workspace Postman mới hoặc publish API ra dịch vụ ngoài chỉ để có CI. Reuse hợp đồng OpenAPI của FastAPI và runner/test hiện hành; nếu dùng collection thì giữ local trong Git, không credential thật và không workspace push.

## 6. Kiểm chứng và tiêu chí hoàn thành

Chạy selection theo rủi ro thay đổi, ghi source hash/JUnit/collection/exit cùng not-run chính xác. Không sửa code trong suite. Nếu sửa shared auth/gate rộng hơn dự kiến, mở hồi quy tương ứng; không mặc định rerun hơn 5.000 test hoặc năm restore drill khi không có thay đổi journal/restore/schema. Migration mới chỉ khi thực sự cần, luôn sau head thực tế, không sửa revision đã merge.

Phải có smoke trên **image/deploy config thật**, project/port/volume/journal/credential riêng, provider key trống và pricing false:

1. Cài mới/bootstrap theo C4, production readiness đạt bằng hậu điều kiện thật; session login qua HTTPS có cookie đúng, HTTP/Origin sai không mở được thao tác ghi.
2. Admin invite → SMTP capture → User accept → login; reset request → capture → confirm → credential cũ không dùng được, session/token đã kết thúc vẫn bị chặn. Hai User và Admin giữ ma trận private ACL qua proxy.
3. Quá quota → 429/Retry-After; hết cửa sổ hoạt động lại; header IP giả không vượt được; Redis/SMTP lỗi có kết quả được kiểm, không có token hoặc chi tiết account lọt vào response/log.
4. Production không có dev-outbox, không có port DB/API/Redis/MinIO public, thiếu/hỏng journal hoặc hold chặn traffic theo gate hiện hành. Đường lỗi vẫn trả connection/slot/tài nguyên như các hợp đồng đã merge.
5. Chrome thật qua HTTPS: invite/reset/login và ít nhất upload → library/collection → keyword search hoặc trạng thái extract phù hợp. Hybrid/RAG với pricing false phải hiển thị đúng thông báo; không gọi 503 đó là chất lượng RAG đã đạt.

Có ít nhất các countercheck nhắm guard TLS/cookie/Origin, trusted IP, limiter nguyên tử hoặc failure policy, no delivery trước known commit; tái hiện lỗi thực nếu có. Không nhân số mutant để chạy theo quota. Giữ failure/control/rerun riêng, source khôi phục trước suite cuối. Build/config check đơn lẻ không thay smoke browser.

Chụp inventory Docker trước/sau, chỉ dọn tài nguyên thuộc project thử sau kiểm label/path. Không query/migrate/restart dev `ras`, không đụng Dify hoặc tài nguyên có sẵn. Không đọc key/`.env`, gọi provider/arXiv thật, gửi mail thật hoặc đổi `pricing_verified`.

## 7. Bàn giao một PR

Commit/push và mở **một PR tiếng Việt**, không merge, không AI co-author/trailer/promo, giữ author/committer. Giữ các docs review/task có sẵn trong PR. Handoff `docs/m8a-pilot-entry-hardening.md` và inventory đi kèm phải có:

- Cách chạy bằng config/secret mẫu, topology/ports/roles và SMTP channel; commands đã thực chạy, kết quả image/Chrome/TLS/rate-limit/CI-local, failure/not-run và cleanup.
- Phân biệt CI workflow đã viết/kiểm local với workflow thực chạy trên GitHub; không tự nhận remote xanh khi chưa có run.
- Phân biệt SMTP test/TLS local với delivery và certificate domain thật; chưa cutover và chưa nghiệm thu production.
- Cập nhật kế hoạch/mục lục/config docs và API/UI contract bị đổi; không nhân bản lịch sử vào mọi file.

**Sau M8a:** M8b đóng vận hành journal độc lập, backup có lịch/retention/mã hóa phù hợp, alert/recovery và nghiệm thu triển khai. Không tự xây các phần đó trong PR này; topology M8a phải hỗ trợ chứ không khẳng định đã đáp ứng. Domain/host, operator thật và nơi lưu journal độc lập phải rõ trước cutover thật.

M7b đã merge phần tooling nhưng **0 verdict thật**, vẫn `awaiting_human_review`; người đọc điền phiếu riêng. D còn cache-write live với ngân sách/ủy quyền mới, policy/account/routing/retention và paper thật có quyền. Trần M7a cũ đã hết; không tự cấp lại. M8a không cho phép bật pricing hay tuyên bố dự án đã nghiệm thu toàn bộ.

**Inventory khi giao:** chỉ đọc source/docs/Git và fetch xác minh merge/tree PR #65; chưa chạy app/test/Docker/SMTP/CI/provider hoặc truy vấn DB. Đây là task **mới giao/chưa triển khai/kiểm**, không phải báo cáo M8a đã đạt.
