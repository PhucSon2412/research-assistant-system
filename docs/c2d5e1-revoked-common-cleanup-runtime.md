# C2d-5e1 — cleanup bản chung đã thu hồi qua runtime

**PR #53 đã merge `b2e76edb`**, tree khớp head `bf9d81f`; kết quả dưới đây theo source/lượt trước merge, chưa rerun merge. Source `0044`, safety pending tới C4. Bước kế tiếp đã giao [C2d-5e2 orphan reconcile runtime](tasks/m6c2b2c2d5e2-orphan-original-reconcile-runtime.md): thủ công/quiescent, orphan fence riêng, không nới common tombstone authority hoặc đổi operator recovery. C2d-5e2 nay đã triển khai trên nhánh review, chưa merge: [handoff](c2d5e2-orphan-original-reconcile-runtime.md) (fence orphan bảng riêng, trigger INSERT 0044 được thay thân hàm trong 0045 để kiểm cả hai loại fence).

**Review lại tại `bf9d81f`, code `9139c01`: P2 đã sửa, không thấy điểm chặn merge mới trong phạm vi PR #53.** Reviewer thực chạy **20/20**; artifact agent **1.584 unique pass / 2.977 not-run**, 61 artifact hash và 311 source hash khớp, 9 countercheck đối chiếu riêng. [Report/evidence/giới hạn](reviews/pr53-c2d5e1-revoked-common-cleanup-runtime.md). PR OPEN/chưa merge, source `0044`, **safety pending tới C4**. Các mốc review trước sửa bên dưới giữ lịch sử; operator C3, baseline debt và consumer còn lại chưa chứng nhận.

**Sửa review PR #53 tại `9139c01` (chờ review lại, chưa merge):** cleanup operator/sweep chỉ nhận login có bằng chứng dương — owner/migrator (superuser hoặc owner `deletion_tombstones`) hoặc recovery đã provision (EXECUTE `ras_auth_restrict` + INSERT journal mirror, **không** EXECUTE `ras_auth_session_create`); probe/release lỗi → `operator_identity_unavailable`, không chứng minh → `operator_cleanup_requires_operator`, đều trước mutation/S3. P2 tái hiện trên `3c4c165` (6 fail) trước sửa. Seal `sealed-fix-1`: **1584 API unique pass**, exit 0, collect 4.561, **2.977 not-run** theo identity chính xác (ID UUID1 đã ổn định; smoke Linux giữ kết quả lịch sử `3b54a6f`); 9/9 countercheck gồm catch-all cũ. Lượt `sealed-3` cũ giữ exit 1, not-run raw 2.978/logic 2.976. Safety pending tới C4.

**Review PR #53 tại `3c4c165` (2026-10-10): có P2 chặn merge.** [Report](reviews/pr53-c2d5e1-revoked-common-cleanup-runtime.md) · [prompt sửa](tasks/fix-pr53-operator-cleanup-identity-and-seal.md). Runtime guard lỗi/profile sai được helper operator cho qua; reviewer tái hiện object bị xóa và purge khi safety pending. Mô tả từ chối runtime bên dưới chỉ đúng khi runtime guard thành công, chưa bảo đảm fail closed. Reviewer 101 API pass; hai regression mới fail đúng P2. Artifact 1.575 pass kiểm riêng; not-run logic 2.976 thay cho raw 2.978 vì hai UUID1 đổi ID; giữ seal exit 1/source lịch sử, chưa sửa inventory agent. PR OPEN/chưa merge; safety pending tới C4.

[Task](tasks/m6c2b2c2d5e1-revoked-common-cleanup-runtime.md) · [Inventory](c2d5e1-test-inventory.json) · [Document runtime](c2d5d2b-document-index-runtime.md) · [Revoke runtime](c2d5c2-common-revoke.md)

