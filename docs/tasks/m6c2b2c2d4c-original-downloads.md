# Prompt C2d-4c: tải original riêng, chung và review

**Trạng thái hiện hành:** đã triển khai trên nhánh review (code `4c49919`); [handoff](../c2d4c-original-downloads.md) và [inventory](../c2d4c-test-inventory.json) ghi kết quả kiểm theo source. Chưa merge. Nội dung giao dưới đây được giữ; trạng thái “chưa triển khai” lúc giao là lịch sử. PR #44 đã merge ngày 2026-10-06 tại `dd8bdc689ef7b495618116add4832628d7d24327`, chứa head `5504810` và source sửa P2 `adfdb72`. Head migration mã nguồn `0039`; không suy revision DB dev. Safety **pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-4c-sau-merge-pr-44-2026-10-06) · [Roadmap](../roadmap.md) · [Bàn giao PUT](../c2d4b-upload-original.md) · [Review PR #44](../reviews/pr44-c2d4b-upload-original.md)

Bạn là coding agent chính. Hoàn thành một PR **C2d-4c**: code, kiểm thử, docs, commit/push và mở PR **tiếng Việt** để review; không merge. Chuyển boundary của **đúng hai GET** với **ba nhánh quyền**:

| Endpoint | Nhánh phải hoàn thành |
| --- | --- |
| `GET /files/{file_id}/original` | Owner của file riêng; tài khoản có quyền tải đúng file chung |
| `GET /admin/review-requests/{request_id}/files/{file_id}/original` | Current Admin, đúng request review còn mở, đúng version/file đã được User gửi |

PUT/notice/intent đã merge, không làm lại. JSON review/publication writers, provider/search/RAG, xóa private, journal mới, operator/delivery C3 và cutover C4 tiếp tục riêng. Không biến việc tải thành quyền đọc chunk/RAG hoặc quyền công bố.

## 1. Base, tài liệu và môi trường

- Fetch lại, xác minh PR #44 MERGED và main chứa head trên; tạo `feat/m6c2b2c2d4c-original-downloads` từ main đã xác minh. Nếu main tiến, báo base/head và đối chiếu diff liên quan trước sửa.
- Giữ **mọi docs chưa commit** của orchestrator: review lại P2, trạng thái merge và prompt này. Đọc diff, đưa cập nhật hợp lệ vào PR; không reset/clean làm mất docs. Nếu dùng worktree, chuyển đủ và đối chiếu.
- Đọc [AGENTS.md](../../AGENTS.md), [quyền và dữ liệu](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2d-2b](../c2d2b-review-publication-json-reads.md), [C2d-3](../c2d3-library-collection-json-writes.md), [final read clock](../c2d-final-read-clock.md), bàn giao PUT và [testing](../testing.md). Không xin lại nền C1/Origin đã chốt theo ủy quyền.
- Đối chiếu code thật: `api/app/api/routes/{uploads,reviews}.py`, `common_original.py`, `api/deps.py`, `identity/{final_caller,predicates,sessions}.py`, `api/{review_publication_reads,resource_writes,upload_original}.py`, `auth_safety.py`, `authz.py`, `storage.py`, `promotion.verified_read`, models/FK/audit. Đọc `test_common_original.py`, `test_review_requests.py`, `test_review_closure_journal.py`, `test_uploads.py`, test runtime mới và hai route streaming Next.js cùng `web/lib/stream.ts`.
- Chỉ PostgreSQL/MinIO/journal/project cô lập, runtime role thật. Cấp **tường minh mọi credentials thử trước import fixture**, kể cả MinIO, để không fallback `.env`. Python/cài thư viện chỉ dùng `C:\Users\ADMIN\.conda\envs\ras`; xác minh interpreter/pip trước chạy, không tạo venv.
- Không đọc/sửa `.env`, query/migrate/restart stack dev, gọi provider/key hoặc đổi `pricing_verified`/safety readiness. Không gỡ tài nguyên có sẵn. Cleanup thử phải kiểm exact ID/name/label/image/port/mount/sole consumer trước khi gỡ, báo snapshot tài nguyên có sẵn giữ nguyên.
- Không dự kiến migration/grants mới. Nếu schema thật không đủ, báo chứng cứ trước mở rộng; revision mới sau head thực tế, không sửa revision đã merge. Giữ ORM/schema test xanh.

