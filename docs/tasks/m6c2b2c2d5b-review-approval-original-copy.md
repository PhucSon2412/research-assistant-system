# Prompt C2d-5b: phê duyệt review và sao chép original

**Đã merge PR #47 `04434e3` ngày 2026-10-08; kết quả giữ theo source trước merge; [handoff](../c2d5b-review-approval-original-copy.md) / [inventory](../c2d5b-test-inventory.json) ghi kết quả theo source.** Lúc giao: PR #46 đã MERGED `8ff5b4ed5f643a4618f70b73550cdcce465181ab` ngày 2026-10-07, chứa head `7938153`/code `1666273`. Head migration mã nguồn đã kiểm là `0040`, không suy revision DB dev. Safety **pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-5b-sau-merge-pr-46-2026-10-07) · [C2d-5a](../c2d5a-review-json-writers.md) · [Review PR #46](../reviews/pr46-c2d5a-review-json-writers.md) · [Caller inventory](../c2d1-caller-inventory.json)

Bạn là coding agent chính. Hoàn thành một PR **C2d-5b**, từ khảo sát, triển khai, kiểm thử và docs đến commit/push/mở PR **tiếng Việt** để review; không merge. Chuyển đúng **`POST /admin/review-requests/{request_id}/approve`** qua runtime thật. Admin kiểm quyền của đúng version/file rồi tạo bản common **mới, pending** với original riêng và `AccessRights` riêng; không công bố, tạo job/index hoặc gọi provider.

Publish/revoke/common-index, cleanup/reconcile vận hành, search/RAG/provider, C3/operator/delivery và C4/cutover là task riêng. Không coi C2d hoàn chỉnh khi chỉ endpoint này đã chuyển.

## 1. Base, docs và môi trường

- Fetch, xác minh PR #46 MERGED và main chứa các SHA trên; tạo `feat/m6c2b2c2d5b-review-approval-original-copy` từ main đã xác minh. Nếu main tiến, ghi base/head thực tế và đối chiếu diff liên quan.
- Giữ **mọi docs chưa commit**: report PR #46, trạng thái merge và prompt này. Không reset/clean làm mất file; nếu worktree, chuyển đủ docs và đối chiếu. Merge tree `8ff5b4e` giống tree head PR #46 tại lượt giao, chưa có thay đổi code phụ thuộc khác.
- Đọc [AGENTS](../../AGENTS.md), [quyền](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2d-2b read](../c2d2b-review-publication-json-reads.md), [C2d-3 write](../c2d3-library-collection-json-writes.md), [final clock](../c2d-final-read-clock.md), [PUT/outcome P2](../c2d4b-upload-original.md), [download/cancellation](../c2d4c-original-downloads.md), [C2d-5a](../c2d5a-review-json-writers.md), [testing](../testing.md). Không xin lại nền C1/Origin đã chốt theo ủy quyền.
- Python/cài thư viện chỉ `C:\Users\ADMIN\.conda\envs\ras`, kiểm interpreter/pip, không venv. PostgreSQL/MinIO/journal/credentials thử phải cô lập, explicit `RAS_TEST_DATABASE_ADMIN_URL` và storage trước import fixtures, không fallback `.env`.
- Không đọc/sửa `.env` hoặc key, gọi provider, đổi `pricing_verified`, query/migrate/restart dev hay mở safety ready. Không API fallback sang migrator/recovery; scoped cutover chỉ fixture nghiệp vụ, HTTP pending riêng không patch.

## 2. Khảo sát và các lỗi hợp đồng cần chuyển

Đọc `promotion.py`, route approve, `review_writes.py`, `review_publication_reads.py`, `original_downloads.py`, `upload_original.py`, storage/config/proxy, SQL approval/closure, `db_roles.py` và fixtures trước sửa. Ghi mâu thuẫn và hành vi quan sát trong decisions; các điểm đã thấy:

