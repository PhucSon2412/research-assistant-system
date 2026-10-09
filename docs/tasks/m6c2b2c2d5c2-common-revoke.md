# Prompt C2d-5c2: thu hồi bản chung qua runtime và journal

PR #48 đã **MERGED** tại `1eede17c1eed82fa7a00f7018fe01b4362ce30a9` ngày 2026-10-09, chứa code `520ac54` và head `654fbac`; `origin/main` tại merge này, tree giống head PR. Head migration mã nguồn **0041** kế thừa 0040, kiểm bằng AST; không query revision DB dev. Đây là **task đã giao, chưa triển khai/chưa kiểm thử**. Các quyết định scoped dưới đây do orchestrator chọn theo ủy quyền; **safety pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-5c2-sau-merge-pr-48-2026-10-09) · [Handoff publish](../c2d5c1-common-publish.md) · [Review PR #48](../reviews/pr48-c2d5c1-common-publish.md) · [Caller map](../c2d1-caller-inventory.json)

Bạn là coding agent chính. Hoàn thành một PR **C2d-5c2**: khảo sát, triển khai, kiểm thử, docs, commit/push và mở PR **tiếng Việt** để review; **không merge**. Chuyển đúng **`POST /admin/common-copies/{item_id}/revoke`** sang runtime thật, giữ thu hồi/tombstone/epoch/cancel/retention và journal nhất quán, outcome độc lập teardown và response proof mới.

Common-index, chuyển worker/cleanup/reconcile sang boundary mới, search/RAG/provider, C3/operator/delivery và C4/cutover là các task sau. Cleanup/recovery hiện hành phải tiếp tục chạy đúng qua hồi quy; không gọi việc kiểm tương thích đó là đã chuyển chúng sang runtime. Không mở safety ready hoặc gọi provider trong PR này.

## 1. Base, tài liệu và môi trường

