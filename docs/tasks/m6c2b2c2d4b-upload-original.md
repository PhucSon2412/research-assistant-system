# Prompt C2d-4b: nhận original và tiêu thụ upload intent

**Trạng thái:** đã triển khai/kiểm trên nhánh review, chưa merge; [handoff](../c2d4b-upload-original.md) giữ kết quả theo source riêng. Nội dung dưới đây giữ hợp đồng giao task. PR #43 đã merge ngày 2026-10-06 tại `632ec683940a8bccc711c8a55743b0a3992c1d03`, chứa head `f60645e`. Head migration mã nguồn `0039`; không suy revision DB dev. Safety **pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-4b-sau-merge-pr-43-2026-10-06) · [Roadmap](../roadmap.md) · [Bàn giao C2d-4a](../c2d4a-upload-notice-intent.md) · [Review PR #43](../reviews/pr43-c2d4a-upload-intents.md)

Bạn là coding agent chính. Hoàn thành một PR **C2d-4b** cho `PUT /uploads/original`: code, kiểm thử, docs, commit/push và mở PR **tiếng Việt** để review; không merge. Giữ luồng JSON xác nhận → intent → truyền file đã có. Original download dành cho C2d-4c; writer/provider khác và C3/C4 tiếp tục riêng.

## 1. Base, khảo sát và môi trường

- Fetch lại, xác minh PR #43 MERGED và main chứa head trên; tạo `feat/m6c2b2c2d4b-upload-original` từ main đã xác minh. Nếu main tiến thêm, đối chiếu thay đổi liên quan và báo base/head mới.
- Giữ mọi docs chưa commit, gồm report PR #43 và prompt này; đọc diff và đưa cập nhật hợp lệ vào PR. Không reset/clean làm mất tài liệu. Nếu dùng worktree, chuyển đủ docs và đối chiếu.
- Đọc [AGENTS.md](../../AGENTS.md), [quyền và dữ liệu](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2d-3](../c2d3-library-collection-json-writes.md), [final read clock](../c2d-final-read-clock.md), handoff C2d-4a và [testing](../testing.md).
- Đối chiếu `api/app/api/routes/uploads.py`, `api/upload_intents.py`, `api/resource_writes.py`, `identity/final_caller.py`, `auth_safety.py`, `models/upload.py`, `models/ingest.py`, `storage.py`, `cli/reconcile_originals.py`, worker và composite FK. Đọc `api/tests/test_uploads.py`, test runtime/intent và `web/app/api/uploads/original/route.ts`, form upload. Báo mâu thuẫn và lock map trước sửa.
- Chỉ PostgreSQL/MinIO/journal/project cô lập, runtime role thật; cấp **tường minh mọi credentials thử trước import fixture**, kể cả MinIO, để không fallback đọc `.env`. Dùng Conda `C:\Users\ADMIN\.conda\envs\ras`, xác minh interpreter/pip; không tạo venv. Không đọc/sửa `.env`, query/migrate/restart dev, gọi provider hoặc đổi `pricing_verified`/safety readiness.
- Không dự kiến migration, grants mới hoặc bảng claim/lease. Nếu chứng minh schema hiện tại không đủ, báo lý do trước mở rộng; revision mới sau head thực tế, không sửa migration đã merge. Task không cần xin lại các quyết định nền C1/Origin đã chốt.

## 2. Lựa chọn thực hiện

**Orchestrator chọn theo ủy quyền:** intent chỉ được tiêu thụ nguyên tử trong giao dịch lưu item/version/file/job cuối. Mỗi attempt staging riêng, không giữ khóa intent/User/journal hoặc transaction DB trong lúc nhận bytes, parser, disk hay S3. Hai request cùng intent có thể cùng staging; chỉ một request được commit. Không tạo early claim cần recovery riêng.

Tài nguyên phải hữu hạn: giới hạn bytes/parser hiện hành, slot upload riêng và deadline nhận body. Giá trị khởi đầu **đề xuất, cần đo**: tối đa 4 attempt staging đồng thời **mỗi process API**, nhận body tối đa 90 giây; đưa vào config có test, không hardcode ở nhiều nơi. Không dùng pool download cho upload. Đây không phải rate limit toàn hệ thống hoặc SLO. Đối chiếu timeout SDK/proxy và ghi giới hạn thực tế; không hứa tổng request luôn dưới proxy 120 giây hoặc proxy timeout đã hủy handler API.

