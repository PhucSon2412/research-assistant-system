# Prompt C2d-5e1: cleanup bản chung đã thu hồi qua runtime

## 1. Nhiệm vụ và base

Bạn là coding agent chính. Hoàn thành **một PR C2d-5e1**: khảo sát, triển khai, kiểm thử, cập nhật docs, commit/push và mở PR **tiếng Việt** cho chủ dự án review; **không merge**. Chuyển cleanup nền của bản chung đã thu hồi sang runtime thật, tách DB checkpoint khỏi S3 I/O và bảo vệ object dùng chung trước race liên kết mới. Giữ nguyên ý nghĩa thu hồi, metadata retention, ledger tài chính và dữ liệu private.

PR #52 đã merge tại **`ba20fda402840243f13bd4bb27e98c4c0cb17562`**, head review `97bc6e7`, code/test `43a6564`; head là ancestor và tree khớp merge. Source migration **0043**. Fetch lại, xác minh base/head thực tế rồi tạo `feat/m6c2b2c2d5e1-revoked-common-cleanup-runtime` từ `origin/main`; không suy revision DB dev. Giữ toàn bộ docs review/giao task chưa commit, gồm [review PR #52](../reviews/pr52-c2d5d2b-document-index-runtime.md); có thể commit docs có sẵn riêng. Không reset/clean/ghi đè thay đổi của người khác.

