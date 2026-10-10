# Prompt C2d-5e2: reconcile original mồ côi qua runtime

## 1. Nhiệm vụ, base và giới hạn

Bạn là coding agent chính. Hoàn thành **một PR C2d-5e2**: khảo sát, triển khai, kiểm thử, cập nhật docs, commit/push và mở PR **tiếng Việt** để chủ dự án review; **không merge**. Chuyển `python -m app.cli.reconcile_originals` và entry trực tiếp của nó sang runtime thật; chỉ dọn original không có bất kỳ SourceFile nào tham chiếu, sau grace và trong phiên vận hành đã quiescent. Tách DB khỏi S3, bảo vệ race tạo tham chiếu bằng fence bền vững, giữ mọi object còn tham chiếu và các outcome upload/approval không xác định.

PR #53 đã merge tại **`b2e76edb028af2ff118d3936ed4cdf47cb88a99f`**, head review `bf9d81f`, code `9139c01`. Đã fetch: `origin/main` tại merge này; merge tree khớp head PR. Source migration **0044**, không suy revision DB dev. Fetch/xác minh lại rồi tạo `feat/m6c2b2c2d5e2-orphan-original-reconcile-runtime` từ `origin/main`. Giữ toàn bộ docs review/task chưa commit của orchestrator, gồm [review lại PR #53](../reviews/pr53-c2d5e1-revoked-common-cleanup-runtime.md); có thể commit docs có sẵn riêng. Không reset/clean hoặc ghi đè thay đổi của người khác.