1. Approve dùng principal và `now` dependency; copy xong không kiểm latest caller/role/CSRF/TTL đầy đủ. `_refuse` tự ghi audit/commit bằng principal cũ.
2. `recovery.record_review_closure` ghi protected mirror trực tiếp; runtime không có quyền. Routine `0040` chỉ withdraw/reject, không nhận `approved`.
3. Commit lỗi có thể replay ngay bằng bind rồi dùng `_cleanup_after_failed_commit`/SELECT để suy không commit và xóa key; không còn phù hợp hợp đồng outcome độc lập của C2d-4b/C2d-5a.
4. Copy ba lượt storage chưa có attempt/slot/cancellation ownership; không được giữ Session/connection/row lock trong I/O hay đóng spool dưới thread.
5. Route trả ORM/DTO cũ sau commit; JSON proxy hiện có timeout thường 15 giây cho approve, ngắn hơn copy có thể mất.

Không chuyển các writer khác theo tiện tay. `promotion.verified_read` đang được original downloads dùng: giữ hợp đồng/export hoặc refactor có hồi quy đầy đủ, không làm hỏng C2d-4c.

## 3. Hành vi sản phẩm giữ nguyên

- Admin có role hiện hành, session usable/latest confirmed head/receipt/generation, Origin và CSRF thật. Trước **mỗi lần approve**, form nguồn/quyền của Admin trống, xác nhận không tick sẵn, không chép `declared_*`. `confirm_verified` phải là boolean `true` tường minh; false/null/1/chuỗi không được nhận thay.
- Đúng request open, exact review grant và item/version/file/checksum/epoch. User upload/đọc miễn phí không chứng minh quyền phân phối. Admin tự nhập nguồn, URL giấy phép hoặc bằng chứng, ngày kiểm và scope có cấu trúc; giữ audience/flags/conditions hiện hành. Không tự bỏ conditions hoặc mở nội dung từ rights.
- Kiểm evidence bắt buộc, scope consistency và ngày kiểm với clock mới theo quy tắc UTC + một ngày hiện có. Lỗi input/denial muộn không dùng profile/time cũ để bỏ final caller.
- Copy sang key mới do attempt tạo, không dùng key private hoặc dedup liên owner/scope. Common item/version/file/rights và approved closure/audit commit cùng transaction; private item/file/version/collection không đổi.
- Bản mới `pending`, `metadata_only`, không file/chunk/search/RAG ở route thường kể cả Admin. Request approved kết thúc grant review ngay từ commit; URL review cũ 404. Response Admin là **thin history**, có ID bản common khi proof mới cho phép, không payload private/declaration/email/file URL hoặc rights evidence.

## 4. Proof, copy và final transaction

Chọn cho task: một attempt owns scalar target, key, spool, slot, executor và outcome; không share dependency Session vào thread có thể sống lâu hơn request. Dùng helper riêng hoặc tái dùng hợp đồng đã kiểm, không writer shared rồi upgrade.

| Pha | Khóa và yêu cầu |
| --- | --- |
| Proof trước copy | Request SHARE → private item SHARE qua exact grant → journal shared → current Admin/CSRF/gate/target scalar và clock sau SQL cuối. Missing/malformed/closed/inactive/checksum mismatch cũng có current caller denial phù hợp. Trả connection trước I/O |
| Copy ngoài DB | Đọc private original theo chunk vào temp, giới hạn actual size và SHA-256; put key riêng; readback bản sao kiểm size/SHA-256. Không ORM/proof/Session/locks sống qua storage. Không whole-file RAM, URL object/client token tới storage |
| Proof giữa pha nếu cần | Transaction ngắn kiểm gate/actor/grant trước pha storage tiếp theo; nhả toàn bộ. Authority không lấy từ proof đầu để commit |
| Acceptance | Admin User NO KEY UPDATE → request UPDATE → private item SHARE qua grant → tạo common/version/file/rights + approved closure/audit → flush mọi DML/FK → journal exclusive trực tiếp → scalar caller/resource/clock → mirror/append/confirm → scalar caller/resource/clock → commit |
| Response sau known commit | Transaction mới: request SHARE → common item SHARE chỉ qua quyền common admin/pending phù hợp → journal shared → current Admin/CSRF/exact link/DTO/clock sau SQL. Không lấy lại private item/grant đã đóng, không User lock sau journal |

Initial/final target frozen chứa IDs/key/size/MIME/checksum/request checksum/epoch cần thiết. Kiểm lại request/file/epoch đã ghim sau copy, không lấy target mới rồi dùng bytes cũ. Không gọi storage để hash/delete hoặc đợi thread/provider khi còn DB connection/locks, kể cả denial/cleanup.