## 2. Hiện trạng cần kiểm và báo mâu thuẫn

Khảo sát orchestrator tại tree PR #44, chưa chạy kiểm C2d-4c:

- Nhánh private mở object trực tiếp, không checksum/spool/final caller sau I/O; Session route có thể còn transaction trong lúc stream.
- Nhánh common đã spool và checksum, nhưng kiểm caller trước SQL quyền cuối, sau đó audit/commit rồi stream mà chưa có proof mới. Proof trước commit không cấp quyền cho response sau commit.
- Nhánh review spool không dùng giới hạn spool chung và chỉ recheck grant; thiếu current Admin/session/clock cuối và proof sau audit commit. ORM hết hạn sau rollback có thể lazy-load DB trong lúc I/O nếu không chụp scalar trước.
- `VerifiedFileResponse` đã xử lý disconnect và iterator chưa đọc; giữ regression/fallback đã có, không quay về generator chỉ cleanup trong `finally` hoặc trông chờ background task/finalizer.
- Proxy download riêng stream nhưng chưa chuyển `Set-Cookie` hoặc signal hủy downstream. Cần kiểm hành vi thật; build/typecheck không chứng minh browser/stream release đúng.

Trước sửa, viết map transaction/lock/I/O/audit cho từng nhánh và chỉ ra hành vi docs/code lệch. Không coi một yêu cầu hoặc nhánh là đã hoàn thành chỉ nhờ test cũ, checksum giống nhau hoặc principal ở đầu request.

## 3. Lựa chọn trong phạm vi task

**Orchestrator chốt theo ủy quyền cho C2d-4c:** cả ba nhánh tải từ **bản spool đã kiểm size + SHA-256 trước byte đầu**. Dùng chung pool `files.verified_download_max_concurrent` hiện có (mặc định 4 **mỗi process API**), độc lập pool upload. Nhánh private được bổ sung kiểm bytes; common/review giữ checksum đúng file/rights/request. Các giá trị tài nguyên là cấu hình ban đầu cần đo, không SLO hay rate limit toàn hệ thống.

Tách helper kiểm bytes/ownership khỏi helper quyền: tái dùng cơ chế spool/response khi phù hợp, **không** đưa review grant vào ordinary file route. Không thêm private audit mới nếu task không cần; giữ audit tải common/review hiện hành, chỉ IDs/mã.

### A. Proof ngắn trước storage

- Giữ entry dependency session/role. Principal đầu request chỉ định danh; mỗi boundary lấy exact session/User/latest confirmed heads/receipts/generation/status/current role từ SQL thật và clock callable mới.
- Short read proof dùng current caller và SQL gate đúng nhánh; pin **scalar immutable target** (item/version/file/key/checksum/size/MIME/filename/epoch, request và checksum review khi có). Không giữ ORM object có thể lazy-load sau rollback hoặc chuyển dependency Session qua thread có thể sống lâu hơn request.
- Resource locks **trước journal shared**: ordinary private/common khóa item `FOR SHARE`; review khóa **request SHARE → private item SHARE** qua exact grant. Khóa thêm version/file nếu cần phải có lý do/thứ tự, trước journal; không khóa tombstone sau item hoặc User/session sau journal.
- Requery scalar target/gate bằng current caller, rồi `final_caller`/clock **sau mọi SQL quyền/binding cuối**, kể cả chính SQL predicate caller. Role Admin mới chỉ bắt buộc ở review; owner gate của ordinary private giữ hành vi hiện hành, Admin không có quyền đọc private của người khác.
- Nhả transaction/connection/locks **trước** storage/temp I/O. Missing/foreign/malformed file/request và sai grant/rights trả cùng `404 not_found`; current caller mất hiệu lực `401 session_expired`/clear cookie, current role thiếu quyền Admin `403`, gate/integrity unavailable `503`. Không open object cho request không qua gate.

### B. Spool và xác minh ngoài DB