Giữ upload chỉ dành cho **current role User**; Admin không có nhánh private bypass. Intent/acknowledgement gắn đúng User, session, notice và metadata của **một lần upload**. Notice cũ không cho xử lý tiếp. Intent được sử dụng trước expiry; sau known commit, expiry của intent không đảo ngược việc nhận file đã thành công.

## 3. Các phase và transaction

### A. Kiểm trước nhận file

- Kiểm session/gate/CSRF/Origin bằng helper thật. Dependency principal chỉ định danh; current actor phải được chứng minh lại. Giữ denial/error priority hiện hành, intent thiếu/sai/foreign/used trả lỗi không tiết lộ người khác; không log token/hash/cookie/filename/text.
- Short read proof: intent SHARE → acknowledgement SHARE → journal shared → scalar binding/current actor/latest head/receipt/generation/current role/CSRF, clock **sau SQL cuối** cho session và intent. Không lấy User lock sau journal. Lấy immutable scalar binding, rồi kết thúc transaction và trả connection trước đọc body.
- Không lưu object/tạo job trước proof này. UI chỉ gửi file sau intent thành công; không hứa chặn được bytes đã truyền trên mạng từ client bất tuân.

### B. Nhận, kiểm file và lưu staging

- Stream vào file tạm, kiểm size trong lúc đọc dù thiếu/sai `Content-Length`, SHA-256 khớp khai báo. Chỉ PDF/Markdown; xác minh định dạng thật, không chỉ extension/Content-Type. Giữ giới hạn config PDF 30 MiB/100 trang, Markdown 2 MiB và parser timeout/memory. Không OCR, không mở thêm loại file.
- PDF upload probe không chứng minh body coverage: worker vẫn quyết định extraction/`needs_review`/index. Không ghi `indexed` hoặc tự chạy embedding trong request upload.
- Parser/storage blocking không chạy trên event loop. Trong mọi I/O không giữ transaction, connection hay khóa DB; file tạm/slot có owner và được đóng trên mọi đường kết thúc.
- Proof mới sau parser và ngay trước `storage.put`; nhả toàn bộ khóa trước put. Actor/session/role/generation/TTL/notice thay đổi trước bước này thì không put, không item/job.
- Key object **ngẫu nhiên riêng mỗi attempt**, không dùng key chung theo intent/checksum/User. Theo dõi cả put đã bắt đầu nhưng ném lỗi vì object có thể đã được ghi một phần.
- Sau put, đọc lại object theo stream và so **size + SHA-256** ngoài khóa trước giao dịch cuối, cho cả PDF/Markdown. HEAD/ETag chỉ từ chối sớm, không thay phép băm. Object bị đổi cùng kích thước phải bị bắt. Không hứa bất biến bytes tuyệt đối nếu object bị thay sau lần băm cuối.

### C. Giao dịch nhận file cuối

1. Writer entry gate thật; khóa **User NO KEY UPDATE → intent FOR UPDATE → acknowledgement SHARE**, trước journal. Đọc lại binding và current actor; intent phải chưa dùng/còn hạn, notice và metadata không đổi. Request thua race không tạo bản thứ hai.
2. Tạo item **private đúng owner**, version, job `origin=upload`, source file đúng item/version/job/checksum/size/kind/pages; giữ composite FK. Private upload vẫn nối file qua `SourceFile.job_id` như worker hiện hành; không áp cách nối promotion một cách máy móc. Job key/label/error chỉ IDs/mã; idempotency key dựa intent UUID.
3. Tiêu thụ `used_at`/`item_id`, ghi business audit và flush **toàn bộ DML/FK trước journal exclusive**. Mốc server mới, không dùng `now` lúc bắt đầu request; không gia hạn intent. Audit không chứa tên file/text/checksum/token.
4. `require_final(writer=True)` trực tiếp, không shared upgrade. Kiểm scalar consumed binding đúng **item vừa tạo**, exact session/User/role/CSRF/latest heads/receipts/generation/gate và clock sau mọi SQL cuối; intent phải còn hạn tại boundary consume. Không nới helper C2d-4a thành bypass để nhận intent bất kỳ đã dùng. Không row lock/FK write mới sau journal.
5. Giữ khóa đến commit. Không storage/provider/client I/O dưới khóa. Known rollback trước commit xóa đủ business rows của attempt; acknowledgement/intent đã tạo ở C2d-4a vẫn riêng, không bị kéo dài lifetime.