- Fetch lại, xác minh PR #48 MERGED/main chứa các SHA trên; tạo `feat/m6c2b2c2d5c2-common-revoke` từ main đã xác minh. Nếu main tiến, ghi base/head thực tế và kiểm diff phụ thuộc.
- Giữ **mọi docs chưa commit của orchestrator**: report review PR #48, cập nhật merge, caller map và prompt này. Mang đầy đủ sang nhánh/worktree mới và vào PR; không reset/clean làm mất chúng. Không sửa kết quả review cũ thành pass mới.
- Đọc AGENTS, [quyền và hợp đồng thu hồi](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [reads review/publication](../c2d2b-review-publication-json-reads.md), [resource writers](../c2d3-library-collection-json-writes.md), [clock](../c2d-final-read-clock.md), [original downloads](../c2d4c-original-downloads.md), [review writers](../c2d5a-review-json-writers.md), [approve/P2](../c2d5b-review-approval-original-copy.md), publish/review ở trên và [testing](../testing.md). Origin/C1 đã chốt theo ủy quyền, không xin lại.
- Python/cài thư viện chỉ `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip, không tạo venv. Explicit test DB/admin/runtime/recovery/storage/journal credentials **trước import fixture**, không fallback đọc `.env`.
- PostgreSQL/MinIO/journal, ports, container/volume và credentials thử phải cô lập. Không đọc/sửa `.env` hoặc key, query/migrate/restart dev, dùng fallback owner/migrator/recovery cho HTTP, gọi provider hoặc đổi `pricing_verified`. Scoped cutover chỉ fixture business; HTTP pending riêng giữ production gate thật.

## 2. Khảo sát và điểm cần chuyển

Đọc `api/routes/publications.py`, `app/publication.py`, `app/recovery.py`, `app/journal.py`, `api/common_publish.py`, `api/review_writes.py`, `api/review_approval.py`, `api/review_publication_reads.py`, `identity/final_caller.py`, `db_roles.py`, models/trigger 0022–0027, 0033 và routines 0040/0041; fixtures, publication/recovery/cleanup/common-index/search/RAG tests và form/proxy web liên quan.

Quan sát tĩnh tại base, cần đối chiếu bằng thử nghiệm:

1. Route revoke vẫn nhận dependency Session và `get_now`/principal đầu request; chưa current actor/clock trước commit hoặc proof response mới.
2. `recovery.record_revocation` qua `_journal_decision` chèn/cập nhật mirror trực tiếp, trong khi runtime không có raw write bảng `revocation_journal_records`; cần routine domain hẹp. Không cấp raw INSERT/UPDATE cho runtime để làm test xanh.
3. Legacy revoke commit lỗi rồi tự replay/SELECT và có thể trả success. Runtime không được dùng quyền recovery hoặc suy kết quả HTTP từ replay/SELECT.
4. `_refuse` audit/commit bằng principal cũ; không dùng nó làm denial boundary mới. ORM item/tombstone của transaction cũ cũng không làm response proof.
5. Form revoke chưa có busy guard và dùng thông điệp lỗi chung. Câu “không trang nào hiện bản này” còn bỏ sót quyền Admin xem thin history; sửa đúng hành vi.

Ghi mâu thuẫn/hành vi đã quan sát và lựa chọn vào decisions/handoff. Không suy mọi lỗi fixture là lỗi ứng dụng hoặc thay expectation ACL/thu hồi để né lỗi setup.

## 3. Hành vi sản phẩm và ranh giới thu hồi

- Chỉ Admin **hiện hành**, latest usable session/head/receipt/generation, Origin/CSRF đúng. `confirm_revoke` chỉ nhận boolean literal `true`; thiếu/false/null/1/chuỗi đều 422. Nếu giữ metadata, `confirm_retention_basis` cũng là boolean strict và phải true; trùng field/thiếu căn cứ/lý do không hợp lệ giữ 422 hiện hành.
- Cho thu hồi common copy `pending` hoặc `listed`, kể cả `indexed`, có `scope_conditions` hoặc không có quyền tải/search/provider. **Không áp điều kiện publish/index để chặn revoke.** Không cần đọc/hash object; file mất hoặc checksum object sai không cản thu hồi quyền.
- Private, sai/thiếu ID trả 404 như không tồn tại; User thường 403; đã revoked trả `409 common_copy_revoked` sau current caller/gate proof. Không tăng epoch, thêm tombstone/journal hoặc thay retention lần hai. Các tuple hỏng/thiếu rights phải có lỗi kiểm soát và test, không `first[0]`/500; ghi rõ giới hạn xử lý dữ liệu hỏng, không mở thêm quyền.
- Một known commit thu hồi nguyên tử: item `revoked`, epoch tăng đúng một lần, `content_status=metadata_only`, active generation NULL, rights/publication kết thúc, job chưa kết thúc được yêu cầu cancel, tombstone/retention/audit/mirror cùng transaction. Giữ các ràng buộc thời gian hiện hành, không sửa timestamp cũ để làm test xanh.
- `t_rev` là **commit DB thu hồi**, không phải lúc nhận request, tick form, ghi journal hay trả 200. Mọi request nội dung mới sau đó bị chặn dù object/index/cache còn; canceled job không được gửi tiếp/công bố sau checkpoint phát hiện thu hồi. Không xóa object/dẫn xuất trong HTTP revoke; cleanup giữ deadline cấu hình 24 giờ và không ảnh hưởng bản private/bản khác cùng bytes.
- Mặc định không giữ metadata sau cleanup. Chỉ giữ đúng field của đúng copy có căn cứ được Admin xác nhận trong lần revoke, ghi policy source/scope/reason/actor/time. Không tự giữ title/URL/license/evidence/tên file; không tự bổ sung UI nhập căn cứ trong PR này (API hiện hành vẫn hỗ trợ).
- Admin còn xem workflow/history theo gate riêng; ordinary library/file/chunk/search/RAG không thấy bản revoked. Stream đã qua boundary cuối trước `t_rev` có thể tiếp tục; bytes/text đã gửi client/provider không thu hồi được. Không hứa hủy được mọi response đã bắt đầu.
- Giữ HTTP 200 dạng `{"item": ..., "cancelled_jobs": n}` khi known commit và response proof sạch. `item` đọc mới; `cancelled_jobs` là số yêu cầu cancel được ghi trong chính attempt đã commit, không khẳng định worker đã dừng hoặc lấy số đếm mới từ retry/replay. Cleanup thắng trước response phải trả history hiện hành đã scrub, không metadata ORM cũ.

## 4. Transaction, routine và quyền DB

Ưu tiên module boundary riêng cùng pattern C2d-5a/5b/5c1; attempt sở hữu Session/executor/outcome cho tới settle. Không thêm I/O S3, pool copy, deadline file hoặc proxy timeout mới khi không có nhu cầu được chứng minh. Journal append/fsync **cố ý** nằm dưới row locks + journal exclusive theo hợp đồng hiện hành, khác storage/hash.

1. Preflight integrity/cutover và writer transaction riêng. Khóa **Admin User NO KEY UPDATE → common item UPDATE → rights theo UUID → publication và jobs theo thứ tự cố định → version/file cần thiết → retention/tombstone mới/audit**, rồi flush toàn bộ DML/FK/unique trước journal. Đối chiếu các FK/trigger để không khóa User/resource mới dưới journal. Worker khóa item trước job; cleanup khóa tombstone trước item, nên không khóa tombstone có sẵn sau item. Denial đã revoked không đi vào cleanup lock path.
2. Journal exclusive **trực tiếp**, không shared rồi upgrade. Kiểm gate đầy đủ, exact staged target/rights/epoch/publication/job cancellation/retention và current Admin/session/CSRF/clock bằng scalar sau SQL cuối, ngay trước record/append. `_db_view` hiện không cộng staged tombstone vào số review/session/token closures; không tự thêm phép trừ broad “pending” để bỏ qua lỗi chain/hold/unconfirmed khác.
3. Thêm migration mới sau head thực tế (**dự kiến 0042**), không sửa revision đã merge. Routine mirror/confirm chỉ cho **live `revocation`**: validate envelope/body shape/type/NULL/UUID/enum/time và toàn bộ binding độc lập ở Python/SQL; mirror false trước append, confirm chỉ false → true sau append/fsync thành công. Không tái dùng routine auth/review chung nhận kind tùy ý.
4. DB bind đúng tombstone của **chính transaction đang thu hồi**, common item/version/file/rights/key/epoch/reason/actor/time/purge deadline và toàn bộ retention basis. Dùng chứng cứ DB mint (`xmin` hoặc marker trigger phù hợp), không tin GUC/boolean/UUID caller tự khai. Kiểm audit/FK/deferred constraints trước byte journal đầu. Confirm chỉ đúng row/line/binding của transaction đó, không confirm record cũ/khác attempt. Nếu chọn xmin thì ghi/test ranh giới savepoint/subtransaction.
5. Giữ `SECURITY DEFINER` owner NOLOGIN/quyền tối thiểu/fixed search_path; REVOKE PUBLIC, allowlist runtime chỉ routine mới. Fresh provisioning và nâng DB đã provision phải cùng ACL. Runtime không raw journal/auth write, DDL/TEMP/BYPASSRLS/role switching; API không mở recovery/migrator connection. Không cấp quyền đọc paper private qua routine.
6. Giữ format journal `REVOCATION_FIELDS`/`RETENTION_FIELDS` và khả năng đọc record lịch sử: record có IDs/object key/codes/epoch/times/căn cứ, **không** thêm title, file bytes/text, giá trị metadata cần giữ, checksum/token/password/email/secret. Không làm parser mới từ chối record lịch sử hợp lệ chỉ vì routine live có điều kiện chặt hơn. Audit/log chỉ IDs/mã/count/tên field; không ghi text căn cứ.

## 5. Journal lỗi, outcome, cancellation và response

Outcome thuộc attempt, tối thiểu có record/append_started/written/commit_started/committed/teardown facts; rollback/close/invalidate không xóa fact. Không dùng kết quả teardown, SELECT hay replay để suy commit chắc chắn. Bảng lỗi là một phần của handoff/test:

| Cửa sổ | Hợp đồng |
| --- | --- |
| Caller/gate/resource/SQL binding lỗi trước append | Không journal/publication mutation thành công; rollback/release và lỗi auth/business/503 có kiểm soát. Missing/malformed/denial muộn cũng cần current caller/gate proof |
| Append không ghi, torn hoặc ghi đủ nhưng fsync báo lỗi | Sau authorization/binding hợp lệ ngay trước append, **commit restriction + mirror false** theo nhánh an toàn hiện hành; trả `503 revocation_journal_unavailable`, không 200. Gate đóng kể cả file và DB khớp; không normal-gate sau lỗi rồi rollback mất mirror false |
| Append/fsync thành công | Confirm row, kiểm lại gate/binding/current actor/clock sau SQL/fsync rồi mới commit; kiểm lỗi sau append có thể để journal đi trước DB, chỉ recovery mới áp intent đó |
| Confirm/hậu kiểm lỗi sau append, commit chưa bắt đầu | Rollback/release; giữ current auth denial nếu có, lỗi nội bộ trả `503 revocation_pending_recovery`. Không gọi đó là commit uncertain hoặc xóa intent đã ghi |
| Commit ném lỗi, có thể landed hoặc rollback | `503 revocation_outcome_uncertain`; giữ record/outcome, không success/DTO/IDs/replay/retry/SELECT inference. Nếu file ahead hoặc mirror unconfirmed, gate phải tiếp tục đóng tới recovery |
| Known commit + release/response lỗi | Giữ thu hồi/tombstone/journal. Trả current auth/resource denial hoặc `503 revocation_response_unavailable`, không nói “chưa ghi nhận gì” |
| Known commit, confirmed journal, response proof/release sạch | 200 DTO hiện hành và count của attempt |

**Lựa chọn scoped cho nhánh append lỗi:** giống C2d-5a, quyết định hạn chế đã được Admin/caller/clock xác nhận ngay trước append. Nhánh này chỉ hoàn tất hạn chế + false mirror và trả 503; không chạy normal gate/current-caller hậu kiểm để vô tình hủy nhánh đóng quyền. Nếu TTL trôi qua trong append lỗi, vẫn không trả success/payload; test và docs phải nêu boundary này. Nhánh append thành công vẫn kiểm current actor/clock trước commit như bình thường. Không mở ngoại lệ này cho writer cấp quyền hoặc cho lỗi SQL/binding/confirm/commit bất kỳ.

- Mirror phải flush false **trước** append; chỉ lỗi từ chính append/fsync vào nhánh false-commit. Routine/constraint/binding lỗi trước append không được xem là lỗi fsync. Confirm lỗi sau file durable là pending/uncertain recovery, không giả confirm hoặc success.
- Known commit với mirror false giữ `revocation_journal_unavailable` làm lỗi chính kể cả release cũng lỗi; giữ dấu gate cần recovery, không để wrapper response biến thành success hoặc che lỗi journal. Commit đã bắt đầu nhưng không biết kết quả ưu tiên `revocation_outcome_uncertain`.
- HTTP không tự replay bằng recovery role. Valid intent đã tới journal có thể được `recover` áp về sau dù HTTP lỗi; replay chỉ hạn chế, không chứng minh POST đã commit hay giữ được `cancelled_jobs` của attempt. Giữ các cửa sổ cả journal lẫn DB cùng lỗi: không hứa item đã revoked nếu chưa có commit DB hoặc intent durable; gate và lỗi phải phản ánh bằng chứng thực.
- Session ngắn/transaction luôn thử rollback và close, invalidate khi cần; release lỗi không thành 500, không rò connection hoặc trả success. Proof response dùng **Session/transaction mới**: common item SHARE/resources cần thiết → journal shared → current Admin/CSRF/latest head/receipt/clock → DTO/history/cleanup decoration → kiểm caller/TTL sau SQL cuối → release sạch. Không User/resource lock mới sau journal hoặc tombstone lock sau item.
- Disconnect/deadline/cancel không giết thread/fsync hoặc trả tài nguyên khi thread còn chạy. Trước intent có thể stop/rollback; sau append_started phải theo outcome/intent thực tế, không tự retry hoặc hứa “đã hủy, không thay đổi”. Ghi/test lựa chọn hoàn tất hạn chế hay rollback để replay, bảo đảm gate và không trả DTO khi caller đã mất.
- Audit từ chối nếu giữ là writer độc lập với current actor/gate, best effort, không thay proof; không dùng `_refuse` cũ hoặc thêm raw auth write. Không retry mutation khi audit/response lỗi.

## 6. Web, fixtures và kiểm chứng

UI revoke bắt đầu unticked, reason trống; thêm busy guard/disable ngăn double submit, bỏ tick sau lỗi. Lỗi transport/502/504/body mất/commit uncertain phải nói **không xác định kết quả thu hồi, tải lại trạng thái**, không tự POST lại hoặc khẳng định item chưa bị thu hồi. Journal lỗi nói hệ thống cần recovery và kết quả có thể đã đóng quyền. Sửa copy về ordinary content vs Admin history. Giữ timeout proxy normal hiện hành cho revoke; chỉ đổi khi có chứng cứ và test scoped, không lấy SLO/publish timeout làm timeout revoke.

Chuyển fixture setup/HTTP/race thích hợp sang `runtime_revoke` dùng Admin/session/CSRF/clock thật. Legacy raw helpers chỉ giữ khi cần replay/cleanup hệ thống hoặc corruption/adversary có nhãn; không dùng owner để bypass mirror rồi gọi đó là runtime. Giữ low-level `_revoke_rows`/`_cancel_jobs` hoặc tương đương cho recovery hiện hành. Không thay các domain expectation chỉ để tránh setup failure.

| Nhóm bắt buộc | Chứng cứ |
| --- | --- |
| Runtime/privilege/SQL | Admin/A/B, private/wrong/missing ID; literal booleans/retention trùng/thiếu; runtime thật không raw journal/auth writes; routine sai kind/ID/NULL/type/actor/time/epoch/key/retention bị từ chối trước append; record/confirm từ transaction khác bị chặn |
| Một known revoke | Pending/listed/indexed, conditions/permits khác nhau; epoch +1/tombstone/rights/publication/active NULL/cancel/mirror/audit đúng một transaction; same bytes của private/other common nguyên trạng; object không bị HTTP đọc/xóa |
| Gate/caller/clock | Logout/reset/disable/role/CSRF/head/receipt/hold thắng trước row/journal lock, trước append, sau fsync và response; idle/absolute sát biên với clock mới **sau SQL cuối/chờ khóa**; current denial không trả DTO cũ |
| Journal/outcome | None/torn/full-fsync-error; mirror false luôn đóng gate; confirm fault; DB/FK/audit lỗi trước append không ghi journal; rollback/landed/uncertain commit; lỗi rollback/close trước/sau release; client cancel/thread còn chạy. Phân biệt intent durable, t_rev và success |
| Race | Hai revoke chỉ một winner/journal/epoch; publish-vs-revoke hai thứ tự với runtime publish + runtime revoke; cleanup tombstone→item, worker item→job và auth writer với lock-wait/barrier thật, không timing sleep làm bằng chứng chính |
| Content/cleanup | Sau t_rev file/link đã lưu/chunk/citation/search/RAG mới bị chặn trước cleanup; active response qua boundary cũ có giới hạn rõ. Cleanup default/có căn cứ xóa đúng field/object/dẫn xuất, private cùng hash không đổi; job/checkpoint late-write không công bố lại |
| Restore | Snapshot trước revoke + journal hiện hành: gate đóng trước recover, replay đúng binding/retention/cancel, item/derived/object chung vẫn mất quyền; private nguyên trạng. Snapshot item absent và present-record hậu kiểm; recover lặp không tái mở, không cần creator/actor mới để chứng minh POST success |
| Migration/hồi quy | Upgrade 0041 có data/journal/retention sang head mới; head/noop/ORM; ACL provision fresh/existing, downgrade không gỡ safety của DB đã đăng ký. Review/approval/publish/download và các domain journal cũ vẫn đúng |
| Browser/proxy | Chrome thật qua Next proxy: unticked/reason/busy, revoke thành công, old original link bị chặn, double click không hai POST; 504 hoặc mất body sau known commit → unknown/UI không re-POST; expired cookie forwarding. Host socket API phải ghi rõ, không gọi đó là Compose API image |
| Pending HTTP | Production gate không patch: API 503/health sống/readiness đóng, không revoke/journal/storage side effect |

Chạy nhóm mới + migration/ORM, publication/publish, recovery/cleanup, approve/review writers và original/search/RAG/worker tests thực sự bị ảnh hưởng fixture/helper. Mở rộng theo caller tác động cụ thể; không bắt chạy toàn inventory khi business chưa C4 hoặc dùng test chưa chạy làm pass. Provider chỉ giả và chặn HTTP ngoài; worker/index/search/RAG legacy trong test giữ nhãn nếu chưa chuyển runtime auth.

Đối chứng có mục tiêu: mất final actor/clock, sai record/tombstone binding, bỏ fsync confirmation fence, raw mirror privilege rộng, outcome bị teardown xóa, response dùng proof cũ và lock order bị đảo. Chọn test tất định bắt assertion đúng lỗi; không tính setup error là mutant được bắt. Khôi phục source trước suite cuối; giữ log của mutant yếu/lượt chuẩn bị lỗi riêng.

## 7. Hoàn thành và bàn giao

- Tạo `docs/c2d5c2-common-revoke.md` và inventory mới với base/code/head, source migration/grants, phase/locks, failure matrix/authorization boundary, exact commands/node/JUnit unique/pass/fail/error/skip/not-run, source hashes trước/sau, mutants và các lỗi chuẩn bị. Cập nhật caller map/decisions/testing/roadmap/README/docs README; AGENTS chỉ thêm hợp đồng/lệnh có chứng cứ, không biến lựa chọn scoped thành luật vĩnh viễn.
- PR #48 đã merge; review 216 unique/64 web và artifact 2.016/7 mutant giữ source/thời điểm riêng, không thành pass mới ở merge/task này. Setup writer 503 và baseline drift ban đầu chưa rõ nguyên nhân: giữ follow-up riêng, không chặn tiến độ chỉ để chạy lại vô hạn; nếu tái hiện hãy ghi reason/trace có lọc secret rồi xử lý đúng nguyên nhân.
- Snapshot baseline container riêng ngay lúc bắt đầu và trước/sau cleanup; chỉ dọn exact ID/label/image/port/mount/sole-consumer đã kiểm. Nếu baseline drift, dừng delete không chắc ownership, ghi rõ mốc được chứng minh; không suy tất cả container nguyên trạng từ một snapshot mới. Không query/restart dev để điều tra drift ngoài scope.
- Không commit journal/paper/artifact/backup/credentials. Quét secret bằng fingerprint không in giá trị, kiểm docs links/control chars/diff. Giữ toàn bộ docs orchestrator/review chưa commit.
- Commit/push, mở PR tiếng Việt, không AI author/committer/Co-authored-by hoặc dòng quảng bá AI. Không merge. Báo kết quả và giới hạn thực tế; handoff bước sau là common-index/worker boundary, không tự bắt đầu C3/C4. **Safety vẫn pending tới C4.**