- Giữ slot từ lúc cấp tới khi mọi thread/spool/response đã kết thúc. Stream object theo chunk vào temp, không đọc toàn bộ PDF vào RAM; không giữ DB transaction/connection/khóa khi open/read/hash/write/close storage hay chờ client. Dừng khi vượt `byte_size`; so **size và SHA-256**, không dùng HEAD/ETag làm bằng chứng thay bytes.
- Không thay object, liên kết file, intent/job, quyền hay trạng thái index. Khi integrity lỗi, **không xóa original** để sửa lỗi. Private dùng checksum upload, review khớp cả request checksum, common khớp target và gate rights/publication. Spool chính là bytes sau đó sẽ stream; không đọc lại object để gửi.
- Giữ mã common `409 common_file_integrity_failed`, review `409 review_file_integrity_failed`; nhánh private dùng `409 private_file_integrity_failed`. Storage lỗi `503 file_unavailable`; slot hết `503 download_busy`. Private object thiếu/sai bytes dùng integrity error, không trả payload storage/internal URL. Đối chiếu normalization object-missing hiện hành ở common/review, báo và giữ hợp đồng đã có nếu không cần thay.
- Temp tạo lỗi, storage thiếu/read lỗi, spool write lỗi và close lỗi phải giải phóng tài nguyên có thể giải phóng được mà không che quyết định deny thành response có bytes. Nếu thread còn chạy khi HTTP bị hủy, ownership vẫn thuộc thread/attempt tới settled; không close spool dưới thread hoặc trả slot sớm. Chặn I/O trong test để chứng minh logout/disable/revoke/withdraw không phải chờ storage.
- Timeout SDK từng thao tác không phải deadline tổng. Giữ cấu hình SDK hiện hành, ghi thời gian chờ byte đầu gồm verify + auth; không hứa timeout proxy hủy handler hoặc thread treo được kill an toàn. Không mở thêm Range/HEAD, URL ký sẵn, cache public hay content preview trong PR này.

### C. Audit và proof mới trước phát file

Private không có audit commit mới: sau spool, transaction mới khóa item → resources cần thiết → journal shared; current caller, exact owner/version/file/target/gate, clock sau SQL cuối. Kết thúc transaction trước gửi response, không commit mutation hoặc gia hạn session trong helper đọc.

Common/review giữ audit thành công **trước phát**, theo hai transaction:

1. **Audit transaction:** writer entry integrity trước locks; **caller User NO KEY UPDATE → request SHARE (review) → item SHARE → resources cần thiết → audit/FK flush → journal exclusive trực tiếp**. Không gọi helper shared rồi nâng khóa. Dùng predicate caller/current role cho GET, **không** thêm yêu cầu CSRF/Origin của POST vào download GET. Recheck scalar gate/target và clock cuối sau SQL; giữ locks tới commit. Audit vẫn chỉ ghi IDs/mã, không title/filename/checksum/key/text. Denied/failure audit best effort không mở quyền hoặc che denial.
2. **Chỉ sau known audit commit:** Session/transaction mới theo lock map đọc A, recheck current caller + gate + exact target, clock sau SQL cuối. Không dùng proof/ORM từ transaction trước. Nếu quyền hết trong audit commit hoặc trước proof mới: không header/body file, giữ audit có thể đã ghi và trả denial. Từ audit commit tới response phải có proof mới trên transaction thật.

Outcome audit phải được giữ độc lập rollback/close như bài học P2 PR #44: commit exception/uncertain/teardown chưa xác định **không phát file**, không tự retry audit mutation hoặc suy commit từ SELECT. Không xóa original. Có thể đã có audit dù response lỗi; ghi rõ mã `503` có kiểm soát và giới hạn, không hứa chưa ghi gì. Resource cleanup thử hết khả năng không được trả bytes sau lỗi.

Tái dùng predicate/caller phù hợp, không dùng `_actor` writer bắt CSRF cho GET hay principal.role cũ. Không giữ journal/resource lock xuyên storage hoặc streaming. Hợp đồng này không chuyển các POST review/publication chưa giao sang final writer mới.

### D. Stream, proxy và thời điểm thu hồi