Đọc [AGENTS](../../AGENTS.md), [mục lục](../README.md), [yêu cầu](../product-requirements.md), [quyền và dữ liệu](../access-and-data.md), [quyết định giao](../decisions.md#giao-c2d-5e2-sau-merge-pr-53-2026-10-10), [cleanup runtime](../c2d5e1-revoked-common-cleanup-runtime.md), [upload original](../c2d4b-upload-original.md), [approval/copy](../c2d5b-review-approval-original-copy.md), [worker runtime](../c2d5d2a-worker-runtime-extraction.md), [nền C1](../c1-auth-safety-foundation.md) và [kiểm thử](../testing.md).

**Safety pending tới C4.** Không đọc/sửa `.env`/key, query/migrate/restart dev hoặc tài nguyên có sẵn, gọi provider/arXiv thật, đổi pricing/cutover hoặc làm C3/C4. Python chỉ Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước chạy/cài, không venv. DB/MinIO/Redis/journal/roles và artifact thử phải cô lập. Runtime không provision hoặc nhận credential owner/recovery; privileged setup/adversary phải có nhãn và không tính là runtime pass.

## 2. Phạm vi và khảo sát trước sửa

| Trong PR | Task riêng |
| --- | --- |
| CLI/direct orphan reconcile, runtime/gate checkpoints, orphan fence + bảo vệ SourceFile INSERT, bounded scan/resume, kết quả/audit tối thiểu | Online orphan GC, lịch beat tự động, durable lifecycle cho mọi upload/approval đang bay |
| Migration/grants hẹp cần thiết; hồi quy upload/approval, cleanup common và operator recovery | Discovery/citation, search/query/rerank, RAG/generation, private deletion/membership journal |
| Runbook quiescence và test process/crash cô lập phù hợp phạm vi | Production operator C3, cutover C4, backup scheduler, multipart lifecycle, live charge/SLO |

Khảo sát `cli/reconcile_originals.py`, `storage.py`, `api/{upload_original,review_approval}.py`, `ingest/{runtime,cleanup,celery_app}.py`, `publication.py`, `recovery.py`, `db_roles.py`, models SourceFile/ObjectDeleteFence và migration `0044` (Python + SQL); tìm mọi caller/test/script của reconcile và mọi writer SourceFile/key. Lập call/engine/key/lock/outcome map trước khi chọn schema.

Hiện trạng đã đọc, phải tái kiểm:

- CLI hiện giữ một Session xuyên `list_objects_v2`, SELECT và S3 delete; không actual runtime/gate proof, không fence chống INSERT chen giữa SELECT/delete, không bounded resume hoặc outcome checkpoint.
- `SourceFile.storage_key` không unique. Original có cùng checksum hoặc cùng key vẫn có thể thuộc nhiều item. Orphan là **không có SourceFile nào trỏ key**, không xét quyền của caller để che tham chiếu, và không loại tham chiếu chỉ vì item revoked.
- Upload/approval mint key riêng dưới `originals/`; timeout/cancel không giết SDK/DB thread. Commit uncertain hoặc rollback chưa xác nhận phải giữ object. Grace mặc định 24 giờ không chứng minh các thread/commit ấy đã kết thúc.
- Hợp đồng hiện tại của reconcile đã yêu cầu chạy thủ công khi mọi attempt cũ kết thúc. Không được âm thầm chuyển thành quét bucket online chỉ vì đã thêm fence.
- Fence `0044` chỉ cho **revoked common có tombstone journal-confirmed**. Không dùng UUID tombstone/item giả hoặc nới routine đó để authorize orphan không có tombstone.
- Cleanup common giữ SourceFile rows và ledger sau purge. Reconcile orphan không thay cleanup/recovery, không xóa rows nghiệp vụ hoặc mở gate.

Nếu docs/code khác, tái hiện và ghi quyết định cùng thay đổi; không sửa expectation/gate/grant để che mâu thuẫn.

## 3. Quyết định scoped của orchestrator

Các lựa chọn sau do orchestrator chọn theo ủy quyền, **không phải xác nhận trực tiếp mới của chủ dự án hoặc luật kiến trúc vĩnh viễn**.

### 3.1. Giữ reconcile thủ công trong môi trường quiescent

- **Không thêm Celery/beat schedule tự động.** Task chuyển ranh giới CLI/direct và cơ chế dọn, chưa chứng nhận online GC. Không thêm HTTP endpoint hoặc UI quản trị mới.
- Runbook phải dừng tiếp nhận upload/approval và mọi writer có thể PUT/copy/commit SourceFile; chờ toàn bộ attempt, SDK/DB thread và transaction cũ settle. Nếu dừng hoàn toàn process/container, vẫn phải xác minh các transaction/I/O từ xa cũ đã kết thúc; không coi process chết là chứng minh commit/PUT từ xa đã settle. Kiểm cả writer ngoài project/container nếu có. Khi không chứng minh được quiescence thì chưa chạy destructive pass; không suy từ proxy timeout, session expiry, Redis rỗng hoặc không thấy job.
- Có thể hỗ trợ preview/dry-run; preview không tạo fence/audit mutation hoặc xóa. Nếu dùng `--apply`, đây chỉ là lựa chọn thao tác, **không** phải bằng chứng runtime/quiescence hay cờ bỏ gate. Ghi rõ quiescence là điều kiện vận hành hiện tại, không tuyên bố CLI tự xác minh được mọi process/remote client.
- Quiescence **không phải recovery hold**. Pending/hold/journal lỗi vẫn chặn reconcile runtime, kể cả destructive pass khi writers đã dừng. Không dùng `recover`, owner fallback, `skip_gate` hoặc receipt/cutover test làm đường vận hành vượt pending. Trước C4 chỉ kiểm nghiệp vụ bằng fixture scoped trên môi trường thử; test gate production thật vẫn phải đóng.

### 3.2. Runtime và fence orphan riêng, cùng cơ chế serialize theo key

- CLI và entry trực tiếp dùng **cùng boundary runtime**. Kiểm actual login/grants và gate bằng helper thật; từ chối owner/recovery/SET ROLE/missing grant/misprovision/probe lỗi. Guard entry không thay các checkpoint sau. Không chọn operator bằng cách bắt lỗi runtime; giữ bản sửa P2 PR #53.
- Thêm revision mới sau head thực tế (dự kiến **0045**); không sửa `0044` đã merge. Chọn fence orphan riêng hoặc mở rộng schema có discriminator/ràng buộc tương đương; **không làm yếu binding revoked-common hiện có**. Không fake tombstone, item, rights epoch hoặc journal revocation.
- Orphan authority phải giữ exact storage key, operation/fence ID, mốc/căn cứ kỹ thuật tối thiểu; không nội dung, filename, URL, checksum nội dung hoặc ghi chú người dùng. Fence đã dùng không hết hạn, sửa/xóa hoặc bị downgrade làm mất âm thầm. Phân loại metadata vận hành này và tác động backup/restore; không thêm journal quyết định xóa paper riêng vì đây chưa là luồng xóa item của User.
- Prepare và **mọi SourceFile INSERT** serialize bằng cùng namespace/key lock của `0044`; trigger kiểm cả fence common và orphan. `READ COMMITTED` hoặc cơ chế chứng minh tương đương, không tin snapshot trước lock wait. Routine orphan chỉ authorize khi **zero SourceFile reference toàn DB**, kể cả private A/B, common pending/listed/revoked, item không còn hoặc dữ liệu binding bất thường. Có bất kỳ row nào thì giữ object, không orphan fence/delete.
- INSERT thắng trước: prepare thấy tham chiếu committed và giữ object. Prepare thắng: fence đã commit khiến INSERT key đó bị từ chối trước commit. Kiểm cả SQL thẳng/runtime, va chạm advisory hash và compatibility key mới của upload/approval. Fence không được dùng để hợp thức hóa quét khi upload còn bay: quiescence vẫn là điều kiện riêng.
- Routine/grant hẹp, binding NULL-safe, fixed search_path nếu SECURITY DEFINER; không broad protected DML/DDL/TEMP/owner/BYPASSRLS. Bảng protected mới phải có guard/provisioning tương ứng, giữ runtime actual-role tests xanh. SourceFile metadata update giữ nguyên key vẫn hoạt động.
- Vẽ lock graph gồm triggers/FK/audit/journal. Không lấy khóa hàng mới sau journal hoặc nâng shared journal lên exclusive. Không giữ key/row/journal lock hay connection DB khi list/HEAD/delete/chờ S3. Tránh tạo cycle giữa INSERT (resource → key → journal), prepare và finalize.

### 3.3. Candidate, prepare, delete và finalize tách pha

1. Entry proof sạch **trước mọi S3 list/HEAD/delete**, kể cả khi bucket rỗng. Scan chỉ configured bucket và prefix original hiện hành; không nhận arbitrary bucket/key qua CLI/direct để xóa. Không quét toàn bucket bằng prefix rỗng. Bounded số trang/object/xóa mỗi pass, giới hạn nằm trong config có validation/test; tránh starvation do mỗi pass luôn quét lại cùng trang toàn file được giữ. Resume/cursor chỉ là hint, không là quyền xóa.
2. List ngoài Session. Chọn object đủ `files.orphan_grace`, quy định rõ biên, UTC/timezone và clock mới. List LastModified chỉ là hint: HEAD/fingerprint mới ngoài DB trước prepare; object vừa ghi lại hoặc fingerprint/LastModified đổi thì bỏ candidate, không xóa dựa metadata cũ. Không dùng grace như bằng chứng absence/quiescence. Thiếu bucket/list lỗi/403/timeout/5xx không coi là danh sách rỗng thành công.
3. Prepare trong short transaction mới: actual runtime/gate → key serialization + zero-reference/routine → flush/constraints → journal exclusive trực tiếp → exact fence/reference/gate và fresh clock sau SQL cuối → **known commit + clean release**. Trả proof detached của đúng fence/key/observation, không ORM/Session sống.
4. Pre-delete trong transaction mới: gate/runtime, fence còn đúng, zero references toàn DB, clock mới và release sạch. Storage identity/age thay đổi giữa các pha phải giữ object và ghi mã phù hợp; fence đã commit vẫn giữ, không tự gỡ để thử INSERT lại. Không hứa HEAD/ETag chứng minh bytes bất biến hoặc xóa có điều kiện trên S3 khi adapter chưa có hợp đồng đó. Ghi rõ giới hạn same-key overwrite ngoài quiescence; không thêm PUT/copy/recreate trong reconcile.
5. S3 HEAD/delete/HEAD ngoài mọi DB connection/lock của attempt. Chỉ lỗi missing thật mới là absent; 403/timeout/5xx là lỗi, không success. Không abort incomplete multipart trong PR này. Chấp nhận at-least-once delete; không tuyên bố exactly-once S3 hoặc rollback S3 cùng DB.
6. Finalize/progress/audit trong transaction mới theo fence/binding/gate/fresh clock; không sửa item/version/file/job/rights/publication/tombstone/ledger/vector, không dựng lại object. Chỉ ghi đã xóa/vắng sau kiểm chứng storage; biệt lập known completion với error/uncertain. IDs/codes/counts, không key/filename/content/secret trong console/audit/log.
7. Lượt chạy độc lập sau phải resume được fence đã chuẩn bị dù object không còn xuất hiện trong S3 listing. Hai process cùng candidate không tạo quyền khác nhau, không hỏng tiến trình hoặc double-count nếu báo completion duy nhất. Thiết kế cursor/progress hữu hạn và idempotence rõ; fence bất biến có thể tách trạng thái progress riêng.

### 3.4. Outcome và recovery compatibility

- Prepare commit exception luôn uncertain, hoặc release lỗi sau known commit: **không S3 trong attempt ấy**, không SELECT suy landed, không retry mutation/error writer để đoán; dừng pass bằng mã rõ. Outcome thuộc attempt, teardown không xóa commit facts. Tick/CLI invocation độc lập sau mới resume qua durable proof.
- Delete timeout/response lost có thể đã xóa. Giữ fence/progress để lượt độc lập xác minh/resume, không báo success khi chưa biết. Final commit exception/release lỗi không báo completion chắc chắn, không retry hay xóa lại ngay trong exception handler.
- Gate đóng trước pre-delete boundary thì không bắt đầu delete. Nếu gate đóng sau admission, delete đã admit có thể xong; không lấy lại bytes/object, finalize bị chặn cho tới lượt hợp lệ. Không tiến tới candidate kế tiếp khi pass đã halt.
- Lỗi storage/DB/clock/cursor malformed có mã kiểm soát, exit khác 0 khi pass không hoàn thành; partial counts tách rõ, không in traceback chứa secret/key. Kết quả chưa chạy/bị từ chối không tính là deleted. Không trả success chỉ vì storage đã xóa nhưng finalize thất bại.
- Normal common cleanup, `operator_cleanup_revoked`, `_sweep_objects` và recover pending vẫn có đường riêng và phải hồi quy. Không đưa orphan boundary vào recover để vượt gate. Restore snapshot cũ không được mô tả là được orphan fence/journal bảo vệ toàn diện; journal độc lập, C3/C4 và rollback đồng thời vẫn là giới hạn.

## 4. Kiểm chứng bắt buộc theo rủi ro

PG/MinIO/journal thật cô lập, **login runtime thật**; fake provider chỉ để tạo dữ liệu hồi quy và chặn egress. Owner chỉ setup/corruption/adversary có nhãn. Kiểm writer/gate thật; fixture scoped ready không thay expectation HTTP production pending.

| Nhóm | Chứng cứ cần có |
| --- | --- |
| Entry/gate | CLI và direct: runtime hợp lệ; owner/recovery/missing grant/+TEMP/protected write/SET ROLE/probe/release lỗi; pending/hold/corrupt journal; trước cả list, không I/O/mutation khi từ chối |
| Candidate/grace | Trước/đúng/sau biên; UTC/clock trôi qua SQL wait; thiếu/sai metadata, object mới, HEAD đổi LastModified; pagination + bounded pass/cursor/candidate bị giữ không gây starvation; prefix ngoài phạm vi không bị động |
| Zero reference/isolation | Private A/B cùng bytes khác key; shared key; common pending/listed/revoked; tham chiếu có item absent hoặc binding lạ; mọi SourceFile đều bảo vệ. Chỉ orphan thật bị xóa, business rows và chi phí so từng hàng giữ nguyên |
| SQL fence/grants | NULL/sai binding, raw DML/UPDATE/DELETE/TRUNCATE/downgrade; grants runtime/provision mới; INSERT thắng trước và prepare thắng trước bằng barrier/lock wait; hash collision không gộp key; ORM/schema và upgrade DB có dữ liệu 0044 |
| DB/S3 ownership | Chặn list/HEAD/delete khi đang chạy và chứng minh attempt không giữ connection/row/key/journal lock; verify release lỗi không đi tiếp; không tạo bucket/write/copy original |
| Outcome | Commit landed/rollback rồi exception, rollback/close lỗi, final uncertain, storage 403/5xx/timeout/missing; dừng batch, no inference/retry, fence giữ và invocation độc lập resume; late SourceFile INSERT không thể commit vào key đã fenced |
| Idempotence/concurrency | Hai invocation cùng orphan; crash sau fence commit trước delete; crash sau delete trước finalize; resume kể cả object không còn trong listing; counts/audit theo contract đã chọn |
| Gate race | Hold thắng trước delete → no delete; hold sau admission → object có thể mất nhưng không finalize/success; không candidate kế tiếp |
| Compatibility | Upload và approval known/uncertain/cancel giữ outcome contract; retained copies vẫn tải theo quyền; common cleanup/late financial settlement; operator recovery/sweep pending; các auth journal/fence hiện có không yếu đi |

Chạy ít nhất một smoke CLI process thật trên Linux hoặc Compose cô lập với runtime credentials: create referenced + old orphan → dọn/resume → kiểm referenced bytes/rows giữ nguyên. Kill process thật ở một cửa sổ fence/delete nếu harness hỗ trợ; nếu chỉ inject exception thì ghi **fault injection**, không gọi đó là process crash hoặc power-loss. Môi trường phải quiescent theo runbook, không dùng việc đổi clock một mình làm chứng cứ settle.

Countercheck tập trung: bỏ runtime/gate, bỏ zero-reference, bỏ INSERT fence/cùng key lock, cho đi S3 sau uncertain/release, coi 403 là missing. Test phải fail bằng no-I/O/no-write/isolation/outcome assertion khi phù hợp; không chỉ khác mã deny. Khôi phục byte/hash trước suite cuối; không mutate code khi test khác đang chạy.

24 baseline debt PR #53 và bốn debt PR #50 giữ riêng. Chạy lại đúng ca liên quan khi cần đối chiếu base/head; không mở rộng quyền hoặc nới safety để làm xanh. Không bắt buộc chạy toàn hàng nghìn node nếu selection đã phủ các rủi ro thay đổi; ghi unique pass/fail/error/skip/not-run, exact collect/selection/JUnit, code SHA và source-before/after. ID tham số phải ổn định, không normalize UUID toàn inventory. Giữ mọi lỗi chuẩn bị/lượt dừng/mutant yếu riêng, không ghi đè hoặc cộng lịch sử thành pass mới. Web/Chrome chỉ chạy khi thực sự đổi web/proxy.

## 5. Bàn giao

- Handoff `docs/c2d5e2-orphan-original-reconcile-runtime.md` và inventory `docs/c2d5e2-test-inventory.json`: call/engine/lock/outcome map, fence/progress/cursor, quiescence/runbook, biên grace, compatibility, remaining callers và giới hạn. Phân biệt rõ bảo vệ reference bằng DB với điều kiện operational không có PUT/commit cũ đang bay.
- Cập nhật README/docs index/roadmap/decisions/testing/dev-setup/caller map/AGENTS. Đánh dấu PR #53 đã merge `b2e76edb`; giữ review lịch sử/source/agent/reviewer riêng. Runbook reconcile cũ chạy Compose owner phải được thay hoặc ghi superseded; chỉ ghi lệnh mới đã thực chạy, không thao tác dev để kiểm lệnh.
- Artifact/journal/credentials/backup ngoài repo; manifest hash, quét secret UTF-8/UTF-16, dọn đúng resource ID/label/sole consumer. Chụp tài nguyên có sẵn trước/sau và báo drift trung thực, không in raw inspect Env. Không đoán nguyên nhân drift worker/beat PR #53.
- Kiểm Markdown links/anchors/control chars, `git diff --check`, ORM/schema và config. Không thêm AI author/committer/Co-authored-by/trailer hoặc dòng quảng bá AI vào commit/PR.
- Commit/push, mở PR tiếng Việt, **không merge**. Báo base/code/head, migration/file, kết quả thực chạy/not-run, các lỗi và giới hạn; bàn giao discovery/citation là bước tiếp theo. **Safety vẫn pending tới C4**; chưa giao C3/C4 hoặc online GC.