Response phản ánh trạng thái mới: bản có thể đã published/revoked bởi writer khác sau commit. Giữ quyền Admin thin workflow; chỉ trả common ID qua gate hiện hành, hoặc trả scope-changed controlled nếu tuple đã mất quyền. Không dùng formatter read-only trên ORM cũ hoặc ép pending vĩnh viễn để nhận success.

**Giá trị khởi đầu scoped theo ủy quyền, cần đo:** pool copy riêng mỗi process, default **2**; deadline quan sát pha copy **90 giây**; exact POST approve qua proxy **120 giây** (`WEB_PROXY_REVIEW_APPROVAL_TIMEOUT_SECONDS`, kiểm 1–300 và không nhỏ hơn timeout thường). Backend chọn tên config rõ, có test; không hardcode rải rác, không thành SLO/benchmark hoặc luật kiến trúc vĩnh viễn. Route khác giữ timeout hiện hành. SDK connect/read/retry dùng config có sẵn. Deadline elapsed dùng monotonic/async timeout; auth dùng callable UTC clock mới, không trộn hai đồng hồ.

Deadline/cancel không giết thread hoặc chứng minh handler đã dừng. Attempt shield executor và theo dõi stop, giữ slot/temp/key/outcome tới khi **mọi thread settled**; cleanup/close không chạy dưới thread. Nếu stop đã biết trước acceptance/commit thì không bắt đầu tiếp business write/commit; durable intent có thể vẫn khiến recovery đóng grant. Cancel/timeout trong hoặc sau commit giữ object và không retry. Thread treo có thể giữ slot, ghi giới hạn thật; không hứa wall-clock bound tuyệt đối/abort multipart an toàn. Registry/reaper quiescent follow-up riêng.

## 5. Approved closure bằng runtime hẹp

**Quyết định scoped:** thêm migration mới sau head thực tế (dự kiến `0041`), không sửa `0040` hoặc revision đã merge. Ưu tiên hai routine **approval mirror/confirm riêng**, không mở domain `0040` withdraw/reject thành generic bypass. Nếu cách khác đơn giản hơn nhưng cùng độ hẹp, ghi rationale và kiểm đủ regression; không cấp raw journal DML.

- Mirror chỉ nhận `review_closure` với `status=approved`, `reason_code=approved_by_admin`. Kiểm Python + PostgreSQL độc lập: exact envelope/body/type/NULL/UUID/time và request thực vừa chuyển open → approved trong transaction (`closure_transaction_id`). Derive typed request ID từ line, mirror false.
- Bind approval tới **đúng common item/version/file/checksum và `AccessRights` của request**, actor Admin khớp reviewer/closed_by, pending mới/owner NULL/key riêng/epoch, không dùng rights của request khác hoặc common cũ làm chứng cứ. Nếu thêm operation/transaction binding cho tuple common, phải do DB tạo/kiểm; không tin GUC/boolean input hoặc một UUID bất kỳ. Force mọi deferred binding/approval/rights/FK/audit constraints **trước byte đầu journal**.
- Pending gate chỉ trừ đúng một approved closure stage trong chính transaction, exact private tuple/actor/time và common/right coupling; giữ closure/mirror khác, hold, file ahead, cutover pending đóng. Không nới normal read gate hoặc cho pending context đọc private ngoài grant.
- Append/fsync trước commit; confirm false → true chỉ cùng line/request/transaction sau thành công. SECURITY DEFINER NOLOGIN, fixed search_path và ACL hẹp như C1; provisioning mới **và upgrade roles đã provision** kiểm runtime EXECUTE, không PUBLIC/recovery/owner/raw DML/DDL/TEMP/fallback.
- Fail-closed khi append/fsync lỗi: chỉ known-commit approval + common/rights + mirror false nếu actor/resource/clock vẫn hợp lệ ngay trước commit; trả 503 journal unavailable, không success DTO và gate đóng. Nhánh này không chạy normal gate để che chính false mirror, nhưng vẫn phải scalar recheck actor/binding/TTL sau I/O. Nếu actor hết hạn hoặc binding sai, rollback business, không positive approval; durable/torn intent còn lại xử lý qua recovery và không hứa grant vẫn open. Lỗi actor/confirm/binding/commit không giả thành journal-write-failed.
- Recovery vẫn chỉ đóng grant `closed_by_recovery` khi snapshot thiếu approval; không dựng common/object/rights/User/receipt hoặc suy approval/audit thành công. Hold-before-migrate không đọc cột mới; giữ replay/backfill/present/absent/idempotence và journal session/reset/invitation/auth.

