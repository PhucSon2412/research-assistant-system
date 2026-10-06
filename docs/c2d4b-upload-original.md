# C2d-4b: nhận original và consume intent

Base fetch `632ec683940a8bccc711c8a55743b0a3992c1d03`; GitHub CLI xác minh PR #43 MERGED, main chứa `f60645edf01d01794590db256d0ea4b5b1ef5c5e`. Nhánh `feat/m6c2b2c2d4b-upload-original` tạo từ `origin/main`; giữ toàn bộ docs chưa commit, gồm review PR #43 và task. Source migration `0039`, không migration/grants/claim lease mới hoặc suy revision DB dev. **Safety pending tới C4; chưa merge.**

[Task](tasks/m6c2b2c2d4b-upload-original.md) · [Mục lục](README.md) · [Caller inventory](c2d1-caller-inventory.json)

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
| Precommit denial/SQL/partial put, thread đã settled | Rollback business rows; best effort SELECT SourceFile trên transaction riêng rồi đóng connection trước delete key riêng. DB không trả lời thì giữ orphan |
| Commit exception, kể cả landed/lost reply | `503 upload_outcome_uncertain`, không IDs/retry; giữ object dù một SELECT chưa thấy tham chiếu |
| Known commit, hậu kiểm thất bại | Giữ rows/object; auth/role/gate/binding denial hoặc `upload_response_unavailable`; không trả IDs hoặc hứa chưa lưu |
| HTTP task cancellation trước acceptance | Báo stop cho attempt được shield; không consume nếu cancellation/disconnect đã biết. Slot/temp thuộc attempt tới khi thread settle |
| Cancellation khi commit thread đã chạy | Không delete/close file dưới thread; không trả DTO cho caller đã cancel. Đợi thread settle trong attempt riêng, known commit hoặc uncertain đều giữ object |

Mỗi blocking phase tự sở hữu file/Session; dependency Session không được chuyển vào thread có thể sống lâu hơn HTTP request. Registry giữ attempt task và thu exception không log nội dung. Slot chỉ trả sau cleanup/temp close và khi thread kết thúc; thread bị treo giữ slot để không vượt concurrency thực tế. Không hứa proxy timeout hủy backend hay có tổng deadline cứng cho toàn request.

`cli.reconcile_originals` giữ cách chạy thủ công sau grace và khi mọi attempt/I/O/commit cũ đã kết thúc; luôn giữ key có SourceFile. Không thêm scheduler/quét bucket đang ingest. Multipart chưa hoàn tất không hiện trong list_objects; chưa có cơ chế riêng abort/expiry multipart, operator phải đối soát khi môi trường đã quiescent. Process/host crash cần reconcile; fault injection không chứng minh mất điện an toàn.

## Config và proxy/UI

`files.upload_max_concurrent=4` mỗi process API, pool riêng download; `files.upload_body_timeout_seconds=90`. Đây là giá trị ban đầu cần đo, không SLO/rate limit toàn hệ thống. PDF 30 MiB/100 trang, Markdown 2 MiB; probe 10 giây/256 MiB, Linux child có CPU/address-space limit, Windows host không có RLIMIT memory.

Storage SDK dùng config connect 10 giây, read 30 giây, tối đa 2 attempt theo retry standard. Các timeout giới hạn từng thao tác/socket, không tổng put/readback; multipart/chunk/retries nhiều bước có thể vượt 120 giây. SDK retry không replay business mutation. Body timeout có thể chờ disk thread settle trước khi trả slot. Upload không đọc cả PDF bằng `read()`; Markdown bounded vẫn decode toàn file ≤ limit.

Proxy Next giữ body streaming, không buffer PDF; timeout 120 giây. Proxy lỗi 502 dùng upload_outcome_uncertain; form coi lỗi mạng/5xx/response thiếu IDs là chưa xác định hoặc response unavailable, đề nghị kiểm thư viện, không tự PUT lại/tạo intent mới. Auth denial sau known commit cũng không chứng minh file chưa được nhận; UI nêu khả năng đó. Checkbox reset mỗi lượt. Original download giữ task C2d-4c.

## Kiểm chứng theo source

Đang hoàn tất kiểm cô lập; số cuối, selection/JUnit/source/hash/not-run sẽ được ghi sau khi chạy. Không dùng số lịch sử PR #43 làm pass C2d-4b.

Conda `C:\Users\ADMIN\.conda\envs\ras` interpreter/pip đã xác minh. PostgreSQL `ras-c2d4b-db-20261006` bind 58455 và MinIO `ras-c2d4b-minio-20261006` bind 59455, label `ras.task=c2d4b-20261006`; journal/credentials/evidence ngoài repo `%TEMP%/ras-c2d4b-20261006`. Credentials DB/admin/runtime/recovery/MinIO tường minh trước import fixture; runner chặn external httpx và urllib3 transport. Chỉ scoped cutover ở fixture nghiệp vụ; pending HTTP riêng không patch. Không đọc/sửa `.env`, tác động dev/provider/pricing/safety ready.

Fixture `test_uploads.py` cũ tạo User qua SQL trực tiếp: đổi sang bootstrap/invitation/login runtime thật, giữ assertions private/ACL/format/composite FK/race. Expectation cũ xóa object khi commit exception mâu thuẫn task mới: đổi ca đó thành giữ orphan, không retry. Các file download được thử hồi quy không chứng minh C2d-4c hoàn tất.

Full business API/cutover/C3/C4, provider/search/RAG, production durability và benchmark chưa thuộc kết quả này. Không consent journal xuyên restore, không thay nền C1/Origin. Bàn giao original download C2d-4c riêng sau review.
