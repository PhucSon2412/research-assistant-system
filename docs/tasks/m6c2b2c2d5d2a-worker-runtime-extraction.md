# Prompt C2d-5d2a: worker runtime, claim và extraction

## 1. Nhiệm vụ và base

Bạn là coding agent chính. Hoàn thành **một PR C2d-5d2a**: khảo sát, triển khai, kiểm thử, cập nhật docs, commit/push và mở PR **tiếng Việt** để review; **không merge**. Chuyển đường ingest nền từ dispatch/claim tới kết thúc extraction/chunking sang runtime thật, với gate, binding, lease và checkpoint có kiểm lại. Kết quả thành công của PR này là **`awaiting_index` + item `metadata_only`**, chưa phải `ready`/`indexed`.

PR #50 đã merge tại **`03b79855221f3d9279cbe5aa677d6a50db59b5ab`**; head PR là `ff5fa82`, code đã kiểm `e283bc6`, source migration **`0042`**. Fetch lại, xác minh ancestor và head thực tế trước khi tạo `feat/m6c2b2c2d5d2a-worker-runtime-extraction`. Không suy revision DB dev từ source. Giữ toàn bộ docs review/giao task chưa commit, gồm [review PR #50](../reviews/pr50-c2d5d1-common-index-request.md); không reset/clean hoặc ghi đè thay đổi có sẵn.