## 6. Outcome, object cleanup và response lỗi

Outcome do operation sở hữu, độc lập rollback/close/invalidate/cancellation: key/copy started/settled, record/append outcome, commit_started/committed và rollback_confirmed. Mã cụ thể agent chọn theo UI hiện có, ghi bảng; semantics bắt buộc:

| Cửa sổ | Hành vi |
| --- | --- |
| Copy/checksum/permission lỗi trước acceptance | Không common/rights; không success DTO. Chỉ dọn **key riêng** khi put/read thread settled và không có acceptance/commit có thể đang chạy |
| Known precommit rollback | Dọn key riêng chỉ khi rollback xác nhận, chưa commit_started và executor settled. SELECT reference nếu dùng chỉ tăng điều kiện giữ, không là authority xóa outcome unknown. Storage/DB cleanup lỗi giữ orphan unreachable cho reconcile |
| Durable intent rồi actor/confirm/DB/cancel fail trước commit | Không DTO, không nói request vẫn open/chưa lưu. Recovery có thể đóng grant; common không được dựng lại. Cleanup cần điều kiện known precommit ở dòng trên |
| Append/fsync lỗi và known approval commit | Giữ common/object/rights, mirror false, gate đóng; 503 journal unavailable; URL review mất grant |
| Commit exception, dù remote landed hoặc rollback | Controlled uncertain, không DTO/IDs/replay-success/retry; **giữ object** dù SELECT chưa thấy SourceFile |
| Known commit + teardown/proof/actor/resource lỗi | Giữ rows/object; auth/resource/response error, không DTO cũ hoặc hứa rollback |
| Known commit + proof mới đạt | 200 thin response, ID chỉ theo gate; không tự publish/index |

Không gọi privileged `recovery.replay_record` từ HTTP runtime, không dùng SELECT/replay/error string/Session để suy commit. CLI recover chỉ hạn chế quyền. `_refuse`/audit denial cũ phải chuyển boundary phù hợp hoặc bỏ best-effort audit không bảo vệ: không tạo transaction commit bằng actor cũ, khóa hàng sau journal hay storage delete dưới DB. Audit chỉ IDs/reason/count; không nguồn, evidence, title, filename, URL, cookie/token hoặc key.

Proxy timeout/502/đứt body/cancel không chứng minh approve chưa lưu; UI nhắc kiểm trạng thái mới, không tự POST lại hay hứa chưa có gì được ghi. Giữ nguyên pass-through status/Set-Cookie/no-store, exact segment/allowlist/Origin forwarding và routes streaming original; không thêm generic admin timeout nới cho mọi route.

## 7. Kiểm chứng và hoàn thành

Auth/accounts/session/head/receipt qua bootstrap/invitation/login/runtime thật; resource setup/corruption đặc quyền riêng. **Approve POST và ba C2d-5a POST đều runtime**; gỡ approve khỏi `original_fixtures.py`/fixture tương tự, chỉ giữ publish/revoke legacy đặc quyền và ghi giới hạn. Không bypass principal/predicate/receipt/grants/quarantine/held hoặc đổi readiness để qua test.

