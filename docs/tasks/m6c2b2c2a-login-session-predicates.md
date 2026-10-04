# Prompt C2a: login, session và predicate auth dùng chung

[Mục lục](../README.md) · [Quyết định C2](../decisions.md#chia-c2-và-chốt-origin-theo-ủy-quyền-2026-10-03) · [Bàn giao C1](../c1-auth-safety-foundation.md#bàn-giao-c2) · [Thiết kế](../auth-restore-safety.md#chia-pr-coding)

## Giao việc và phạm vi

Bạn là coding agent chính. Bắt đầu **coding C2a**, hoàn tất triển khai, kiểm chứng, commit/push và mở PR tiếng Việt để review; chưa merge. Đây là PR đầu của C2, không phải toàn bộ C2. Chủ dự án đã ủy quyền orchestrator chọn phương án hợp lý, thuận tiện; quyết định Origin và cách chia PR dưới đây do orchestrator chốt trong phạm vi đó, không phải lựa chọn trực tiếp của chủ dự án. Không dừng ở một báo cáo khảo sát hoặc xin lại nền đã chốt.

PR #33 đã merge tại `c98bd33`, chứa code cuối `c647d6e`; head source hiện quan sát là `0034`. C1 có receipt/projection/replay hai pha, runtime/recovery routines, session generation và reset-token status fence; safety vẫn **pending tới C4**. Source head không chứng minh revision DB dev.

Thứ tự C2: **C2a login/session/predicate → C2b reset và disable/enable → C2c bootstrap/invitation → C2d tích hợp final checks và kiểm đủ writer/caller**. C3 mới triển khai recovery User/Admin, approval/operator và delivery; C4 mới mở cutover sau diễn tập. Phạm vi kế tiếp có thể tinh chỉnh theo bằng chứng, nhưng không gom mọi flow vào PR C2a hoặc gọi auth đã hoàn chỉnh.

Đọc `AGENTS.md`, docs liên quan và code trước khi sửa. Fetch origin, xác minh main chứa PR #33 và giữ các cập nhật docs bàn giao đang chưa commit. Tạo `feat/m6c2b2c2a-login-session` từ main mới nhất; đưa các docs bàn giao vào PR. Báo base SHA, head migration thực tế, phạm vi và mâu thuẫn code/docs. Giữ riêng thay đổi ngoài task, không reset hoặc ghi đè chúng.

## Quyết định đã chốt: Origin/client compatibility

- MVP tiếp tục browser qua proxy Next.js cùng origin, cookie session và CSRF hiện có; chưa thêm API key/bearer auth hay ngoại lệ dành cho client không phải browser.
- Mọi HTTP request ghi (`POST/PUT/PATCH/DELETE`, gồm auth anonymous) cần **Origin tường minh thuộc `RAS_HTTP__ALLOWED_ORIGINS`**, ngoài các kiểm CSRF hiện có. Thiếu, `null`, sai hoặc origin có scheme/host/port khác đều bị từ chối; không fallback từ Host/Referer/Forwarded hoặc môi trường development.
- Chỉ chấp nhận cấu hình origin HTTP(S) có scheme/host/port hợp lệ, không credentials/path/query/fragment/wildcard. Không tự thêm origin khi allowlist rỗng. Xác định normalization nhất quán trong config và test, không dùng so khớp hậu tố host.
- Proxy chuyển tiếp Origin thật; không tự tạo header tin cậy cho request thiếu/sai Origin. Client HTTP/script thử phải gửi Origin tường minh cùng cookie và CSRF đúng. Header Origin không thay xác thực hoặc ACL, cũng không chứng minh người gọi là browser.
- `GET/HEAD/OPTIONS` giữ hợp đồng hiện hành; không biến OPTIONS thành đường mutation. CLI bootstrap/recovery không phải HTTP, dùng hợp đồng riêng ở PR sau.
- Chính sách áp cho các route streaming upload hiện có khi request ghi, nhưng không đổi hợp đồng upload intent, payload, giới hạn, timeout hoặc download. Không chọn email provider hoặc môi trường production trong C2a.

## Phần cần triển khai

### 1. Predicate và ranh giới kiểm

Tạo predicate dùng chung, kiểm **đúng User** và đủ bằng chứng:

- Credential `usable`, generation/operation/receipt khớp latest credential projection; status `active/proven`, generation/operation/receipt khớp latest status head active. Receipt đúng User/domain/generation/decision/reason/time; mirror confirmed không thay receipt.
- Session chưa thu hồi, generation bằng credential generation hiện hành; idle/absolute expiry lấy từ config, dùng đồng hồ mới ở lần kiểm tương ứng. Legacy NULL generation, quarantine, held hoặc disabled không cho principal.
- Integrity/hold/journal hiện hành vẫn được kiểm; không chỉ nhìn projection của snapshot hoặc chỉ business status. Entry kiểm integrity trước khóa; final check trong transaction theo thứ tự khóa thiết kế. Không gọi một kiểm integrity dùng connection riêng trong lúc chính transaction đang giữ journal lock.
- Áp cho `resolve_session`, `session_is_live`, session listing/touch, `/csrf`, `get_session_context` và login; các final caller đã gọi `session_is_live` phải nhận cùng quy tắc mới, gồm original chung.
- Lập inventory mọi caller auth/final check của file/review/chunk/search/RAG/collection/provider. Các chỗ chỉ kiểm principal lúc bắt đầu, chưa có final check hoặc đang dùng SQL session riêng phải ghi rõ bàn giao C2d. **C2a không được tuyên bố đã phủ T14 toàn hệ thống** nếu chưa chuyển các caller đó.
- Predicate chỉ quyết định auth, không nới ACL paper/private/common/review. Giữ các gate item/version/file/rights/generation và lọc trước top-k hiện có.

### 2. Login và rehash

- Verify/hash ngoài khóa. Đóng transaction đọc trước công việc chậm; lưu snapshot tối thiểu User UUID, verifier, credential generation/operation/state và status generation/operation/safety.
- Sau verify/hash, khóa tất cả User liên quan theo UUID tăng dần rồi tải lại bằng dữ liệu mới. Kiểm snapshot, predicate/head/gate và clock mới; stale verify không được gắn vào generation mới. Cách xử lý mismatch phải bounded và không phát session/cookie sai.
- Unknown User, sai mật khẩu, disabled, held hoặc quarantine giữ lỗi login công khai chung; audit chỉ ID/mã, không email nhập thử hoặc verifier. Không tuyên bố đã giải quyết timing enumeration bằng việc body giống nhau.
- Rehash chỉ sau trusted verify cùng mật khẩu và bằng CAS routine. Giữ credential generation, operation và `password_changed_at`; không auth event/receipt mới do rehash. Chỉ dùng hash policy hợp lệ do migrator quản lý; nếu CAS thua phải kiểm lại trạng thái, không cấp session dựa trên verifier cũ.
- Session mới ghim generation đã kiểm; cookie cũ dùng để replace chỉ được xử lý theo đúng session/owner và predicate/expiry. Khóa tất cả User trước session, sort session UUID; không khóa owner thứ hai sau khi đã giữ session/journal.
- Chỉ phát cookie/thành công khi commit đã xác định và kiểm gate/account/session cuối đạt. Hold hoặc journal lỗi thắng trong lúc hash/chờ khóa phải chặn, không chỉ dựa vào middleware đầu request.

### 3. Session writer và commit nền

- Chuyển issuance, touch, rotate CSRF, logout, revoke một/all session và replace sang routines/quyền runtime hợp lệ. Không cấp raw INSERT/UPDATE protected tables cho runtime để làm test qua.
- Bổ sung routine/grants cần thiết cho session mutation và **closure mirror**: C1 mới có auth-only mirror routine, trong khi batch closure cũ còn dùng ORM INSERT mirror. Kiểm bằng role runtime thật, không chạy đường chính với migrator rồi gọi đó là runtime.
- Session closure mới vẫn đi `commit_auth`, journal reason/time/User binding đúng; audit và mọi mutation liên quan cùng transaction. Session touch/CSRF rotation không tự trở thành credential/status event.
- Mở rộng nền `commit_auth` khi cần để nhận auth changes ở các PR sau, dùng `auth_safety.stage` và **một** batch auth → invitation → reset → session; chưa triển khai các endpoint writer C2b/C2c. Không commit từng event hoặc phát lại provider/delivery vì commit uncertain.
- Flush/kiểm binding trước append, toàn mirror false trước byte đầu, confirm cả batch sau append/fsync. None/torn/prefix/full-fsync-error và commit rollback/lost reply giữ đúng hợp đồng C1 và journal cũ. Replay chỉ hạn chế; không dựng User/verifier/receipt/active.
- Không báo login/logout thành công chỉ vì replay closure chạy được sau commit exception. Lỗi không được phát cookie session/CSRF mới hoặc success response; phân biệt việc xóa cookie bảo vệ với việc cấp session mới.
- Touch/final read không lấy khóa User sau session hoặc sau khóa item; theo lock order hiện có. Không giữ khóa khi hash hoặc chờ I/O ngoài; journal advisory luôn sau các khóa hàng cần thiết.

### 4. Schema/quyền và pending

- Nếu cần thay schema/routines, thêm revision sau head thực tế (hiện `0034`), **không sửa `0033/0034` đã merge**. Đồng bộ ORM/schema, quyền runtime/recovery/NOLOGIN definer, fixed search_path, revoke PUBLIC và downgrade fence.
- Không thêm routine cấp active/usable từ replay, raw auth-write grant, GUC/reason bypass, runtime TEMP/DDL/owner/BYPASSRLS hay quyền chuyển sang migrator.
- Safety vẫn pending; `/readyz` không ready và HTTP được bảo vệ/worker/beat vẫn bị gate thật chặn. Bootstrap/invitation/reset/status writer chưa chuyển vẫn đóng. Không thêm flag/env/header/CLI hoặc nhánh production để bỏ pending; không restart/nâng stack dev.

## Kiểm chứng bắt buộc

Chạy trên PostgreSQL/journal/roles tạm thật, Conda `C:\Users\ADMIN\.conda\envs\ras`; không venv. Đặt `RAS_TEST_DATABASE_ADMIN_URL` thử tường minh trước pytest, không để fixture fallback đọc `.env`. Provision runtime/recovery riêng; mọi phép thử writer quan trọng dùng runtime login thật, giữ permission tests C1.

| Nhóm | Chứng cứ cần có |
| --- | --- |
| Predicate | Usable + active/receipt/head hiện hành → login/session/read hợp lệ; quarantine/held/disabled, NULL generation, stale credential/status head và cross-User receipt/session → từ chối. Head disable/enable thiếu receipt không được dựa vào snapshot active cũ |
| Session | Issue/resolve/touch/list/rotate/logout/revoke/replace hợp lệ; ownership A/B/Admin, CSRF bound; idle 29:59/30:00, absolute 7:59:59/8:00:00 và expiry trong lúc hash/chờ khóa/final check bằng một nguồn clock kiểm soát được |
| Rehash | Cùng mật khẩu thật, policy nâng hợp lệ → CAS thắng, không generation/event mới; PHC/policy sai và CAS thua do credential/status đã đổi → không session sai; không dùng GUC/raw UPDATE |
| Race | Barrier tất định: login đang verify/hash hoặc chờ User lock, credential/status transition thắng; login thắng rồi session bị revoke/fence. Previous-cookie A/B đổi chéo, UUID lock order và session touch/closure không deadlock. Dùng transition thật qua C1/stage/commit nền trong fixture, không mock predicate hoặc tắt trigger để mô phỏng C2b |
| Batch/outcome | Session closure cùng audit, mixed auth/closure nền khi hỗ trợ; append/fsync lỗi, commit thực landed hoặc rollback nhưng mất reply → không cookie/success giả, integrity vẫn đóng đúng và recover chỉ hạn chế |
| Origin | Allowed/missing/null/sai scheme-host-port, host look-alike, CSRF thiếu/sai/bound sai. Test middleware/dependency và proxy forwarding thật ở tầng đã chuyển; request thiếu Origin không được proxy tự hợp thức hóa |
| Migration/roles | ORM/schema; upgrade DB `0034` có User/receipt/session/journal và role đã provision; giữ head/progress/pending, session binding, routine ACL và downgrade fence; raw mutation/DDL/bypass vẫn từ chối |
| Pending/integrity | HTTP pending không patch vẫn 503/health sống/ready đóng. Fixture writer chỉ thay kết quả cutover, rồi journal missing/ahead/unconfirmed hoặc hold thật vẫn chặn. Không mock `recovery.evaluate`, commit, writer, auth predicate hoặc DB guard |
| Secret/ACL | Marker mật khẩu/raw cookie/CSRF/reset token/verifier không ra journal/log/audit/error; A/B/Admin không nhận private item/file/chunk của nhau. Dùng cả ca được phép và bị chặn, không dùng global 503 làm chứng cứ ACL |

Giữ test hồi quy C1 `0033/0034`, migration và config phù hợp. Chạy test mới/nhóm liên quan, test quyền bằng runtime và các hồi quy trực tiếp bị tác động; thêm web test/typecheck/build nếu sửa web. Các kiểm đối chứng chạy tuần tự, khôi phục bytes trước suite chính, không sửa code/migration hoặc chạy mutant song song suite đang đo.

Theo hợp đồng C1–C3, test writer/predicate trực tiếp được **fixture test-only scoped** thay riêng kết quả cutover; chain/hold/integrity/DB guard phải thật. Origin có thể kiểm bằng middleware/dependency riêng; không gọi đây là HTTP auth production đã hoạt động. HTTP pending không patch là nhóm riêng; full API/proxy/browser nghiệp vụ với gate thật mở thuộc C4. Không đổi expectation cũ thành 503 hoặc skip rồi tính pass. Báo inventory đã chạy/pass/fail/chưa chạy theo file/node; số 236 C1 là lịch sử, không tự tính là pass của commit mới.

## Tài liệu, giới hạn và bàn giao

- Giữ và đưa docs orchestrator bàn giao vào PR; cập nhật trạng thái PR #33 đã merge, source head/routine/config, kết quả thực chạy, C2a đang review, phần còn đóng và C2b/C2c/C2d. Nguồn review P2 là **orchestrator**, không chủ dự án trực tiếp; ghi đúng nguồn quyết định mới.
- Không đọc/sửa `.env`, đụng tài nguyên/dữ liệu dev, gọi OpenRouter, đổi `pricing_verified`, chọn provider delivery/operator hoặc bật cutover. Dùng secret/file/journal tổng hợp ngoài repo; dọn đúng project sở hữu. Fault injection/fsync không chứng minh độ bền mất điện hoặc journal production độc lập.
- Hoàn thành khi login/session và predicate nền chạy đúng bằng runtime với ca được phép/bị chặn/race/outcome, SQL/role/schema xanh, pending/integrity còn thật và đủ inventory caller chưa chuyển. Không coi toàn C2 hoàn tất chỉ vì PR này xanh.
- Bàn giao cụ thể: chữ ký helper/routine/clock/commit, lock order, predicate normal/quarantine cho C2b, User creation/bootstrap/invitation C2c, các final caller chưa chuyển C2d. Chỉ thêm lệnh đã thực chạy vào AGENTS/dev-setup.
- Báo cuối base/commit/PR, file/migration/quyền thay đổi, quyết định triển khai và mâu thuẫn đã xử lý, lệnh/test/inventory/race và các lượt fail, trạng thái pending, tài nguyên đã dọn và giới hạn còn lại. Commit không có AI author/committer/trailer; mở PR tiếng Việt cho người review, chưa merge.