Base đã fetch: `origin/main = ba20fda402840243f13bd4bb27e98c4c0cb17562` (merge PR #52; head `97bc6e7` là ancestor, tree khớp). Nhánh `feat/m6c2b2c2d5e1-revoked-common-cleanup-runtime`; commit `6254a8a` giữ riêng toàn bộ docs orchestrator/review có sẵn. Source migration `0043 → 0044`; không suy revision DB dev. **Safety pending tới C4**, không mở pricing/cutover/provider thật.

## Call, engine và khóa

| Caller | Boundary sau PR | Ghi chú |
| --- | --- | --- |
| Beat/Celery `ras.revocation.cleanup` | `_gate` đầu task → `app.ingest.cleanup.cleanup_revoked(get_engine(), storage, settings)` | Engine runtime từ `RAS_DB__*`; mỗi checkpoint tự kiểm lại login/gate |
| CLI `python -m app.cli.cleanup_revoked` | Cùng `cleanup_revoked` runtime | Exit 0 hết batch, 1 khi checkpoint halt, 2 thiếu cấu hình; chỉ in mã/số đếm |
| Direct runtime | `app.ingest.cleanup.cleanup_revoked` | Không nhận ID/key từ caller; quyền là tombstone common đã journal xác nhận |
| Recovery `recover` | `recovery._cleanup` → `publication.operator_cleanup_revoked`; `_sweep_objects` giữ nguyên | Đường operator riêng, chạy khi gate đóng và traffic/writer dừng; chỉ nhận login có **bằng chứng dương** owner/recovery (sửa review PR #53) |
| Test system/adversary cũ | `publication.operator_cleanup_revoked` với engine owner, đã gắn nhãn | Không tính là runtime pass |

Checkpoint runtime (mỗi cái một Session ngắn, rollback + close luôn được thử, release lỗi → invalidate + `worker_release_unavailable`):

| Checkpoint | Thứ tự khóa / bước | Kết quả |
| --- | --- | --- |
| entry | `require_runtime` (login thực, không owner/recovery/membership/DDL/TEMP/protected write, gồm `object_delete_fences`) + EXECUTE `ras_auth_cleanup_fence` + `require_writer` | Halt `worker_runtime_required` / `recovery_gate_closed` |
| candidates | Đọc ID tombstone `common_copy` chưa purge, `purge_due_at` rồi ID, `cleanup_batch_size` | Chỉ gợi ý, không phải proof |
| prepare | Đọc scalar tombstone/journal/item **không khóa hàng** → kiểm `derived_rows_shared` → routine (key lock) → flush/constraints → journal exclusive (chỉ khi vừa tạo fence, ngược lại shared) → đọc lại binding/fence/tham chiếu → clock mới → commit | `delete` / `shared_kept` / `no_object` hoặc từ chối có mã |
| pre_delete | Journal shared → tombstone/target/fence/tham chiếu chính xác → clock mới → release sạch | Proof mới cho S3 |
| S3 | HEAD → DELETE → HEAD trên key đã authorize, ngoài mọi Session/connection/khóa | `deleted` / `already_absent` / `storage_unavailable` / `object_still_present` |
| finalize | Tombstone `FOR UPDATE SKIP LOCKED` → item `FOR UPDATE` → DML dẫn xuất/job/call/membership/metadata (khóa hàng ngầm) → audit → flush + `SET CONSTRAINTS ALL IMMEDIATE` → journal exclusive → hậu điều kiện → clock mới → commit | Một lần purge/audit cho tombstone |
| error | Tombstone `FOR UPDATE SKIP LOCKED` → `cleanup_attempts+1`/`last_cleanup_*` → flush → journal exclusive → kiểm lại → clock → commit | Chỉ sau từ chối nghiệp vụ known + release sạch |

Không checkpoint nào lấy khóa hàng/key mới sau journal, không mở connection thứ hai trong transaction, không khóa User để ghi audit system. Clock đọc sau SQL cuối và lần chờ journal; vì cleanup không có lease/TTL, kiểm clock là sàn: thời điểm ghi không trước `recorded_at` của tombstone và clock sau journal không lùi so với mốc đã ghi (`cleanup_clock_invalid`).

## Fence bền vững theo key (0044)

`object_delete_fences(id, storage_key UNIQUE, tombstone_id, item_id, source_file_id, rights_epoch, fenced_at)`; không FK (như tombstone), không nội dung/tên file/URL/ghi chú. Trigger từ chối mọi UPDATE/DELETE/TRUNCATE, kể cả owner. Fence là **metadata quyết định xóa**: không bị xóa cùng dẫn xuất, giữ sau `purged_at`, downgrade 0044 từ chối khi có fence.

Chỉ `ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint)` ghi fence (SECURITY DEFINER, owner definer NOLOGIN, `search_path=pg_catalog,public,pg_temp`, EXECUTE chỉ cho runtime, không PUBLIC/recovery). Routine: NULL/rỗng → lỗi; bắt buộc READ COMMITTED; lấy `pg_advisory_xact_lock(1918989163, hashtext(key))`; tombstone đúng id/kind/item/file/key/epoch, chưa purge, có bản ghi journal `revocation` đã confirmed; item còn tồn tại phải `common` + `revoked` + đúng epoch; file còn tồn tại phải đúng key/item. Mọi `source_files` cùng key mà item vắng, không có item hoặc item không `revoked` (private, common pending/listed) là tham chiếu được bảo vệ → trả `shared_kept`, không tạo quyền xóa. Không còn → `fenced` (chèn) hoặc `already_fenced`. Runtime gọi routine trực tiếp không qua gate Python chỉ có thể fence key không còn tham chiếu hợp lệ của tombstone đã xác nhận; hệ quả duy nhất là key đó không nhận SourceFile mới.

Trigger `BEFORE INSERT` trên `source_files` (invoker, mọi writer gồm SQL thô và owner) lấy **cùng** key lock, từ chối nếu key đã fence và từ chối transaction không phải READ COMMITTED (snapshot cũ có thể không thấy fence vừa commit). Upload/approval mint key mới nên không bị ảnh hưởng; cập nhật metadata SourceFile không đổi key giữ hợp đồng cũ. `storage_key` vẫn không unique; checksum trùng không là căn cứ xóa.

Lock graph: inserter `hàng → key → journal`; prepare `key → journal` (không giữ hàng nào); finalize `tombstone → item → hàng dẫn xuất → journal`, không bao giờ key. Không cạnh nào đi từ key/journal sang hàng, nên không có chu trình key↔item. Hai thứ tự race:

- **Insert trước:** INSERT giữ key lock tới commit; routine chờ, sau đó statement mới thấy SourceFile đã commit → `shared_kept`, object giữ. Insert rollback → không còn tham chiếu → fence.
- **Prepare trước:** prepare giữ key lock tới commit fence; INSERT chờ rồi thấy fence → bị từ chối trước commit, kể cả khi nó đang giữ khóa item bản thu hồi.

Va chạm `hashtext` chỉ serialize hai key bảo thủ; fence/so sánh luôn theo chuỗi key đầy đủ (test tìm cặp va chạm thật).

## S3 và finalization

Fence chỉ authorize delete sau known commit + release sạch và proof pre-delete mới. Prepare commit uncertain hoặc release lỗi → dừng, **không S3** trong attempt đó; tick độc lập sau đọc fence bền vững (`already_fenced`) và tiếp tục. Gate đóng trước pre-delete → không bắt đầu delete; gate đóng sau pre-delete → delete đã admit có thể hoàn tất, không lấy lại được, finalize bị từ chối cho tới khi gate mở (hoặc operator recover). Hai worker cùng key có thể cùng delete (at-least-once); finalize `SKIP LOCKED` + kiểm `purged_at` bảo đảm một purge/audit. Không PUT/copy/tạo lại original.

Finalize xóa theo thứ tự FK như cũ (con trỏ active/status → citation link → reference → index entry → scrub vector/chunk hash của call → generation → chunk → section → PDF page → candidate), kết thúc job dở (lease xóa), xóa membership, scrub metadata không có căn cứ đúng trường. Giữ item/version/file/rights/publication/request/tombstone/job/ledger/retention. `derived_rows_shared` được kiểm ở prepare (trước fence/S3) và lại trong finalize. `purged_at` chỉ khi: object đã xác minh vắng với fence còn đó và không tham chiếu được bảo vệ; hoặc `shared_kept` với tham chiếu được bảo vệ còn thấy ở finalize (biến mất → `shared_reference_changed`, retry sẽ fence/xóa); hoặc tombstone không có key.

## Outcome

| Cửa sổ | Hành vi đã kiểm |
| --- | --- |
| Prepare commit landed/rollback rồi exception | `halted:worker_commit_uncertain`; không S3, không error writer; lượt sau xóa |
| Release lỗi sau known commit (prepare/finalize) | `halted:worker_release_unavailable`; mutation giữ; prepare không S3; finalize không đếm success |
| S3 403/5xx/timeout ở HEAD hoặc verify | `error:storage_unavailable`; nội dung DB không bị scrub; object có thể đã mất; fence giữ; lượt sau `deleted`/`already_absent` |
| Object vắng thật | `already_absent` |
| DB từ chối purge (0023/0024) hoặc từ chối nghiệp vụ sau delete | Rollback nội dung, `error:<mã>`; fence giữ; original không dựng lại |
| Final commit landed/rollback rồi exception | `halted:worker_commit_uncertain`; không success/đếm purged/SELECT suy luận/error writer; batch dừng, tombstone sau không đụng |
| Error checkpoint commit lỗi | `halted:worker_commit_uncertain`, không ghi lại |
| Tham chiếu được bảo vệ xuất hiện sau pre-delete (chỉ khi bỏ qua trigger) | `halted:cleanup_reference_conflict`, không purge |
| Lock timeout khi khóa tombstone/item/key | Thiết kế: `skipped:cleanup_busy`, không tính attempt; tick sau thử lại. **Chưa có test riêng** cho nhánh này |
| Hai cleanup đồng thời | Hai delete, một `deleted`, một audit, attempts 1 |

**Thay đổi so với hợp đồng cũ (ghi rõ):** trước đây lỗi DB sau khi xóa object rollback toàn bộ giao dịch; nay fence (metadata vận hành) đã commit trước S3 nên **còn lại** sau lỗi. Không nói toàn DB rollback như chưa từng prepare. Lỗi storage vẫn không scrub nội dung DB.

## Kế toán muộn

Cleanup giữ nonce/job token/day/ceiling/token/model/provider/cost của `embedding_calls`, chỉ xóa vector + chunk hash (`content_removed_at`). Kiểm: provider trả kết quả sau khi runtime revoke + cleanup đã purge → settle `completed` một lần trên ngày gốc, `reserved_usd` của bucket về 0, không vector/hash/index, không active generation. Late reap/settle hai thứ tự sau cleanup: reap giữ ceiling (`uncertain`), settle đúng một lần, settle lặp không trừ lần hai. Cleanup không đổi reservation thành rejected và không giải tiền.

## Recovery

`recover` vẫn gọi đường operator (`operator_cleanup_revoked`, S3 trong transaction như cũ, chỉ an toàn khi API/worker/beat dừng) và `_sweep_objects` (xóa object của mọi tombstone trong journal, kể cả đã `purged_at`, nếu không còn tham chiếu không-revoked). Runtime cleanup không import/gọi nhánh này. **Sau sửa review PR #53:** `operator_cleanup_revoked` và `_sweep_objects` chỉ chạy khi chính connection/login thực (`current_user = session_user`, không SET ROLE) chứng minh dương một trong hai: (a) owner/migrator — superuser hoặc owner bảng `deletion_tombstones`; (b) login recovery đã provision — EXECUTE `ras_auth_restrict` **và** INSERT `revocation_journal_records` **và không** EXECUTE `ras_auth_session_create` (năng lực chỉ runtime có, theo `app.db_roles`). Routine/bảng thiếu được coi là không chứng minh. Probe lỗi hoặc rollback/close lỗi → `operator_identity_unavailable`; không chứng minh → `operator_cleanup_requires_operator`; cả hai dừng trước khóa hàng/mutation/S3 (`recover` đổi thành `RecoveryError` có mã). Không suy operator từ runtime guard thất bại, tên role khai báo, GUC hay cờ bypass. Bản trước dùng `except runtime.Halt: return` nên runtime bị cấp sai TEMP/protected write/thiếu grant hoặc probe lỗi vẫn xóa object và purge — đã tái hiện trên code `3c4c165` (6 ca fail) trước khi sửa. Kiểm: hold → runtime halt; recover bằng login recovery purge tombstone, object restore quay lại bị sweep, recover lặp lại không xóa thêm; gate cuối `auth_safety_pending`, không ready. Đây là kiểm năng lực theo provisioning hiện có, **không** phải chứng nhận danh tính operator đầy đủ (C3): login được cấp sai đúng bộ quyền recovery mà thiếu EXECUTE session_create sẽ được nhận như recovery.

Phân loại restore: fence nằm trong DB nên đi cùng backup/restore DB. Restore về trước khi có fence → recover/operator dọn lại theo journal; pg_dump tạo trigger ở post-data nên pg_restore không bị trigger fence chặn (lập luận từ thứ tự pg_dump, **chưa** chạy drill restore trong PR này). Journal không thêm trường mới. Giới hạn journal/DB cùng rollback và lưu trữ journal production giữ nguyên như M6c.

## Cấu hình hiện hành (chưa là SLO)

`cleanup_batch_size` 50, `cleanup_poll_seconds` 900, `deleted_cleanup_deadline` 24 h, storage connect 10 s/read 30 s/`max_attempts` 2, `gate_lock_timeout_ms` 2000 (lock_timeout mọi checkpoint), `cleanup_passes` 5 (recover). Lấy từ config hiện có, không đổi; chưa đo hiệu năng.

## Caller còn legacy / chưa làm

- `publication.operator_cleanup_revoked`, `_purge_one`, `recovery._sweep_objects`: đường operator; chuyển identity/operator riêng là C3.
- `app.cli.reconcile_originals` (C2d-5e2 trên nhánh review), discovery/OAI, citation reconcile, private deletion/membership journal: task riêng.
- `scripts/drill_backup_restore.py` gọi CLI cleanup với Compose mặc định (owner) — nay CLI từ chối owner (exit 1). Drill và hai script demo/smoke lịch sử (`demo_m6b2b2b_common_index.py`, `smoke_m6b2a_publish_browser.py`, đổi import sang operator) **không chạy lại**.
- Query/rerank/search/RAG runtime, mặc định Compose/API cutover, C3/C4: riêng. Bốn baseline debt PR #50 không thuộc selection và không được gọi đã sửa.

## Giới hạn

Không exactly-once S3, không rollback object cùng DB, không bằng chứng power-loss. SIGKILL thực chỉ kiểm giữa fence commit và delete trong smoke Linux; các cửa sổ khác là fault injection trong process. Transaction không READ COMMITTED bị từ chối thay vì hỗ trợ. Web/proxy không đổi nên không chạy Chrome/npm.

## Kiểm chứng

Seal cuối `sealed-3` tại `3b54a6f` (production code từ `6b1adc5`, test cuối `2afe1b8`; `3b54a6f` chỉ sửa đường dẫn deselect của seal): **1575 API unique pass**, 0 fail/error/skip; collect 4.551, raw **2.978 not-run**; theo identity logic là **2.976** vì hai node `test_bad_values_no_sql_unknown[generation-<uuid1>-…]` đổi ID giữa collect và chạy (số raw giữ lịch sử, đã sửa ID ổn định ở lượt sau). 311 source hashes (`api/`, `scripts/`) trước/sau trùng.

| Nhóm | Pass |
| --- | ---: |
| Smoke worker+beat Linux (SIGKILL giữa fence và delete) | 1 |
| Cleanup runtime mới | 71 |
| Cleanup/publication cũ (operator có nhãn) | 23 |
| Revoke HTTP + migration 0042 | 172 |
| Document/common index, extraction, common index cũ | 233 |
| HTTP index request + publish | 362 |
| Upload/approval (SourceFile INSERT) | 512 |
| Recovery/backup (phần không phải debt) | 15 |
| ORM/migration gồm 0044, provision/auth foundation | 186 |

Seal script trả exit 1 chỉ vì nhóm schema có 2 node tham số hóa mang uuid1 sinh lúc collect (ID collect-only khác lượt chạy); 186/186 đạt và hai tập trùng sau chuẩn hóa UUID. Ghi nguyên trạng, không chạy lại để lấy exit 0.

**Debt base giữ riêng (24 node):** 14 fail + 9 error ở `test_recovery.py`/`test_backup.py`/`test_auth_safety_foundation.py` và 1 node `test_c2d4b_upload_original.py::test_worker_advance_and_intent_expiry_before_response_keep_receipt` fail y hệt ở base `ba20fda` khi chạy lại cô lập; deselect tường minh, nằm trong not-run, không sửa/không tính pass. Các dòng cleanup tôi chuyển sang runtime trong `test_recovery.py` đều chạy qua trước điểm fail có sẵn.

**Countercheck:** 8/8 cơ chế bắt assertion nghiệp vụ tại `2afe1b8` (role guard, gate cuối finalize, trigger INSERT fence, bảo vệ tham chiếu, giữ ledger/nonce, hậu điều kiện tham chiếu, commit uncertain, release lỗi); source khôi phục byte/hash sau mỗi mutant, chạy tuần tự trước seal. Lượt đầu, mutant gate cuối **không bị bắt** (test cũ đóng gate trước entry của finalize) — giữ là lượt yếu, đã thêm test đóng gate bên trong finalize.

**Lượt không tính:** chuẩn bị p1–p11 (lỗi fixture, beat nhận thiếu storage env, danh sách bảng mong đợi), `sealed-1` (4 test `test_common_index` gọi helper operator bằng engine runtime — đúng là đường vòng guard mới chặn, đã chuyển sang boundary runtime — cùng 1 debt upload), `sealed-2` (deselect dùng tiền tố `api/tests/` không khớp nodeid). Chi tiết, log và SHA-256 artifact trong [inventory](c2d5e1-test-inventory.json); artifact ngoài repo ở `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5e1-20261010`.

## Sửa review PR #53

Code `9139c01` (sau commit docs reviewer `4fa774f`): `publication._require_operator` thay `except runtime.Halt: return` bằng truy vấn bằng chứng dương (mục Recovery ở trên); Session riêng luôn rollback + close, lỗi → invalidate + `operator_identity_unavailable`. `_sweep_objects` dùng cùng guard. ID tham số `test_bad_values_no_sql_unknown[generation-uuid1-…]` cố định; seal thêm `selection_in_collection` và tùy chọn `--skip` có ghi lý do.

| Kiểm | Kết quả |
| --- | --- |
| Tái hiện trên `3c4c165` | 6 regression fail (`DID NOT RAISE`): runtime +TEMP, +protected write, thiếu grant, bắt chước recovery, guard runtime lỗi, sweep; 2 control đạt |
| Sau sửa, chuẩn bị | 12/12 operator/sweep, 97/97 file cleanup runtime + test UUID1; hai lượt collect-only cho ID giống hệt |
| Countercheck | 9/9 bắt assertion, gồm `operator-catchall` (tái lập catch-all → snapshot/S3 assertion fail) |
| `sealed-fix-1` | **1584 unique pass**, exit 0; collect 4.561, 2.977 not-run; 311 source hash trùng; nhóm: cleanup-legacy 23, cleanup-runtime 81, revoke 172, index 233, request-publish 362, upload-approval 512, recovery 15, schema 186 |

Smoke worker+beat Linux không chạy lại (đường worker/runtime cleanup không đổi; tag image thử đã gỡ) — kết quả 1 pass tại `3b54a6f` giữ là lịch sử. 24 debt base không rerun ở base trong lượt này. Artifact: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr53fix`.