### D. Response sau known commit

- Dùng **transaction/proof mới**, kiểm caller/current role/CSRF/gate và exact consumed intent/ack/item/version/file/job bằng SQL private owner. Không dùng ORM/proof trước commit; không trả IDs/private DTO khi hậu kiểm từ chối.
- Viết lock map response trước triển khai: nếu khóa item/job thì **item trước job** như worker; các resource lock khác trước journal shared, không User lock sau journal. Clock session lấy sau SQL cuối. Intent đã consume hợp lệ nhưng hết hạn trong lúc chờ response không làm mất item; hậu kiểm cần consumed binding, không đòi intent lại unused/còn hạn để dùng lần nữa.
- Worker có thể đổi job trước response: không đòi job vẫn `received`, không ghi đè state hoặc `metadata_only` đã tiến triển. Giữ response IDs và `state=received` như biên nhận việc upload được tiếp nhận; trạng thái xử lý hiện tại đọc qua API job. Logout sau commit không tự hủy job đã nhận hợp lệ.
- Known commit rồi hậu kiểm lỗi: object/rows có thể đã lưu; không xóa object có tham chiếu hoặc hứa mutation chưa xảy ra. Response đã qua boundary có thể tiếp tục gửi sau khi nhả khóa; bytes đã gửi không lấy lại được.

## 4. Commit uncertain, disconnect và object mồ côi

- **Mọi exception từ commit là uncertain**, kể cả commit landed nhưng mất reply: lỗi có kiểm soát, không IDs, không retry/replay mutation. **Giữ object** để đối soát; một SELECT chưa thấy tham chiếu không chứng minh commit từ xa đã kết thúc. Không gọi cleanup kiểu `_delete_if_unreferenced` trong nhánh uncertain.
- Trước commit/known rollback, best effort chỉ xóa key riêng của attempt khi đã xác nhận không có tham chiếu; DB không trả lời thì giữ orphan. Guard từ chối sau put cũng đi qua cleanup này, không chỉ rollback DB rồi bỏ object.
- Client abort/`CancelledError` không chứng minh thread chạy put/commit đã dừng. Trước khi bắt đầu giao dịch cuối, cancellation đã biết thì không nhận file. Nếu I/O/commit đã chạy trong thread, đợi kết quả hữu hạn hoặc coi outcome uncertain và giữ object; không delete/đóng file còn bị thread sử dụng. Sau settled known commit vẫn bảo vệ object có tham chiếu.
- Slot/temp luôn được trả khi request kết thúc, kể cả tạo tempfile lỗi, parser lỗi, disconnect hoặc cancellation. Resource còn do thread sử dụng phải có ownership cho tới khi thread kết thúc, không thả slot sớm làm vượt concurrency thực tế.
- Giữ `cli.reconcile_originals`: chỉ dọn sau grace config và khi không còn attempt/I/O/commit cũ đang chạy; luôn giữ key có `SourceFile`. Không thêm tự động quét bucket đang nhận upload hoặc scheduler production. Ghi giới hạn partial/multipart SDK nếu chưa có cơ chế dọn upload chưa hoàn tất.
- UI phải phân biệt validation/auth denial với upload outcome chưa xác định/response không khả dụng; không tự PUT lại token cũ hay tự tạo intent khác để retry mutation. Cho người dùng kiểm thư viện khi có thể đã nhận file. Proxy 502/504 không chứng minh backend chưa lưu.

## 5. Kiểm thử và chứng cứ

Tài khoản/intent nghiệp vụ qua bootstrap/invitation/login/runtime thật, latest head/receipt/predicate thật; scoped cutover chỉ trong fixture nghiệp vụ. HTTP pending riêng **không patch** phải trả 503; health/readiness giữ hợp đồng. Không dùng principal giả, nới grants hoặc tạo receipt bằng bypass production để test qua.