- Headers từ target đã được proof cuối kiểm: Content-Type, Content-Length, Content-Disposition an toàn, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. Không lộ key/S3 URL, token, provider hoặc nội dung lỗi. Dùng `VerifiedFileResponse` hoặc owner/response tương đương đã kiểm đầy đủ; finalizer chỉ là fallback.
- Boundary cuối là lần xác minh caller/gate/target/clock dưới locks trên **transaction response mới** (với nhánh audit). Khi closure/revoke thắng trước boundary, không file headers/bytes. Nếu boundary thắng, response có thể truyền tiếp sau khi nhả locks và sau `t_rev`; bytes đã gửi không thu hồi được. **Không hứa recheck mỗi chunk hoặc hủy hồi tố.** Request mới sau closure/revoke, kể cả URL/cookie cũ, phải bị chặn.
- Giữ hai route streaming same-origin riêng, không chuyển download sang proxy JSON hoặc buffer cả PDF. Kiểm link mở/download qua proxy, không chỉ gọi FastAPI. Khi API clear cookie, proxy phải chuyển Set-Cookie hợp lệ theo helper hiện hành; không làm mất cookie của auth response khác hoặc forward cookie/CSRF ra storage.
- Nối downstream abort/cancel với upstream fetch/ReadableStream khi phù hợp, kiểm nó trả slot/temp sau executor settled. Không chỉ dựa vào background task hoặc việc GC xảy ra sớm. Lỗi sau file headers bắt đầu phải kết thúc stream lỗi; không chèn JSON vào bytes file hay chuyển partial body thành thành công đủ length. Không thêm auto retry tải khi audit outcome chưa rõ.

## 4. Kiểm thử cần có

Fixture business dùng bootstrap/invitation/login/runtime/head/receipt/journal thật; scoped cutover chỉ trong fixture. **Pending HTTP riêng không patch** giữ `/healthz=200`, `/readyz=503`, các download `503` không I/O/audit nghiệp vụ. Không principal giả, bypass auth/grants/quarantine/held hoặc sửa expectation để qua.

| Nhóm | Tiêu chí kiểm chứng |
| --- | --- |
| ACL ba nhánh | A/B/Admin, cùng bytes/checksum nhưng item khác; private owner; common listed + đúng rights/publication/file/epoch/audience/permits/no conditions; pending/revoked/metadata-only; review exact open version/file, request thiếu/đã đóng/epoch lệch/closed_by_recovery. `metadata_only` common có quyền tải vẫn tải được, không suy quyền từ search/index. Ordinary route không nhận review grant |
| Caller/auth | Logout/revoke/disable, idle/absolute tại biên và sát trước biên, generation/head/receipt/status held/quarantine/binding, đổi role review Admin → User. Thay đổi giữa proof đầu, read bị chặn, audit trước/sau commit, response SQL cuối; current role phù hợp từng nhánh, không deny owner chỉ vì đổi role mà owner gate vẫn cho phép |
| Clock cuối | Chặn gate SQL và chính predicate session vượt idle/absolute: hết hạn → 401/no bytes, sát trước hạn → success; không dùng `now` đầu request hoặc gia hạn idle bằng helper cuối |
| Integrity và I/O | PDF/Markdown đúng SHA; bytes khác cùng size, thiếu/thừa bytes, missing/read/write/temp lỗi. Pin scalar tránh ORM lazy-load; không checked-out connection/transaction/locks trong storage. Revoke/withdraw/auth writer commit được khi read bị chặn |
| Audit/postcommit | Known commit rồi actor/grant mất quyền; commit rollback/landed-uncertain + close/rollback lỗi; no file bytes, không retry hoặc xóa original; pool/slot/temp release. Audit action/IDs/mã giữ, không dữ liệu nhạy cảm |
| Race | Withdraw/reject/approve versus review download, revoke/epoch/publication/rights versus common download, caller versus audit/response. Barrier tất định cho cả hai thứ tự; locks/watch DB chứng minh không deadlock. Closure trước final boundary chặn; final boundary thắng được stream tiếp, request sau bị chặn |
| Ownership/stream | Pool dùng chung ba nhánh, hết slot, tempfile create fail, iterator chưa đọc/response không gửi, stream xong, client ngắt trước/sau chunk đầu, send header/body lỗi, HTTP cancel và thread còn chạy. ASGI 2.3/2.4 với event tất định, không assertion phụ thuộc chunk thắng disconnect; lần tải sau lấy được slot và file tạm đóng |
| Proxy/browser | Đúng hai route streaming qua web, lỗi 401 clear cookie/404/409/503, không byte file trên deny; cancel upstream/slot release. Ít nhất một tải thành công mỗi nhánh với A/B/Admin phù hợp, checksum tải khớp; smoke Chrome khi công cụ có, ghi rõ nếu chỉ HTTP hoặc handler Node |
| Hồi quy | C2d-4b **gồm 10 P2 regression**, consent/intent liên quan, common original/review hiện có, final read clock, login/session/auth safety và ORM/schema khi liên quan; giữ journal closure/session/token/invitation. Không lấy số cũ làm pass source mới |

