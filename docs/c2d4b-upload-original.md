# C2d-4b: nhận original và consume intent

**Cập nhật sau merge:** PR #44 đã merge `dd8bdc6` ngày 2026-10-06, chứa head `5504810` và source P2 `adfdb72`. [C2d-4c — original downloads](c2d4c-original-downloads.md) đã triển khai trên nhánh review, code `4c49919`; kết quả mới trong [inventory riêng](c2d4c-test-inventory.json). Không chạy lại suite tại merge hoặc suy revision DB dev; safety pending tới C4. Những trạng thái OPEN bên dưới là snapshot lịch sử tại source kiểm.

**Review lại P2 tại `5504810` (code `adfdb72`):** không phát hiện điểm chặn merge còn lại trong phạm vi C2d-4b. Reviewer độc lập **76/76 API** và hai ca tái hiện/control riêng đạt; đã đối chiếu 463 artifact agent và 262 source hashes. [Report mới](reviews/pr44-c2d4b-upload-original.md#review-lại-p2-tại-5504810) tách số và giới hạn khỏi lịch sử dưới đây. PR OPEN/chưa merge, safety **pending tới C4**; download C2d-4c riêng. Web không đổi, không chạy lại.

**Review độc lập PR #44 tại `b578082` (trước sửa P2):** 230/230 API chọn (đủ 220 mới + 9 legacy runtime + ORM), 51/51 web/typecheck đạt; [report](reviews/pr44-c2d4b-upload-original.md). **P2 chặn merge:** Session cleanup lỗi sau commit exception che mất uncertain, làm attempt xóa object. Ca bổ sung fail đúng lỗi, control pass; tại source review, hợp đồng giữ object chưa bảo đảm ở nhánh lỗi kép này. Bản sửa và kiểm mới giữ riêng dưới đây. PR vẫn OPEN/chưa merge, safety pending tới C4. Kết quả agent giữ riêng theo source.

Base fetch `632ec683940a8bccc711c8a55743b0a3992c1d03`; GitHub CLI xác minh PR #43 MERGED, main chứa `f60645edf01d01794590db256d0ea4b5b1ef5c5e`. Nhánh `feat/m6c2b2c2d4b-upload-original` tạo từ `origin/main`; giữ toàn bộ docs chưa commit, gồm review PR #43 và task. Source migration `0039`, không migration/grants/claim lease mới hoặc suy revision DB dev. **Safety pending tới C4; chưa merge.**

[Task](tasks/m6c2b2c2d4b-upload-original.md) · [Mục lục](README.md) · [Caller inventory](c2d1-caller-inventory.json) · [Test inventory](c2d4b-test-inventory.json)

## Đối chiếu hành vi và boundary

Route cũ khóa intent UPDATE xuyên body/parser/S3; dùng `now` đầu request, thiếu actor/consent proof sau I/O, thiếu SHA-256 readback và response proof mới. Cleanup cũ có thể xóa object trong nhánh commit exception sau một SELECT chưa thấy tham chiếu. Boundary mới ở [api/upload_original.py](../api/app/api/upload_original.py), route vẫn đi principal/session/CSRF/Origin thật. Upload chỉ dành cho current role User, mặc định private; không tự công bố, index, chạy worker hay provider trong request.

Intent vẫn được tạo bằng JSON trước file. `_check` của C2d-4a giữ unused-only; `_consumed` riêng chỉ nhận đúng binding/item/used_at của attempt vừa commit. Missing/foreign/wrong-session/used/expiry/notice cũ tại proof ban đầu trả `404 invalid_intent`; giữa pipeline expiry/binding đổi trả `409`. Auth/CSRF/current role/gate giữ denial tương ứng; không log filename/token/hash/text.

Tự rà soát phát hiện settings snapshot đầu request chưa bắt được cấu hình notice đã nạp lại khi staging đang chạy. Regression chuẩn trước sửa: 5/5 fail đúng 201 thay vì 409 (parser/put/readback/consume/response). PUT nay còn đọc `get_settings().upload_notice_version` tại mỗi boundary và sau SQL cuối của consume/response; 5/5 đạt sau sửa. Không thêm cơ chế hot reload hay sửa helper unused C2d-4a. Các lần triển khai phải đồng bộ cấu hình notice của process; không suy một env chưa nạp lại là cấu hình active đã đổi.

## Boundary và lock map

| Phase | Transaction, khóa và clock |
| --- | --- |
| Trước body | Session riêng trong thread: intent SHARE → ack SHARE → journal shared → exact scalar binding, caller/current User/CSRF/latest heads/receipts/generation → clock sau SQL cho caller và intent. Rollback/close trả connection trước body |
| Receive/parser | Stream vào tempfile theo size config/khai báo, SHA-256; write/parser trong thread, không DB connection/transaction. PDF probe không chứng minh body coverage; worker giữ quyết định needs_review/index |
| Trước put | Proof đọc mới cùng lock map, cùng immutable metadata/notice/consent; đóng transaction trước put |
| Storage/readback | Key ngẫu nhiên riêng mỗi attempt. Put và open/stream size+SHA-256 trong thread, không DB; sai bytes cùng size bị từ chối. Không dùng HEAD/ETag thay băm |
| Writer | Entry integrity → User NO KEY UPDATE → intent UPDATE → ack SHARE → current actor/binding → item private → version → upload job → SourceFile.job_id → consume + audit/flush mọi FK → journal exclusive trực tiếp → scalar consumed binding + current actor + clock sau SQL, intent còn hạn → một commit |
| Response known commit | Session/transaction mới: private owner item SHARE → job SHARE → intent SHARE → ack SHARE → version SHARE → file SHARE → journal shared → exact consumed binding + SQL item/version/file/job/owner/checksum/size/mime/pages → current actor/CSRF/clock sau SQL. Không User lock hoặc write sau journal |

Response giữ IDs và `state=received` như biên nhận upload. Không buộc job còn received hay item còn metadata_only, không ghi lại trạng thái worker. Intent hết hạn sau consume hợp lệ không đảo ngược item đã nhận; session/current User vẫn phải còn live ở response. Logout sau commit không hủy job. Response đã qua boundary có thể tiếp tục gửi sau khi nhả khóa; bytes đã gửi không lấy lại được.

## Outcome, cancellation và cleanup

| Outcome | Object / response |
| --- | --- |
| I/O lỗi trước acceptance, hoặc precommit có rollback xác nhận | Rollback business rows; best effort SELECT SourceFile trên transaction riêng rồi đóng connection trước delete key riêng. DB không trả lời thì giữ orphan |
| Commit exception, kể cả landed/lost reply | `503 upload_outcome_uncertain`, không IDs/retry; giữ object dù một SELECT chưa thấy tham chiếu |
| Known commit, teardown lỗi | `503 upload_response_unavailable`, không IDs/retry; giữ rows/object |
| Acceptance lỗi chưa phân loại/rollback chưa xác nhận | `503 upload_outcome_uncertain`, không IDs/retry; giữ object, không suy rollback qua SELECT |
| Known commit, hậu kiểm thất bại | Giữ rows/object; auth/role/gate/binding denial hoặc `upload_response_unavailable`; không trả IDs hoặc hứa chưa lưu |
| HTTP task cancellation trước acceptance | Báo stop cho attempt được shield; không consume nếu cancellation/disconnect đã biết. Slot/temp thuộc attempt tới khi thread settle |
| Cancellation khi commit thread đã chạy | Không delete/close file dưới thread; không trả DTO cho caller đã cancel. Đợi thread settle trong attempt riêng, known commit hoặc uncertain đều giữ object |

Regression hủy đồng thời mọi asyncio Task tái hiện 2/2 fail (put/commit): wrapper `to_thread` bị cancel làm slot trả sớm dù executor chưa xong. Boundary nay shield executor Future trực tiếp, giữ contextvars, đợi completion thật trước cleanup; kiểm request cancellation và cancel-all riêng. Đây là cancellation trong process đang sống, không bảo đảm host/process bị kill cứng.

Mỗi blocking phase tự sở hữu file/Session; dependency Session không được chuyển vào thread có thể sống lâu hơn HTTP request. Registry giữ attempt task và thu exception không log nội dung. Slot chỉ trả sau cleanup/temp close và khi thread kết thúc; thread bị treo giữ slot để không vượt concurrency thực tế. Không hứa proxy timeout hủy backend hay có tổng deadline cứng cho toàn request.

`cli.reconcile_originals` giữ cách chạy thủ công sau grace và khi mọi attempt/I/O/commit cũ đã kết thúc; luôn giữ key có SourceFile. Không thêm scheduler/quét bucket đang ingest. Multipart chưa hoàn tất không hiện trong list_objects; chưa có cơ chế riêng abort/expiry multipart, operator phải đối soát khi môi trường đã quiescent. Process/host crash cần reconcile; fault injection không chứng minh mất điện an toàn.

## Config và proxy/UI

`files.upload_max_concurrent=4` mỗi process API, pool riêng download; `files.upload_body_timeout_seconds=90`. Đây là giá trị ban đầu cần đo, không SLO/rate limit toàn hệ thống. PDF 30 MiB/100 trang, Markdown 2 MiB; probe 10 giây/256 MiB, Linux child có CPU/address-space limit, Windows host không có RLIMIT memory.

Storage SDK dùng config connect 10 giây, read 30 giây, tối đa 2 attempt theo retry standard. Các timeout giới hạn từng thao tác/socket, không tổng put/readback; multipart/chunk/retries nhiều bước có thể vượt 120 giây. SDK retry không replay business mutation. Body timeout có thể chờ disk thread settle trước khi trả slot. Upload không đọc cả PDF bằng `read()`; Markdown bounded vẫn decode toàn file ≤ limit.

Proxy Next giữ body streaming, không buffer PDF; timeout 120 giây. Proxy lỗi 502 dùng upload_outcome_uncertain; form coi lỗi mạng/5xx/response thiếu IDs là chưa xác định hoặc response unavailable, đề nghị kiểm thư viện, không tự PUT lại/tạo intent mới. Auth denial sau known commit cũng không chứng minh file chưa được nhận; UI nêu khả năng đó. Checkbox reset mỗi lượt. Original download giữ task C2d-4c.

## Sửa P2: outcome độc lập với teardown

Source sửa `adfdb722fdd0de529b45addddd516872e3b404ac`, trên cùng PR #44; [review gốc](reviews/pr44-c2d4b-upload-original.md) giữ kết quả tại `b578082`. Không đổi SQL ACL/clock/lock map, schema/grants, download hoặc web.

Attempt sở hữu `AcceptanceOutcome` riêng với ba mốc: commit_started đặt **trước** commit; committed chỉ đặt khi commit trả thành công; rollback_confirmed chỉ đặt khi rollback trả thành công **và chưa từng gọi commit**. Teardown không thể xóa hai mốc commit. `accept()` quản lý Session tường minh thay vì để `__exit__` che exception; luôn thử rollback rồi close, fallback invalidate/discard connection khi close lỗi. Đọc outcome sau thread settled; cancellation vẫn giữ object bảo thủ.

Attempt quyết định cleanup từ outcome, không từ ApiError/code cuối: commit đã bắt đầu hoặc không có known precommit rollback thì giữ object. Known commit + teardown lỗi không phát proof/IDs, trả response unavailable. Precommit rollback xác nhận vẫn dọn key riêng dù close lỗi; rollback không xác nhận giữ orphan. Invalidate là giải phóng connection, không retry mutation hoặc chứng minh commit từ xa. Nếu driver/OS làm mọi cleanup transport đều thất bại, không hứa release vật lý tức thời; state/object vẫn được giữ.

Tám regression thực trên runtime PostgreSQL/MinIO: commit lỗi + close, commit lỗi + rollback/close, landed/lost reply + close, known commit + close hoặc rollback/close, precommit control normal/close/rollback-close. Trước sửa **7 fail/1 pass**, sau sửa **8 pass**; hai ca bổ sung acceptance exception chưa phân loại trước acceptance/sau known commit cũng pass. Kiểm exact error/no IDs, một put/tối đa một commit, rows/object/delete, transaction/pool/temp/slot/attempt release. Fault harness cho SQL rollback/landed để tạo trạng thái kiểm, API chỉ quan sát commit exception; không chứng minh remote commit thật còn in flight.

Lượt đầu sau chỉnh gặp 1 collection error do indentation ở accept; sửa, py_compile đạt, chạy lại 8 rồi 10 regression đạt. Không tính lượt lỗi hoặc focused vào số suite cuối. **Suite cuối 463/463 pass, 0 fail/error/skip**: 239 upload (230 C2d-4b, gồm 10 regression P2, + 9 legacy), 155 consent, 68 runtime login/session + 1 ORM schema. Collect 2.915, **2.452 not-run**. [Inventory, phần `p2_fix`](c2d4b-test-inventory.json) giữ source/JUnit/selection và 262 source hash trước/sau bằng nhau. Web không đổi; không rerun test/typecheck/build web, giữ số lịch sử riêng. Đã dọn hai container/hai exclusive volume P2 theo ID/name/label/image/port/mount/consumer; 24 container có sẵn giữ ID/image/StartedAt. Xóa ba file credentials sinh riêng, giữ journal/JUnit/evidence ở `%TEMP%/ras-pr44-p2-20261006`. Không đổi hoặc cộng các số 808 ban đầu, 230 reviewer hay 51 web lịch sử. Safety pending tới C4, chưa merge; C2d-4c riêng.

## Kiểm chứng theo source ban đầu (trước P2)

Source code vòng ban đầu `68950f3357e740bed6030af53dc385bcef5de585`, base `632ec68`; commit bàn giao ban đầu `b578082` chỉ docs/inventory. Fetch cuối main vẫn cùng base. Collect toàn API **2.905 node**, selection/JUnit unique **808/808 pass**, **0 fail/error/skip**, **2.097 not-run**. Ba process test cô lập chạy selection rời nhau trên cùng source, hash trước/sau của 262 file Python/SQL/web bằng nhau; không sửa code hoặc chạy mutants đồng thời suite.

| Nhóm cuối | Pass |
| --- | ---: |
| PUT original: 220 ca mới + 9 legacy runtime | 229 |
| Toàn C2d-4a notice/intent | 155 |
| Hồi quy caller/clock/writer/login/Origin/config/migration/ORM | 424 |

Web **51/51 pass**, typecheck và build đạt; web không đổi sau `6981643`. Smoke HTTP thật chạy Uvicorn cô lập và handler TS Node proxy streaming; form qua callback thật, **chưa browser DOM**. Pending không patch trả 503, health 200/readiness 503. AST parser/download và auth/authz/identity/models/migrations giữ base; ORM schema test đạt, không suy download boundary đã chuyển.

Bốn đối chứng bỏ readback/response binding/keep uncertain/preput proof: **4/4 bị bắt**, mỗi ca đúng một failure, không error/skip; bytes/hash source khôi phục trước suite cuối. Hai lỗi sản phẩm được regression tái hiện trước sửa: notice reload 5 fail → 5 pass; cancel-all 2 fail → 12 pass gồm 10 abort cũ. Lượt chuẩn bị khác: 46 pass/1 fail do fixture dùng state item bất hợp lệ, sửa sang worker thật và progression hợp lệ rồi 49 pass; các lượt thêm 15 và 11 pass. Suite `0f7c579` đã có 806 pass nhưng giữ lịch sử riêng, không cộng vào 808.

Một lượt prepare bị ngắt, collect ban đầu 2.879 + 1 lỗi import `scripts` (đã sửa runner sys.path), và lượt final đầu bị dừng để sửa notice. Lần chỉnh selection qua stdin dùng sai `__file__`/count cũ khiến run selection cũ khởi chạy, đã dừng đúng process thử; log bị ghi đè, không còn tổng hoàn chỉnh và không tính pass. Mutant lần đầu vướng anchor CRLF sau một guard đã bị bắt; restore source rồi chuẩn hóa anchor và chạy đủ bốn. [Inventory](c2d4b-test-inventory.json) giữ node, JUnit/source/log SHA, preparation và not-run riêng.

Conda `C:\Users\ADMIN\.conda\envs\ras` interpreter/pip đã xác minh. PostgreSQL `ras-c2d4b-db-20261006` bind 58455 và MinIO `ras-c2d4b-minio-20261006` bind 59455, label `ras.task=c2d4b-20261006`; journal/credentials/evidence ngoài repo `%TEMP%/ras-c2d4b-20261006`. Credentials DB/admin/runtime/recovery/MinIO tường minh trước import fixture; runner chặn external httpx và urllib3 transport. Chỉ scoped cutover ở fixture nghiệp vụ; pending HTTP riêng không patch. Không đọc/sửa `.env`, tác động dev/provider/pricing/safety ready.

Fixture `test_uploads.py` cũ tạo User qua SQL trực tiếp: đổi sang bootstrap/invitation/login runtime thật, giữ assertions private/ACL/format/composite FK/race. Expectation cũ xóa object khi commit exception mâu thuẫn task mới: đổi ca đó thành giữ orphan, không retry. Các file download được thử hồi quy không chứng minh C2d-4c hoàn tất.

Đã dọn đúng hai container và hai volume thử sau kiểm ID/name/label/image/port/mount/sole consumer; 24 container có sẵn giữ nguyên ID/image/StartedAt. Ba file credentials sinh riêng đã xóa, journal/JUnit/log/evidence giữ ngoài repo. `git diff --check`, 49 Markdown/1.057 link và mọi caller contract-map file/anchor đạt. Warning hiện có: Starlette/httpx TestClient deprecation, Alembic computed default `users.status_domain`, Node module type; không đổi dependency trong task này.

Full business API/cutover/C3/C4, provider/search/RAG, production durability và benchmark chưa thuộc kết quả này. Không consent journal xuyên restore, không thay nền C1/Origin. Bàn giao original download C2d-4c riêng sau review.