| Nhóm | Tiêu chí cần kiểm |
| --- | --- |
| Contract/ACL | A upload PDF/Markdown private; B/Admin không dùng intent của A; wrong session/used/expired/notice/CSRF/Origin bị chặn. Intent thành công trước PUT; đúng item/version/file/job và một lần consume |
| Format/resources | Size/hash/format mismatch, thiếu Content-Length, vượt limits/pages, parser timeout; không object/job sai. Slot/temp cleanup và deadline cấu hình; không đọc toàn PDF vào RAM |
| Fresh actor/clock | Thay đổi sau dependency, trong receive/parser/put/readback, final lock wait và SQL cuối: session revoke, disable, credential/status head/receipt/generation/quarantine/held, current role, idle/absolute và intent expiry. Trước commit rollback; sau known commit không DTO nhưng giữ dữ liệu hợp lệ |
| Không khóa khi I/O | Chặn body/parser/S3/readback bằng event; auth revoke/disable vẫn commit được trong lúc I/O bị chặn. SQL/transaction trace chứng minh trả connection; dùng lock wait có kiểm soát, không chỉ sleep |
| Concurrency | Hai request cùng intent đều staging vẫn chỉ một item/version/source/job; loser chỉ dọn key của mình. Intent khác/owner khác cùng bytes độc lập private. Worker tiến state trước response không bị ghi đè/từ chối sai |
| Integrity/fault | MinIO trả bytes sai cùng size bị SHA bắt; partial put, DB precommit lỗi, landed/lost reply, commit còn chạy trong thread, DB unavailable khi cleanup. Unknown outcome giữ object; không delete referenced object |
| Disconnect | Ngắt lúc stream, parser/storage/readback và final commit; không slot/descriptor leak, không use-after-close hay delete trước thread commit. Reconcile sau outcome settled/grace giữ referenced key, dọn orphan cũ |
| Hồi quy | C2d-4a notice/intent, C2d-3/clock, pending/runtime và upload FK/ORM liên quan. Giữ expectation nghiệp vụ; đổi fixture theo runtime thật, không nới assertions để che lỗi |

- PostgreSQL/MinIO thật trong ca chính; injection ghi rõ mô phỏng, không coi là mất điện/crash thật. Chặn HTTP provider, chỉ cho local test storage/proxy; log/audit không key/cookie/token/text/filename.
- Đối chứng một số guard quan trọng, chạy riêng rồi khôi phục bytes/hash **trước** suite cuối. Không sửa code trong lúc suite chạy. Chọn suite theo rủi ro/helper đổi; collect toàn inventory, đối chiếu selection/JUnit unique node và source SHA/hash trước/sau; pass/fail/error/skip/not-run riêng. Không bắt buộc chạy lại mọi node lịch sử không liên quan.
- Kiểm HTTP qua **proxy streaming riêng**, không chỉ port 8000; smoke form upload với tài khoản tổng hợp khi đủ công cụ, ghi rõ browser thật hay HTTP/callback. Nếu đổi web/proxy/error mapping, chạy test/typecheck/build liên quan. Original download cũ có thể dùng để kiểm hồi quy nhưng không coi C2d-4c đã hoàn tất.
- Tài nguyên thử có label/port/volume riêng; kiểm resolved paths/ID/mount/consumer trước cleanup, giữ container/volume dev và tài nguyên có sẵn nguyên trạng.

## 6. Hoàn thành và bàn giao

- Thêm `docs/c2d4b-upload-original.md` và inventory theo source thực tế; cập nhật caller inventory, decisions, testing, roadmap, dev-setup/mục lục. Ghi phase/lock map, cleanup theo outcome, clock consume/response, race/cancellation, config và giới hạn proxy.
- Hoàn thành khi PUT qua runtime thật và kiểm contract, race, clock, I/O không khóa, integrity/uncertain/cancel cùng hồi quy chọn đạt; no private leak/provider call. Không ghi lệnh chưa chạy thành đã kiểm; Markdown links và `git diff --check` sạch.
- Không bổ sung consent journal, original download boundary, provider/search/RAG, operator hoặc cutover. Safety **pending tới C4**; không khiến dev healthy bằng bypass.
- Commit không AI author/committer/trailer; push nhánh và mở PR tiếng Việt cho người review, gồm base/head/code SHA, file, kết quả/node/not-run, lỗi các lượt trước, đối chứng và giới hạn. **Không merge.** Báo link PR và bàn giao C2d-4c; không xin lại quyền commit/push/mở PR đã giao ở đây.