Đọc [AGENTS](../../AGENTS.md), [mục lục](../README.md), [yêu cầu](../product-requirements.md), [quyền và dữ liệu](../access-and-data.md#hợp-đồng-thu-hồi-và-xóa), [quyết định giao](../decisions.md#giao-c2d-5e1-sau-merge-pr-52-2026-10-10), [revoke runtime](../c2d5c2-common-revoke.md), [document runtime](../c2d5d2b-document-index-runtime.md), [worker/extraction](../c2d5d2a-worker-runtime-extraction.md), [nền C1](../c1-auth-safety-foundation.md) và [kiểm thử](../testing.md).

**Safety pending tới C4.** Không đọc/sửa `.env`, key, query/migrate/restart dev hoặc tài nguyên có sẵn; không gọi OpenRouter/arXiv thật, đổi pricing/cutover hoặc chạy C3/C4. Python/cài thư viện chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước chạy, không tạo venv. Setup/migration chỉ project thử cô lập; runtime không provision hoặc nhận credential owner/recovery/operator.

## 2. Phạm vi và khảo sát

| Trong PR này | Task riêng |
| --- | --- |
| `ras.revocation.cleanup`, CLI cleanup thông thường, runtime cleanup trực tiếp, durable object-delete fence, DB finalization/error checkpoint | Reconcile orphan, discovery/OAI/citation background, private deletion/membership journal |
| Tương thích revoke, document financial settlement, upload/approval insert, recovery cleanup/sweep cũ | Chuyển operator/recovery/C3, query/rerank/search/RAG runtime, C4/default Compose/API |
| Migration/fence/grants hẹp tối thiểu, fake provider và MinIO/PG/Redis thật cô lập | Provider live/charge, benchmark/SLO, backup scheduler/encryption, UI quản trị mới |

Khảo sát `publication.py` (`_purge_one`, `_remove_derived`, `_finish_jobs`, `_object_in_use`, `_remove_object`, `_remove_metadata`, `cleanup_revoked`), `recovery.py` (`_cleanup`, `_sweep_objects`), CLI cleanup/reconcile, `ingest/{celery_app,runtime,document_index,document_accounting}.py`, `storage.py`, `db_roles.py`, `models/{library,rights,index}.py`, migrations 0022–0025/0042–0043 và tests cleanup/publication/recovery/revoke/index/upload/approval.

Hiện trạng đã đối chiếu từ source, cần tái kiểm trước sửa:

- `_purge_one` khóa tombstone → item, xóa dẫn xuất rồi gọi S3 trong cùng Session/transaction; `cleanup_revoked` tự commit và có nhánh commit lỗi lại ghi error bằng transaction khác. Entry gate của task không đủ chứng minh boundary nội dung đã chuyển.
- `SourceFile.storage_key` **không unique**, được phép phục vụ nhiều item; identity/key không sửa được nhưng vẫn thêm được SourceFile mới. Hai lượt SELECT trước/sau xóa không ngăn mất file của một liên kết được thêm trong khe giữa.
- `recovery._cleanup` gọi cùng helper, còn `_sweep_objects` xóa object từ journal kể cả tombstone đã `purged_at`. Recovery cần chạy khi gate đang đóng; thay helper chung bằng gate runtime một cách máy móc sẽ làm recover không hoàn tất.
- Ledger document giữ nonce/day/ceiling/cost sau cleanup; late settlement chỉ tài chính, không lấy item lock. Cleanup không được đổi reservation thành rejected, giải tiền hoặc xóa call/job đang cần đối soát.
- Test cũ cho storage failure đòi nội dung DB chưa bị scrub; lỗi sau xóa object rollback DB nhưng không dựng lại object. Durable deletion fence mới là metadata vận hành được phép giữ sau lỗi; ghi rõ thay đổi này, không nói toàn DB đã rollback như chưa từng có prepare.

Vẽ call/engine/lock/outcome map và kiểm runtime grants trước chọn migration. Nếu code/docs khác nhận định trên, tái hiện bằng chứng và sửa quyết định cùng docs, không dựng fallback hoặc giữ một giả định sai để làm test xanh.

## 3. Quyết định thiết kế scoped của orchestrator

Các lựa chọn sau do orchestrator chọn theo ủy quyền, **không phải xác nhận trực tiếp mới của chủ dự án hoặc luật kiến trúc vĩnh viễn**.

### 3.1. Runtime và operator tách điểm vào

- Runtime cleanup không cần Principal/cookie Admin. Quyền là **tombstone thu hồi common hợp lệ, đã journal xác nhận**, không phải một ID/key tùy ý truyền vào. Kiểm actual runtime bằng guard thực; owner/recovery/thiếu grant bị từ chối, không fallback hoặc provision.
- `ras.revocation.cleanup`, CLI cleanup thông thường và entry trực tiếp phải dùng cùng boundary runtime mới. Entry guard chưa đủ: mỗi prepare/pre-delete/finalize/error writer có short transaction, gate/binding/clock riêng.
- Recovery cleanup/sweep vẫn là **đường operator riêng**, được gọi từ recovery orchestration khi traffic/writer đã dừng; không dùng runtime gate cần ready. Giữ hành vi recovery hiện có, không coi đã chuyển C3 hoặc chứng nhận toàn operator. Có thể tách/đặt tên lại helper legacy để call graph rõ, nhưng không thêm `skip_gate`, `during_recovery=True` hay engine fallback cho runtime caller tự chọn.
- Runtime không import/call nhánh operator để vượt pending/hold; operator không làm gate ready hoặc tạo receipt/credential mới. Các test direct system/adversary dùng privileged engine phải gắn nhãn, không tính là runtime pass.

### 3.2. Fence bền vững trước khi nhả DB để xóa

Chọn **fence bền vững theo object key** cho key đã được chấp nhận xóa, cùng cơ chế serialize ở PostgreSQL cho mọi SourceFile INSERT dùng key đó. Không dùng unique trên `storage_key`, không xóa chỉ vì checksum trùng và không giữ session/advisory lock qua S3.

- Thêm revision mới sau head thực tế, dự kiến **0044**; không sửa migration đã merge. Fence chỉ giữ key, ID/token, tombstone/binding/mốc/mã cần thiết, không nội dung file, tên file, URL hoặc ghi chú quyền. Phân loại đây là metadata quyết định xóa, cần giữ sau cleanup; không xóa cùng derived rows hoặc mất fence khi `purged_at`.
- Cơ chế đề xuất cụ thể: prepare và trigger SourceFile INSERT dùng **cùng transaction advisory lock theo key, có namespace ổn định**. Prepare chỉ lấy key lock rồi đọc tombstone/item/reference bằng scalar **không khóa item/job/tombstone/resource** trong transaction đó. Đây là chủ ý để tránh vòng key → item với writer SourceFile/FK đang khóa item → key. Lock hash collision chỉ được serialize bảo thủ, không được coi hai key là cùng object.
- Dưới key lock, prepare xác minh đúng tombstone common/epoch/version/file/key, item còn tồn tại phải revoked; target vắng trong snapshot chỉ theo tombstone/journal hợp lệ. Kiểm **mọi** SourceFile cùng key: private, item vắng hoặc common chưa revoked đều là tham chiếu cần bảo vệ. Có tham chiếu hợp lệ → `shared_kept`, **không** tạo quyền xóa. Không có → tạo fence bền vững, flush/constraints, journal exclusive trực tiếp, recheck scalar binding/reference/gate và clock, known commit + release sạch.
- Trigger INSERT phải lấy cùng key lock và từ chối key đã fenced, kể cả SQL thẳng bằng runtime; không chỉ kiểm trong Python upload/approve. Nếu insert thắng trước, prepare nhìn thấy tham chiếu đã commit và giữ object. Nếu prepare thắng, insert bị chặn **trước khi được commit**; fence không hết hạn vì timeout/worker chết. Các SourceFile đang tồn tại vẫn được giữ và đọc theo quyền riêng.
- Fence không được sửa/xóa qua raw runtime DML. Chọn routine/grant hẹp để chuẩn bị fence có binding thật; không cấp broad protected DML/owner/TEMP/DDL/BYPASSRLS hoặc GUC/reason bypass. Nếu dùng SECURITY DEFINER: fixed search_path, SQL parameterized, quyền tối thiểu và binding NULL-safe. Kiểm cả SQL lẫn Python, không tin request/DTO thay DB proof.
- Chỉ mở quyền insert cho key mới khác fence; upload/approval vốn mint key mới vẫn hoạt động. Không tự tạo physical sharing hoặc tự đổi key của item hợp lệ. SourceFile metadata update không đổi key vẫn hoạt động theo hợp đồng cũ.
- Fence prepare chỉ authorize delete sau known commit/release và pre-delete proof mới. Commit uncertain hoặc release lỗi → **không S3** trong attempt ấy. Lượt tick độc lập sau được đọc durable fence và resume bằng proof mới; không đoán landed từ SELECT trong exception handler.

Nếu khảo sát chứng minh cần biến thể tương đương, phải ghi lý do và chứng minh hai thứ tự concurrent INSERT/prepare cùng lock graph không cycle. Không thay fence bằng hai SELECT hoặc lời hứa “key ngẫu nhiên nên không thể trùng”.

### 3.3. Pha S3 và finalization

1. **Candidate:** batch hữu hạn theo `cleanup_batch_size`, ưu tiên `purge_due_at` rồi ID như hiện hành; deadline là hạn phải dọn, không phải ngày bắt đầu được phép dọn. Candidate không là proof; hai worker/task trùng phải an toàn.
2. **Prepare và pre-delete:** như fence trên; trả DTO/proof detached, immutable. Pre-delete có transaction mới, exact fence/tombstone/target/reference/gate, clock sau SQL/journal wait; Session release sạch rồi mới I/O. `no_object` không gọi S3; `shared_kept` không delete key được bảo vệ.
3. **S3:** HEAD/delete/HEAD bằng key đã authorize, ngoài mọi DB Session/connection/row/journal/key lock của attempt. Chỉ missing-object thực mới là `already_absent`/delete đã xác minh; 403/timeout/5xx không là vắng. Dùng timeout/retry config storage hiện hành, không hardcode/trả raw exception. Không PUT/copy/recreate original sau lỗi. Hai delete cùng key có thể được thực hiện at-least-once; chỉ một finalization/audit thành công cho tombstone, không hứa S3 exactly-once.
4. **Final transaction:** khóa **tombstone → item** như cleanup cũ (revoke/replay khóa item rồi insert, không khóa tombstone); khảo sát mọi caller để giữ graph không cycle. Nếu target tồn tại phải đúng common/revoked/tombstone tuple/epoch; item private hoặc binding sai đóng. Khóa resource/FK/call/job/membership cần thiết trước journal; không lấy key lock ở đây, chỉ đọc fence đã bền vững/không sửa được.
5. Dọn theo FK và phạm vi cũ: active pointer/status → citation links → references → index entries → scrub vector/chunk hash của calls → generations → chunks → sections → PDF pages → candidates; kết thúc unfinished attempts/jobs, clear lease, xóa memberships, scrub metadata không có căn cứ đúng trường. Giữ item/version/file/rights/publication/request/tombstone/job/ledger và cơ sở retention tối thiểu theo hợp đồng.
6. Khóa/journal/flush phải tính cả FK, triggers và audit: flush + constraints trước journal exclusive, sau đó scalar hậu điều kiện và clock mới. Không gọi S3, connection thứ hai, row/key lock mới sau journal. Chỉ đặt `purged_at` khi object đã xác minh vắng, hoặc có proof mới về tham chiếu hợp lệ để `shared_kept`, hoặc không có key; DB leftover/retention fence vẫn chặn purge chưa đủ.
7. Nếu item khác có index trỏ tới chunk/call của target: dừng `derived_rows_shared`, không phá item kia. Cùng checksum nhưng key/row khác xử lý độc lập. Không khóa User chỉ để ghi system audit; không sửa auth/session/receipt/journal quyết định.

Fence là authorization xóa object; checkpoint finalization là quyết định nội dung. Gate/hold có thể đổi sau pre-delete boundary: delete đã được admit có thể hoàn tất, không lấy lại được; **không bắt đầu delete mới khi gate thắng trước boundary**, không final commit khi gate đã đóng. Để unpurged và retry/operator xử lý. Ghi và kiểm giới hạn này; không hứa rollback S3 cùng DB.

## 4. Outcome, accounting và recovery

| Cửa sổ | Hợp đồng |
| --- | --- |
| Prepare commit/release không xác định hoặc lỗi | Dừng, không delete, không inference/replay/retry trong attempt |
| S3 lỗi trước hoặc sau delete | Không purged; không scrub DB content như thể đã xóa thành công; object có thể đã mất. Fence giữ, retry mới xác minh lại |
| Object vắng, final DML hoặc gate từ chối trước commit | Rollback content; fence giữ, original không được dựng lại; lượt sau resume an toàn |
| Final commit exception, landed hoặc rollback | Outcome uncertain, không success/count purged/SELECT suy landed hoặc error writer tiếp. Dừng batch; lượt độc lập mới xem durable state |
| Known final commit rồi teardown lỗi | Giữ mutation; không rollback outcome thành precommit hoặc tạo cleanup success giả trong response |
| Lỗi nghiệp vụ known trước commit + rollback/release sạch | Có thể error checkpoint transaction mới, exact unpurged/binding/gate + clock; chỉ IDs/mã, không raw exception. Không dùng đường này sau uncertain |
| Hai cleanup đồng thời hoặc message lặp | At-least-once delete an toàn, finalization idempotent, không hai success audit hoặc counters/metadata scrub sai |
| Provider result về muộn trong/sau cleanup | Financial nonce/day/ceiling/tokens/model/provider giữ, settle một lần; không vector/hash/index resurrect, không giải reserve theo suy đoán |

Outcome thuộc attempt, độc lập rollback/close/invalidate. Session ngắn luôn thử rollback và close, invalidate khi release lỗi; lỗi phải trả mã có kiểm soát, không traceback/DSN/nội dung. Nếu có thread, attempt sở hữu tới thread settle; không giả hủy được thread đang xóa S3. Không mở scheduling/retry vô hạn, không đếm lần rerun thành unique test.

Recovery `_cleanup` và `_sweep_objects` vẫn phải dọn bản bị restore, kể cả `purged_at` đã có nhưng object quay lại. Không auto ready khi reconcile xong; safety pending. Journal/fence/DB cùng rollback vẫn có giới hạn storage đã công bố; không tuyên bố restore production an toàn. Giữ journal tối thiểu hiện hành, không thêm toàn văn/URL/bằng chứng vào journal hoặc log.

## 5. Kiểm thử và evidence

PG/pgvector + MinIO + Redis/journal cô lập, role runtime thật; setup/migration/operator chỉ fixture gắn nhãn. Explicit ports/project/env file và settings trước import, không `.env` fallback/dev default port. Provider fake qua adapter chỉ để dựng index/late result; egress provider/arXiv thật bị chặn. Scoped cutover chỉ test nghiệp vụ; pending/hold/missing-journal/owner-role giữ guard production thật, không thêm bypass production.

| Nhóm | Phải chứng minh |
| --- | --- |
| Entry/gate | Celery + CLI + direct runtime; owner/recovery/thiếu grant/parts bị chặn; pending/hold/corrupt/missing journal không mutation/S3; current gate trước từng checkpoint/delete và sau I/O |
| Fence/SQL | NULL/type/binding sai; raw INSERT dùng fenced key bị từ chối; runtime không sửa/xóa fence; hai thứ tự insert-first/prepare-first quan sát lock bằng barrier; không key→item cycle; hai key khác nhau không bị đồng nhất |
| Isolation | Private A/B cùng checksum, shared physical key hợp lệ, common sống/pending, orphan item/ref; giữ object/rows hợp lệ; target private/wrong tuple từ chối; upload/approval key mới và metadata update hồi quy |
| Cleanup đầy đủ | Markdown/PDF/reference/citation/index/vector/candidate + membership + jobs; DB scan marker biến mất đúng target, tối thiểu ledger/audit/retention còn; default scrub và per-field basis; derived_rows_shared bị giữ, idempotence |
| I/O/clock | Block HEAD/delete/verify sau delete: API/worker khác không chờ DB lock do cleanup; gate/epoch/binding đổi; connection của attempt release; fresh clock sau SQL/lock wait; không giữ journal/key lock qua S3 |
| Failure/outcome | Prepare/pre-delete/final/error checkpoint commit landed-then-error/rollback; teardown rollback/close lỗi; S3 403/timeout/5xx vs missing, delete xong DB lỗi; no inference/retry/success/đụng item khác |
| Index race | Worker hash/provider/store/publish vs cleanup; late heartbeat/reaper/settle hai thứ tự, ngày gốc và nonce cũ; giữ tiền đúng, không vector resurrect hoặc deadlock |
| Recovery compatibility | Hold và journal replay dùng operator riêng; unpurged cleanup, purged nhưng object restore quay lại, repeated recover; không mở safety; raw runtime không đi vòng qua helper operator |
| Process integration | Worker + beat Linux thực qua override runtime, PG/MinIO/Redis thật, task cleanup tới purged; restart giữa prepare/delete/finalize trên durable state; connection process không owner/TEMP/protected DML; không provider thật |

Chạy countercheck nhắm đúng guard quan trọng: bỏ gate cuối, fence SourceFile insert, ref protection, nonce/accounting preservation, final postcondition, uncertain handling hoặc release; mỗi kết quả phải có assertion nghiệp vụ/no-I/O/no-write. Mutant yếu/setup error không tính đạt. Khôi phục bytes/hash trước lượt sealed, không sửa source hoặc chạy mutant song song suite.

Hồi quy theo call graph: cleanup/publication/recovery/revoke, worker/document index/late accounting, upload/approval (SourceFile trigger), ORM/migration và source-key fixtures. Bốn baseline debt PR #50 giữ riêng: hai common-search corruption fixture thiếu `session_replication_role`, readyz kỳ vọng 200 khi pending, common-original helper revoke 409. Nếu node bị tác động trực tiếp, tái hiện base/head và sửa đúng quyền/kỳ vọng; không cấp runtime quyền đặc biệt hoặc nới gate. Không mở rộng suite vô hạn để đạt một tổng số.

Giữ collect/selection/JUnit chính xác, unique pass/fail/error/skip/not-run, code SHA và source hashes trước/sau. Artifact/credentials/journal/backup ngoài repo, manifest/hash và quét secret UTF-8/UTF-16; raw errors/lượt dừng/countercheck yếu giữ riêng, không ghi đè. Không web/Chrome nếu không đổi web; nếu cần đổi UI/proxy phải kiểm riêng đúng luồng browser.

## 6. Hoàn thành và bàn giao

1. Celery/CLI/direct cleanup bình thường dùng runtime boundary mới, không S3 trong DB transaction, không đi đường recovery để vượt gate. Bản revoked được scrub/purge đúng phạm vi, private/shared object còn quyền được giữ; key đã authorize delete không nhận SourceFile mới.
2. Migration mới sau head thực tế; ORM/schema xanh. Giữ auth/journal/retention/derived fences cũ, quyền tối thiểu; downgrade không được âm thầm làm mất deletion fence đã dùng. Mọi bảng/routine mới có test DB độc lập và tác động restore/cleanup được phân loại.
3. Handoff `docs/c2d5e1-revoked-common-cleanup-runtime.md`, inventory `docs/c2d5e1-test-inventory.json`: call/engine/lock/outcome map, fence lifecycle, shared-key race, S3 boundary, late accounting, recovery compatibility, actual config và caller còn legacy. Ghi rõ các gap và phần chưa đo; không tuyên bố exactly-once S3/rollback object/power-loss durability.
4. Cập nhật README/docs index/roadmap/decisions/testing/dev-setup/caller map; PR #52 đánh dấu merged. AGENTS trỏ handoff và chỉ thêm lệnh đã chạy. Các con số deadline/batch/storage timeout là config hiện hành hoặc đề xuất cần đo, không biến thành SLO đã đạt.
5. Cleanup chỉ tài nguyên thử theo exact ID/label/sole volume consumer; snapshot tài nguyên có sẵn, báo drift trung thực, không in raw inspect Env. Giữ evidence đã quét secret, xóa credentials thử; kiểm Markdown links/anchors/control chars và `git diff --check`.
6. Không AI author/committer/Co-authored-by/trailer hoặc dòng quảng bá AI. Commit/push, mở PR tiếng Việt, không merge. Báo ngắn base/code/head, file/migration, kết quả thực chạy/not-run, lỗi/giới hạn và bàn giao. **Safety pending tới C4**; reconcile orphan → discovery/citation → search/query/rerank → RAG/generation → tổng hồi quy C2d tiếp tục riêng; C3/C4 theo điều kiện đã ghi.