Đọc [AGENTS](../../AGENTS.md), [mục lục](../README.md), [yêu cầu](../product-requirements.md), [quyền và dữ liệu](../access-and-data.md), [quyết định giao](../decisions.md#giao-c2d-5d2a-sau-merge-pr-50-2026-10-09), [nền C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [handoff PR #50](../c2d5d1-common-index-request.md#bàn-giao-c2d-5d2-checkpoint-worker-còn-mở), [revoke](../c2d5c2-common-revoke.md), [caller inventory](../c2d1-caller-inventory.json), [PDF](../m2b-pdf.md) và [kiểm thử](../testing.md). Khảo sát `ingest/worker.py`, `ingest/celery_app.py`, `ingest/indexer.py`, `auth_safety.py`, `recovery.py`, `db.py`, `db_roles.py`, config, Compose, derived fences và các fixture runtime trước sửa.

**Safety vẫn pending tới C4.** Không đọc/sửa `.env`, key; không query/migrate/restart stack dev hoặc tài nguyên có sẵn; không gọi OpenRouter/arXiv thật, đổi `pricing_verified`, mở cutover, chạy operator C3 hay bắt đầu PR tiếp theo. Python/cài thư viện chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`, kiểm interpreter/pip trước chạy; không tạo venv.

## 2. Phạm vi được chốt theo ủy quyền

Orchestrator chia C2d-5d2 thành các PR có thể kiểm riêng:

| Bước | Kết quả |
| --- | --- |
| **C2d-5d2a — PR này** | Runtime worker/beat được kiểm trên cấu hình cô lập; dispatch ID, claim/lease, validate/extract/chunk, checkpoint thành công/lỗi và pause an toàn tới `awaiting_index` |
| C2d-5d2b — sau merge | Document embedding/index/provider dispatch, reservation/heartbeat/outcome và công bố keyword/vector generation qua runtime |
| Task riêng sau đó | Cleanup/reconcile/discovery background còn thiếu boundary; search/query embedding/rerank, RAG/generation; tổng hồi quy C2d, C3/C4 |

- Chuyển cả origin **`upload` private** và **`promotion` common** trong các bước extraction hiện có. Không thêm nguồn/định dạng/parser mới, OCR, reindex, graph, đổi ranking/pack hoặc AI summary.
- Các entry chính: `ras.ingest.discover` → `due_job_ids` → `ingest_job.delay`, `ras.ingest.process` → `process_job`, `claim_job`, `_begin`/`_finish`/`_fail`, validate/extract/chunk và citation reconcile **trong transaction extraction**. Điểm vào trực tiếp phải có boundary, không chỉ wrapper Celery.
- **Không gọi indexer legacy qua đường worker đã chuyển**, kể cả cấu hình test có key/price flag. Dispatcher/claim chỉ nhận extraction states; `awaiting_index`, `indexing` và retry có `resume_state=indexing` được giữ nguyên để PR sau xử lý. Không reset chúng về validate, đốt attempt, giải phóng reservation, dựng index/generation hoặc giả terminal failure. Nhánh này phải có kết quả skip có mã rõ và test chặn provider.
- Gate đầu vào của cả năm task Celery hiện có vẫn phải đóng khi pending/hold/journal lỗi. Không tuyên bố các task discovery/reconcile/cleanup đã chuyển chỉ vì chúng chạy bằng runtime hoặc qua gate đầu. Cập nhật map để phân biệt **đã chuyển / chỉ kiểm tương thích / còn legacy**.
- Job là công việc bền vững của ứng dụng; cookie/session Admin tạo job không là quyền của worker. Admin logout, reset hoặc mất role sau known acceptance không tự hủy job chung nếu rights còn hợp lệ. Không dựng Principal hoặc session giả cho worker.
- Với private job, chốt scoped cho PR này: owner UUID phải đúng, status **active/proven theo latest status head + receipt** trước đọc nội dung và ghi checkpoint. Disabled/held/không chứng minh được status thì **pause**, không công bố hoặc ghi thêm dẫn xuất; không biến pause thành terminal failure/xóa dữ liệu. Không đòi session hoặc credential generation của người upload vẫn như lúc upload, không tự enable/giải quarantine. Khi owner đủ điều kiện trở lại, dispatcher có thể tiếp tục job hợp lệ. Đây là lựa chọn orchestrator cho background extraction, không là luật vĩnh viễn hoặc quyết định trực tiếp của chủ dự án.

## 3. Engine/role và cấu hình kiểm được

Hiện `get_engine()` dùng `RAS_DB__*`; **Compose mặc định còn truyền `POSTGRES_USER` cho cả API/worker/beat**. Không chỉ thay engine trong fixture rồi báo worker đã hết owner.

- Đường worker/beat được chuyển phải dùng role **runtime hiện có** đã provision bên ngoài tiến trình, không owner/migrator/recovery/definer, không membership nâng quyền, DDL/TEMP/BYPASSRLS/raw protected auth writes. Ưu tiên reuse role và grants hiện hành; không thêm role/hệ thống credential mới khi chưa có nhu cầu được chứng minh.
- Cung cấp override Compose/script cô lập có thể chạy lại: worker/beat nhận `RAS_DB__*` runtime rõ ràng, không fallback `POSTGRES_USER`; thiếu/sai/privileged credential thì fail closed trước dispatch/claim/content I/O. Không mang credential migrator/recovery vào container worker/beat hoặc mở connection đặc quyền từ worker. Provision/migrate do bước setup riêng, không runtime tự provision.
- **Không đổi mặc định Compose dev sang cấu hình đã cutover ở PR này.** Override là cấu hình kiểm runtime; README/runbook phải nói rõ mặc định và API integration còn thuộc C4. Không restart dev để chứng minh override. Nếu cần biến override mới, không secret mặc định; ghi rõ cách cấp bằng file env thử ngoài repo và không in giá trị. Chạy Compose với env file thử tường minh, tránh tự nạp `.env` repo.
- Chứng minh `current_user` thực trong worker/beat và SQL permissions bằng kết nối đó. Có negative test owner/role thiếu grant; không âm thầm fallback engine owner khi lỗi permission. Operator/recovery sử dụng engine riêng chỉ trong setup/restore được gắn nhãn.
- Ưu tiên không migration/grants mới. Nếu có thiếu quyền/guard thực sự, ghi bằng chứng, giới hạn đúng nhu cầu và thêm revision **sau head thực tế**, không sửa migration đã merge hoặc mở quyền protected để fixture xanh. Giữ ORM/schema test.

## 4. Dispatch, claim, lease và transaction

### Dispatch và claim

- PostgreSQL là queue bền vững; Redis chỉ mang **job UUID**, không paper text, storage key/URL, cookie, token hoặc verifier. Không dựa vào Celery result để xác định quyền/job đã chạy. Beat phát tick theo lịch không đồng nghĩa job đã được cấp quyền; ngay trước gửi ID ingest phải kiểm gate, và consumer luôn tự kiểm lại.
- `due_job_ids` chỉ chọn extraction job đến hạn, tuple/scope phù hợp và không đang có lease còn sống. Candidate query không phải proof cho claim. Gate thay đổi sau SELECT thì không tiếp tục phát ID theo proof cũ; lỗi broker không làm mất job hoặc đánh dấu job đã xử lý. Không giữ connection/transaction/row/journal lock trong lúc gửi broker.
- Claim hiện khóa job `SKIP LOCKED` mà không item; chuyển sang thứ tự nhất quán **private owner User nếu cần → item → job → binding resources → journal**, common không lấy khóa actor User đã tạo job. Với candidate lookup không khóa, đọc lại scalar binding sau khi khóa; nếu dùng SKIP LOCKED thì không khóa job trước rồi chờ item.
- Serialize claim/revoke bằng item trước job; thử cả hai thứ tự. Chỉ một claimant nhận lease mới, tăng attempt đúng một lần. Message trùng, hai worker, process restart/Redis outage không tạo hai attempt cùng quyền. Lease token cũ không ghi được kết quả khi worker mới đã claim.
- Claim/renew/finish đọc clock mới **sau lock wait và SQL predicate cuối**, không dùng timestamp lúc task bắt đầu. Đúng biên `lease_until` đã hết; lock wait vượt hạn không được gia hạn bằng proof cũ. Retry/backoff/max attempts giữ config; branch exhausted cũng phải tuân thủ item/job/gate và không động ledger index ngoài scope.

### Checkpoint ghi

1. Initial integrity/cutover check ở transaction ngắn đã release; không gọi helper dùng connection thứ hai trong lúc giữ resource locks.
2. Khóa theo thứ tự trên; đọc scalar tuple/state/cancel/epoch/lease, private current status hoặc common rights/publication/tombstone gate. Không đọc Admin cookie làm quyền. Common giữ `common_index.block_reason` đúng tuple/file/checksum/epoch, `listed`, audience, fulltext + external permission, không conditions. Private giữ owner/scope/file, không suy quyền từ hash trùng.
3. Ghi đúng job/attempt/derived rows và flush mọi DML/FK/unique/audit/citation trước journal. Claim chỉ cấp lease; begin/finish/failure không được dùng ORM snapshot cũ thay scalar recheck. Không khóa tombstone sau item; predicate đủ để kiểm tồn tại.
4. Lấy journal **exclusive trực tiếp** cho checkpoint writer theo `auth_safety.require_final(writer=True)` hoặc helper tương đương, không shared rồi upgrade. Gate kiểm integrity + cutover trong cùng transaction. Sau SQL cuối kiểm lại binding/state/lease/cancel/quyền/status và clock mới trước commit; không lấy thêm User/resource lock sau journal. Đây là lựa chọn scoped reuse boundary hiện hành, không yêu cầu journal event mới cho mỗi bước ingest.
5. Commit outcome và release sạch. Không giữ connection/locks khi đọc S3, parse, đợi thread, gửi broker hoặc gọi provider. Journal file read trong helper gate hiện hành không bị hiểu thành yêu cầu chuyển gate ra ngoài transaction.

Session ngắn phải thử rollback/close và invalidate khi release lỗi; không tiếp tục I/O bằng proof chưa release sạch. Exception SQL/teardown có mã kiểm soát, không log raw exception chứa dữ liệu. Hợp đồng thông báo task không phải HTTP DTO: chỉ ID/mã/trạng thái, không text/path/secret.

## 5. I/O, parser và dẫn xuất

- Tách proof/DTO bất biến khỏi ORM Session trước I/O. Mỗi lần đọc original dùng exact source binding, kiểm **actual size và SHA-256**, gồm ghi đè cùng kích thước. HEAD không thay phép băm. Release Session/connection trước S3/parser; storage/parser bị giữ bằng barrier thì revoke vẫn commit được.
- Sau đọc/parse/chunk, transaction ghi mới phải kiểm lại gate, item/file/version/checksum/epoch/job/lease/cancel và owner status. Kết quả tính trước thu hồi/lease lost không được ghi vào item đã đóng quyền. Dữ liệu đã đọc vào memory không tự làm job có quyền; bỏ kết quả khi proof cuối thất bại. Nêu rõ khe overwrite sau lần hash cuối; không hứa ngăn tuyệt đối object bị thay giữa hash và commit.
- Giữ Markdown/PDF pipeline version, byte/page/parser timeout/memory/output limits và coverage hiện hành. Không đổi ngưỡng 70%/100 ký tự/2.000 ký tự hoặc classifier để làm test xanh. PDF body không đạt/biên bibliography đáng ngờ đi đúng `needs_review`/`fulltext_rejected`, không `indexed`; trang thân thiếu vẫn thể hiện coverage/locator.
- Reference list/citation link không vào body evidence; reference reconcile trong extraction giữ exact-ID, tombstone/stale/matcher guards và khóa riêng của citations **trước journal**. Không bắt đầu background citation backfill mới. Locator phải khớp original text/page của đúng version/file; không dùng rows `layout-v1` để hoàn tất pipeline `layout-v2`.
- Retry/reuse cùng file/pipeline chỉ dùng lại kết quả khớp, không nhân đôi sections/pages/references/chunks, không chia sẻ tự động giữa owner hoặc common/private cùng checksum. Các bảng dẫn xuất giữ fence `0024`; bảng mới phải có cleanup/leftover/fence cùng lúc.
- `awaiting_index`: ghi `extraction_finished_at`, để `finished_at` trống, release lease; item vẫn `metadata_only`, không active generation. Terminal state mới ghi `finished_at`. Không tự nối duplicate khác owner hoặc liên kết version/publication.
- Task/attempt sở hữu stream/temp/parser process/executor tới khi chúng settle, kể cả timeout/cancellation/shutdown. Không trả slot/đóng Session đang có thread dùng; không để child parser sống vô hạn hoặc còn descriptor sau task. Reuse giới hạn hiện có; số mới nếu cần phải config/test và là đề xuất cần đo.

## 6. Gate đóng, commit uncertain và recovery

**Hiện `recovery.gate_open` đã gọi `evaluate` gồm integrity và cutover.** Lỗ hổng cần giải quyết là proof đầu bị cũ và checkpoint chưa kiểm trong transaction; không mô tả code hiện hành là hoàn toàn thiếu cutover gate.

| Cửa sổ | Hợp đồng PR này |
| --- | --- |
| Pending/hold/journal lỗi trước claim hoặc I/O | Skip mã rõ; không claim/tăng attempt/S3/parser/derived DML/provider |
| Gate đóng trong lúc I/O, hoặc owner private bị held/disabled | Không ghi kết quả/tiến bước mới; pause, không dùng `_fail` để lách gate hoặc làm terminal failure; lease có thể hết tự nhiên |
| Lease hết/mất token, worker khác thắng | Không ghi bằng token cũ, kể cả failure/cleanup; không xóa rows/object của worker mới |
| Common revoke/epoch/cancel thắng | Không content I/O tiếp theo/derived commit; trạng thái cancel nếu còn được phép ghi hạn chế dưới gate, nếu gate đóng thì để revoke/recover/cleanup xử lý |
| Lỗi trước commit, rollback xác định | Retry/backoff theo policy chỉ qua checkpoint mới đủ proof; không kết luận từ exception chung |
| Commit đã bắt đầu rồi ném lỗi, landed hoặc rollback | Dừng attempt, trả outcome uncertain có mã; không tiếp tục stage/đọc file/mark failed/xóa object hoặc gọi lại dựa trên SELECT suy success. Giữ `commit_started`/`committed` độc lập rollback/close |
| Known commit, teardown lỗi | Giữ job/derived rows, dừng attempt; không nói rollback đã xóa mutation hoặc chạy stage kế tiếp |
| Redis/process mất sau known checkpoint | Lượt dispatch sau có thể đọc lại **trạng thái bền vững** và claim lease hợp lệ rồi resume idempotent; phân biệt với retry tự động ngay trong attempt uncertain |

Không replay journal trong worker hoặc chuyển sang engine recovery để vượt gate. Restore/safety còn pending là giới hạn thật; không thêm env/GUC/boolean production bypass. Unit/business test có thể patch **cutover-only** trong fixture để thử checkpoint; integrity/journal/hold/DB role/clock/locks vẫn thật. Các bài pending không patch gate; expectation nghiệp vụ cũ không đổi hàng loạt sang 503.

## 7. Kiểm chứng bắt buộc theo rủi ro

Dùng PostgreSQL/MinIO/journal và Redis/Celery cô lập; credentials/env/artifact ngoài repo, key trống và chặn HTTP ngoài. Đặt explicit test settings trước import để fixture không fallback `.env`. Ghi baseline container/volume ID, image và StartedAt; chỉ cleanup tài nguyên đã xác minh thuộc task. Không chạy broad suite trên dev hoặc giữ source đang bị mutant trong lúc suite khác chạy.

| Nhóm | Chứng cứ cần có |
| --- | --- |
| Runtime/entrypoints | `current_user` thật; role owner/thiếu grant bị chặn trước I/O; gọi trực tiếp và qua task đều giữ gate; năm task pending không business side effect; `/healthz` sống, `/readyz` 503 |
| Durable dispatch | Redis mất rồi trở lại vẫn xử lý job; ID-only payload; gate đổi giữa SELECT/send thì ngừng; hai worker/message trùng chỉ một claimant; retry index states không claim trong PR này |
| Exact binding | Private A/B cùng bytes không dùng nhầm rows; common/private độc lập; job/version/file/rights/epoch sai và tombstone đóng; Admin tạo job logout không tự hủy common job |
| Claim/clock | Item→job hai thứ tự race với revoke; SKIP LOCKED/lock wait; lease đúng biên, hết trong SQL cuối, stale token; retry/backoff/exhausted không vượt checkpoint |
| I/O/checkpoint | Barrier S3/parser không giữ DB connection/lock; revoke/hold/owner disable/lease replacement trong I/O chặn ghi tiếp; same-size overwrite; resource release khi exception/cancel/parser timeout |
| Extraction | Markdown body/reference và PDF text/partial/scan/bibliography-uncertain theo parser thật; locator đúng; retry idempotent; `awaiting_index` không finished/indexed/active; owner status pause rồi resume không tự enable |
| Outcome | Claim/begin/finish/failure commit landed-but-lost-ack và rollback exception; rollback/close/invalidate faults; không stage tiếp hoặc gọi `_fail` bằng proof/outcome cũ |
| Regression | PR #50 runtime index request, revoke, upload, M2 extraction/PDF/references, derived fence/cleanup, recovery/hold; migration/ORM nếu thay schema/grants |

- **Smoke tiến trình thật**: worker + Redis và dispatcher/beat, role runtime trên cấu hình override cô lập; Markdown private + PDF text + promotion chung tới `awaiting_index`, không provider calls/index rows. Test-only scoped beat schedule được phép trong harness, không production bypass; nêu task khác chưa được chuyển. HTTP pending riêng giữ gate thật. Không cần Chrome nếu không sửa web; nếu sửa web/proxy thì chạy test/typecheck/build và ca UI liên quan.
- Kiểm đối chứng ít nhất các cơ chế: runtime không owner fallback; final gate; item→job; lease/clock sau wait; post-I/O binding/cancel; SHA-256; outcome uncertain không `_fail`/stage tiếp. Kết quả phải assertion nghiệp vụ hoặc no-I/O/no-write được quan sát; observer lock wait riêng không thay kiểm dữ liệu. Lưu raw log/JUnit của cả mutant yếu/lỗi setup, không ghi đè.
- Chạy nhóm mới + regression liên quan, dựa vào call graph/rủi ro; không cần mở rộng thành hàng nghìn tích Descartes hoặc rerun mọi thứ khi không đổi source. Nếu chia tiến trình, đối chiếu selection/JUnit unique; source hashes trước/sau phải khớp, không sửa code khi suite chạy. Báo pass/fail/error/skip/not-run thật, không gọi toàn inventory xanh.
- Bốn lỗi baseline từ [review PR #50](../reviews/pr50-c2d5d1-common-index-request.md) là debt tổng hồi quy trước C4: hai corruption fixture `session_replication_role`, readiness expectation, common-original revoke helper 409. Không tính chúng đã sửa. Nếu block trực tiếp test task này thì tái hiện base/head và sửa fixture theo nghiệp vụ thật, không nới production grants/gate; ngoài liên hệ đó giữ riêng.

## 8. Hoàn thành và bàn giao

- Handoff `docs/c2d5d2a-worker-runtime-extraction.md`: engine/role/config thật, entrypoint/checkpoint đã chuyển, lock order/clock, durable state, I/O ownership, pause/uncertain semantics, code/merge SHA và giới hạn. Inventory riêng có selection/JUnit/source hashes và lỗi/not-run; không sửa số lịch sử PR #50 thành kết quả mới.
- Bàn giao **C2d-5d2b** cụ thể: `indexer._gate/_reserve/_verified_unlocked/process_index/_provider_failure/_publish`, call lease/heartbeat, owner NULL/quota/budget, pricing/model/provider allowlist, uncertainty/reuse/final hash. Liệt kê đường index đang đóng và cách PR sau thay bằng dispatch đã kiểm; không để optional flag gọi legacy indexer.
- Liệt kê discovery/reconcile/cleanup/recovery/CLI khác còn legacy và tác động của role override; không tuyên bố vận hành background hoàn chỉnh. API/Compose default cutover, private deletion/membership journal, operator/delivery/storage/backup và provider charge còn riêng.
- Cập nhật README, docs index/roadmap/decisions/testing/dev-setup và caller inventory đúng phạm vi; AGENTS chỉ trỏ handoff hoặc thêm lệnh **đã chạy**, không biến lựa chọn scoped thành luật vĩnh viễn. Kiểm links/anchors, control characters và `git diff --check`.
- Commit/push, mở PR tiếng Việt, mô tả cho chủ dự án/người review. Giữ tác giả/committer hiện có; không thêm AI author/`Co-authored-by`/trailer hoặc dòng quảng bá AI. Không merge. Báo ngắn thay đổi, kiểm chứng thực chạy, lỗi/giới hạn và phần chưa hoàn thành. **Safety vẫn pending tới C4.**