- Bộ runtime cũ của common/review có thể dùng SQL fixture auth hoặc CSRF expectation không phù hợp read GET; chuyển fixture sang hợp đồng thật khi cần, ghi lý do. Không bỏ assertion quyền/bytes/audit/disconnect hoặc đổi expected failure thành pass để bỏ risk.
- Tạm bỏ từng guard trọng yếu ngoài suite cuối (private checksum, exact review gate/current role, current caller/clock cuối, post-audit proof mới, release response/cancel) để kiểm đối chứng; khôi phục byte/hash trước lượt kiểm cuối, không chạy mutant song song suite.
- API chọn theo risk, chạy đủ ca mới và hồi quy nêu lý do. Inventory current collect + selection + unique JUnit + not-run; nếu chia process, đối chiếu thiếu/trùng. Chốt code SHA/hash trước/sau, không sửa code trong suite. Báo rõ failure/preparation/fault/mutant/skip/not-run, không cộng 463/76/51 của PR #44 vào lượt mới.
- Nếu đổi web/proxy, chạy test/typecheck/build liên quan. Runtime backend/MinIO qua proxy phải có chứng cứ, browser chưa thử ghi thẳng. Có thể dùng file tổng hợp gần 30 MiB để kiểm bounded stream/slot và ghi môi trường/thời gian một lần; không coi đó là benchmark p95 hoặc SLO.

## 5. Docs, giao PR và hoàn thành

- Thêm `docs/c2d4c-original-downloads.md` và inventory JSON: route/branch/lock/I/O/audit/commit/response map, headers/error/retention/resources, evidence/source/not-run, fault limits và boundary thu hồi.
- Cập nhật caller inventory cho đúng hai GET/ba nhánh, không ghi caller writer/provider khác là hoàn tất. Đồng bộ docs README, root README, access-and-data, testing, decisions, roadmap, dev-setup; đánh dấu PR #44 merged, giữ nguyên số/repro lịch sử theo source. AGENTS chỉ dẫn hợp đồng hiện hành, không ghi lệnh chưa chạy hoặc biến cấu hình đề xuất thành luật vĩnh viễn.
- Kiểm link/anchor/control characters/diff và ORM/schema. Đóng/dọn tài nguyên thử sau ownership check; báo dev/resources có sẵn giữ nguyên. Artifact/journal/paper/credentials nằm ngoài repo, secret không log/commit.
- Commit giữ author/committer đã cấu hình, không thêm AI co-author/trailer. Đã được giao **commit/push/mở PR tiếng Việt** cho người review; không xin lại quyền, **không merge**. Báo base/head/code SHA, files, API/web/mutant/smoke pass/fail/not-run, mọi lỗi đã gặp, cleanup và giới hạn.

**Hoàn thành:** hai endpoint/ba nhánh giữ quyền riêng biệt, bytes được băm vào spool ngoài DB và hữu hạn theo pool, caller/gate/target/clock cuối đúng transaction, audit known commit có proof mới trước phát, cancellation/teardown không mất ownership; test và docs/evidence tương ứng đạt. Không mở readiness pending, không gọi provider hoặc tuyên bố restore/production/mất điện an toàn. Bàn giao writer review/publication và provider callers còn lại cho task riêng; C3/C4 vẫn mở.