| Nhóm | Ca bắt buộc |
| --- | --- |
| Contract/ACL | A/B/Admin, cùng bytes khác owner; xác nhận boolean strict/form trống không autofill; exact request/version/file/checksum/epoch; evidence/date/scope; request closed/grant inactive; pending không ở library/file/search/RAG/collection; private/collection không đổi; thin response/no private payload |
| Caller/clock | Role/session/status/quarantine/head/receipt/generation/binding/CSRF đổi sau dependency, trong I/O, lock wait, predicate SQL, fsync, commit, response. Idle/absolute đúng biên/sát biên; clock sau SQL cuối, không touch idle helper |
| Storage/attempt | Object thiếu/sai bytes cùng size; source/copy read/put/readback/spool/close lỗi; 30 MiB synthetic stream không RAM buffer; slot exhaustion, thread blocked, deadline, client disconnect/task cancel/cancel-all, cuối trả slot/temp/connection. Private original không xóa; uncertain/known commit không xóa copy |
| SQL/routines | Runtime success/raw/PUBLIC/recovery/DDL/TEMP/GUC denials; NULL/missing/extra/type/UUID/time; wrong requester/actor/request/private tuple/common/version/file/checksum/rights/transaction/epoch; chỉ force valid approved pair trước append; confirm-only và normal pending/gate khác vẫn đóng |
| Journal/outcome | None/torn/full-fsync/prefix-compatible intent; lỗi sau append/confirm, commit rollback/landed uncertain, teardown rollback/close/invalidate; không IDs/success/retry, flags không bị xóa; gate và grant/rows/object/audit đúng |
| Race | Hai approve cùng request, approve đua withdraw/reject/epoch bump/auth writer, GET history/original, response đua publish/revoke. Barrier tất định cả hai thứ tự, quan sát lock waits/no deadlock; I/O bị chặn không chặn closure/auth commit và không giữ DB locks |
| Restore | Snapshot open/absent → runtime approve → copy journal → hold/migrate/recover; approval absent chỉ closed_by_recovery, không common/rights/receipt dựng lại; snapshot có approval giữ coupling, replay idempotent/present; old review URL 404, private intact |
| Migration/regression | Upgrade populated/provisioned `0040` → head mới, reprovision, ORM/schema, downgrade populated chặn theo nền, hold trước migrate. C2d-5a mirror/pending/fault/race và GET/original/clock/auth-journal bị ảnh hưởng |
| Proxy/browser | Backend runtime socket → Next proxy → Chrome form thật nếu có. Approve hợp lệ/denial/uncertain/503, consent ban đầu không tick, thin response và URL cũ bị chặn; exact approve có timeout riêng >15s, route khác không đổi; no auto retry/secret log |
| Pending thật | Không patch: health sống/ready503/approve503, không copy/DML/journal change |

Chọn đủ test mới và regression theo nguy cơ; không sửa app writer ngoài scope để làm fixture xanh. Test nào legacy expectations không còn đúng phải thay bằng regression runtime cùng invariant và ghi rationale, không âm thầm bỏ/count pass. Đối chứng riêng trước suite cuối: bỏ caller/role/clock, exact binding/common coupling, hash copy, pending gate, proof mới, outcome retention hoặc ownership executor phải fail assertion có ý nghĩa; khôi phục byte/hash, không sửa source khi suite đang chạy.

Inventory collect/selection/JUnit unique/missing/duplicate/not-run, source SHA và hashes trước/sau. Ghi từng preparation/fail/error/skip/interrupted/mutant, phân biệt setup failure và assertion; không cộng lịch sử PR #46. PostgreSQL/MinIO thật, HTTP ngoài chặn, 0 provider; nếu không có browser/không benchmark ghi đúng not-run. Timings fixture không thành SLO; thử concurrency/PDF phải nêu workload/môi trường, nhãn mục tiêu đề xuất/cần đo.

Tạo handoff `docs/c2d5b-review-approval-original-copy.md` và inventory JSON; đồng bộ README/docs README/decisions/data-model/access/testing/roadmap/dev-setup/AGENTS/caller map. PR #46 merged `8ff5b4e`; số review 234/58 và agent 1.371 giữ theo source cũ, chưa rerun tại merge. Thêm lệnh đã chạy thật, contract cho publication task sau, key/orphan/cancel/journal/restore giới hạn. Không biến lựa chọn scoped thành luật kiến trúc vĩnh viễn hoặc quyết định trực tiếp mới của chủ dự án.

Artifact/journal/paper/credentials ngoài repo; dọn đúng resources thử bằng exact ID/name/label/image/port/mount/sole consumer, đối chiếu tài nguyên có sẵn, không dev. `fsync`/fault injection/snapshot cùng máy không chứng minh mất điện, production storage độc lập hoặc full cutover. Commit giữ tác giả/committer hiện có, không AI co-author/trailer. Đã giao commit/push/mở PR tiếng Việt; không xin lại quyền, không merge.

**Hoàn thành:** một approve chạy runtime thật; original được copy/verify ngoài DB có ownership hữu hạn, actor/grant/clock kiểm lại trước mutation; approved/common/rights/journal coupling đúng, uncertain không success/xóa copy/retry; known commit có proof mới/thin response, grant không sống lại sau restore có journal hiện hành. Các writer/provider/C3/C4 riêng và safety pending.
