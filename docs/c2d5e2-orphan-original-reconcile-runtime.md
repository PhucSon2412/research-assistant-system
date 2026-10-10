# C2d-5e2 — reconcile original mồ côi qua runtime

**Trạng thái: PR #54 đã merge `37edd5c9c28e60ba43425f0b1bd9f42946585e28`, tree khớp head review `cf66f1c`; chưa rerun tại merge.** Nhánh `feat/m6c2b2c2d5e2-orphan-original-reconcile-runtime` tạo từ base `b2e76edb028af2ff118d3936ed4cdf47cb88a99f` (merge PR #53, tree khớp head `bf9d81f`). Source migration `0044 → 0045`; không suy revision DB dev. **Safety pending tới C4.** Reconcile vẫn **thủ công trong môi trường quiescent**; không có Celery/beat, HTTP hoặc UI mới, không online GC. Đã giao [C2d-5f1 citation runtime](tasks/m6c2b2c2d5f1-citation-reconcile-runtime.md), chưa triển khai/kiểm; discovery harvesting tách C2d-5f2.

[Task](tasks/m6c2b2c2d5e2-orphan-original-reconcile-runtime.md) · [Inventory](c2d5e2-test-inventory.json) · [Cleanup runtime C2d-5e1](c2d5e1-revoked-common-cleanup-runtime.md) · [Upload original](c2d4b-upload-original.md) · [Approval/copy](c2d5b-review-approval-original-copy.md)

## Hai lớp bảo vệ khác nhau

- **Bảo vệ tham chiếu bằng DB (đã kiểm):** chỉ key không có **bất kỳ** hàng `source_files` nào trong toàn DB mới được fence; fence bền vững làm mọi `source_files` INSERT cùng key bị từ chối. Đây là quyền xóa duy nhất.
- **Điều kiện vận hành (không kiểm được bằng code):** không còn writer nào đang PUT/copy original hoặc commit SourceFile. Grace 24 giờ, proxy timeout, session hết hạn, Redis rỗng hay “không thấy job” **không** chứng minh SDK/DB thread hoặc transaction từ xa đã kết thúc. CLI không tự xác minh quiescence; `--apply` chỉ là lựa chọn thao tác, không phải bằng chứng hay cờ bỏ gate.

## Caller, engine và khóa

| Caller | Boundary | Ghi chú |
| --- | --- | --- |
| CLI `python -m app.cli.reconcile_originals [--apply]` | `app.ingest.orphans.reconcile_originals(get_engine(), get_storage(settings), settings, apply=…)` | Engine runtime từ `RAS_DB__*`. Mặc định **preview**. Không nhận bucket/key. Exit 0 hết pass hoặc chạm bound, 1 khi halt, 2 thiếu cấu hình. Chỉ in mã/số đếm |
| Direct | `app.ingest.orphans.reconcile_originals` | Cùng boundary; không nhận key/bucket từ caller |
| Celery/beat/HTTP | Không có | Cố ý; online GC là task riêng |
| `recover`, `operator_cleanup_revoked`, `_sweep_objects` | Không gọi orphan boundary | Đường operator/recovery giữ nguyên (kiểm: recover không gọi reconcile và không đụng orphan) |
| Lệnh cũ chạy Compose owner | **Superseded** | Owner/recovery login nay bị từ chối (`halted:worker_runtime_required`) trước mọi S3 |

Checkpoint (mỗi cái một Session ngắn mới; `runtime.short` luôn rollback + close, lỗi release → invalidate + `worker_release_unavailable`):

| Checkpoint | Thứ tự | Kết quả |
| --- | --- | --- |
| entry | `require_runtime` (login thực; cấm owner/recovery/SET ROLE/membership/DDL/TEMP/protected write gồm hai bảng mới) + EXECUTE hai routine + `require_writer` | Halt `worker_runtime_required` / `worker_runtime_unavailable` / `recovery_gate_closed` trước **mọi** S3, kể cả bucket rỗng; clock đọc ngay sau, clock hỏng → `orphan_clock_invalid` trước S3 |
| pending | Fence chưa có completion, `fenced_at` rồi ID, ≤ `orphan_max_deletes` | Resume không cần listing |
| cursor | Đọc `orphan_reconcile_cursors.start_after` | Hint; sai định dạng → `orphan_cursor_invalid` trước list |
| list | Entry mới → một trang `list_objects_v2(Prefix='originals/', StartAfter=cursor)` ngoài Session | Lỗi/403/timeout/5xx/thiếu bucket → `storage_unavailable`; trang truncated rỗng/không tăng dần/cờ sai → `storage_listing_invalid` |
| hints | Đọc `source_files`/fence của các key trong trang | Chỉ để bỏ sớm; routine vẫn là authority (kiểm khi bỏ hints) |
| HEAD | Ngoài DB; list và HEAD phải cùng size/Last-Modified (giây)/ETag | Khác → `kept:changed`; vắng → `skipped:vanished`; lỗi → halt |
| prepare | lock_timeout → clock → `ras_auth_orphan_fence` (key lock 0044 + zero reference) → flush/`SET CONSTRAINTS ALL IMMEDIATE` → journal **exclusive** (chỉ khi vừa fence, ngược lại shared) → fence/tham chiếu/completion chính xác → clock mới → commit | Proof detached (fence ID/key/size/Last-Modified/fenced_at; ETag chỉ trong bộ nhớ) |
| pre_delete | Journal shared → fence khớp proof, chưa completion, zero reference → clock mới → release sạch | `already_completed`; tham chiếu → `orphan_reference_conflict` |
| storage | HEAD (so với quan sát của fence) → DELETE → HEAD | `deleted` / `already_absent` / `kept_identity_changed`; còn object → `orphan_object_still_present` |
| finalize | `ras_auth_orphan_complete` (một completion/fence) + audit → flush/constraints → journal exclusive → fence/completion/zero reference chính xác → clock mới → commit | `already_completed` khi pass khác đã ghi |
| cursor write | Upsert hàng `id=1` → flush → journal exclusive → đọc lại → clock → commit | Chỉ khi pass không halt và cursor đổi |

Không checkpoint nào giữ Session/connection/hàng/key/journal lock khi list/HEAD/DELETE (kiểm `pool.checkedout()==0` ở mọi lời gọi storage, lấy được key/journal lock và không có `idle in transaction` khi DELETE bị chặn).

## Schema 0045

- `orphan_object_fences(id, storage_key UNIQUE, run_id, observed_size, observed_last_modified, grace_cutoff, fenced_at)`: CHECK key `^originals/[0-9a-f]{32}$`, size ≥ 0, `observed_last_modified < grace_cutoff < fenced_at`. Không nội dung, tên file, URL, ETag/checksum hay ghi chú. Trigger từ chối mọi UPDATE/DELETE/TRUNCATE kể cả owner. **Bảng riêng**, không đụng `object_delete_fences` (0044): không tombstone/item/epoch/journal revocation giả.
- `orphan_object_completions(fence_id PK→fence, outcome ∈ {deleted, already_absent, kept_identity_changed}, run_id, completed_at)`: append-only, tối đa một hàng/fence; fence giữ bất biến.
- `orphan_reconcile_cursors(id=1, start_after, updated_at)`: hint vị trí listing, DML runtime như bảng vận hành; CHECK tiền tố `originals/`, ≤ 1024 ký tự.
- `ras_auth_orphan_fence(run, key, size, last_modified, cutoff, at)` (SECURITY DEFINER, owner definer NOLOGIN, `search_path=pg_catalog,public,pg_temp`, EXECUTE chỉ runtime): NULL/key lạ/size âm/không cũ hơn cutoff/cutoff không trước `at` → lỗi; bắt buộc READ COMMITTED; `pg_advisory_xact_lock(1918989163, hashtext(key))` **cùng namespace 0044**; có bất kỳ `source_files` nào cùng key (private A/B, common pending/listed/revoked, không item, item vắng) → `referenced`, không ghi; đã fence → `already_fenced`; còn lại chèn → `fenced`. Fence có tham chiếu (chỉ khi trigger bị vượt) → lỗi.
- `ras_auth_orphan_complete(fence, key, run, outcome, at)`: NULL/outcome lạ → lỗi; fence không khớp key hoặc `at < fenced_at` → lỗi; còn tham chiếu → lỗi; `INSERT … ON CONFLICT DO NOTHING` → `completed`/`already_completed`.
- `ras_source_files_delete_fence()` (trigger INSERT của 0044) được `CREATE OR REPLACE` trong 0045 để từ chối key có **fence common hoặc orphan**, cùng key lock và luật READ COMMITTED; file 0044 không sửa. `storage_key` vẫn bất biến khi UPDATE (trigger 0001/0023), nên metadata update giữ nguyên key vẫn chạy và không thể dời key sang key đã fence.
- Grants: hai bảng fence/completion vào `PROTECTED` (`app.db_roles`) và danh sách cấm ghi của `require_runtime`; login runtime/recovery chỉ SELECT; definer SELECT/INSERT; hai routine vào `RUNTIME_ROUTINES`. Cài đã provision được 0045 cấp như 0044 (cùng cách tìm definer/login). Downgrade 0045 từ chối khi có fence/completion và khôi phục thân trigger 0044.

**Phân loại:** fence/completion là **metadata vận hành quyết định xóa object** (key + mốc kỹ thuật), không phải nội dung paper và không phải luồng xóa item của User, nên không thêm journal riêng. Chúng nằm trong DB nên đi cùng backup/restore DB. Restore snapshot cũ hơn fence sẽ mất fence: snapshot đó có thể chứa SourceFile trỏ object đã xóa sau này (file tải lỗi checksum/missing) hoặc thiếu chặn INSERT cho key đã xóa. **Không** mô tả restore là được orphan fence/journal bảo vệ toàn diện; journal độc lập, C3/C4 và rollback DB/journal đồng thời vẫn là giới hạn.

## Lock graph

- SourceFile INSERT: hàng tài nguyên (item/intent/request…) → key (trigger) → journal.
- Orphan prepare: key → (chèn fence) → journal; không khóa hàng tài nguyên nào.
- Pre-delete: journal shared, không key/hàng.
- Finalize: completion (FK KEY SHARE lên fence, fence không bao giờ UPDATE) + audit → journal; không bao giờ key.
- Cursor: hàng cursor → journal.
- Common cleanup (0044) giữ nguyên: prepare key → journal; finalize tombstone → item → hàng dẫn xuất → journal.

Không cạnh nào đi từ key/journal sang hàng, không nâng shared lên exclusive. Hai thứ tự race đã kiểm bằng lock wait thật: **INSERT trước** (giữ key tới commit) → prepare chờ rồi thấy tham chiếu → `kept:referenced`, không fence; INSERT rollback → fence/xóa. **Prepare trước** → INSERT (kể cả đang khóa item của nó) chờ key rồi bị từ chối `object key is fenced for deletion`. Va chạm `hashtext` chỉ serialize bảo thủ hai key; fence/so sánh luôn theo chuỗi key đầy đủ (test cặp `md5` va chạm thật).

## Candidate, grace và clock

- Chỉ bucket cấu hình và tiền tố `originals/`; chỉ key đúng dạng upload/approval mint (`originals/` + 32 hex thường). Key khác dưới tiền tố (`originals/crash-orphan`, chữ hoa, đuôi, thư mục con) → `kept:foreign_key`, không HEAD/xóa. Tiền tố khác không được list.
- Mốc tuổi là **Last-Modified tính bằng giây UTC** (như HEAD trả và fence lưu); listing có phần lẻ giây chỉ là hint được cắt về giây. Đủ điều kiện **khi và chỉ khi** `last_modified < now − orphan_grace` (đúng biên → giữ). Clock ứng dụng phải có timezone; mọi so sánh theo UTC; clock lùi so với lần đọc trước trong pass (kể cả sau SQL/lock wait cuối) → `orphan_clock_invalid`, prepare không commit.
- Grace là điều kiện bổ sung, **không** là bằng chứng absence/quiescence. Object ghi lại giữa list và HEAD → `kept:changed`; ghi lại sau fence (HEAD khác quan sát của fence) → completion `kept_identity_changed`, object giữ, fence vẫn chặn INSERT. HEAD/ETag không chứng minh bytes bất biến; adapter không có delete có điều kiện. Ghi đè cùng key cùng size trong cùng giây giữa HEAD và DELETE (chỉ có thể khi không quiescent) không phát hiện được: giới hạn được ghi, reconcile không PUT/copy/recreate.

## Bound, cursor, resume

`files.orphan_list_page_size` 500 (1–1000), `files.orphan_max_pages` 20 (1–1000), `files.orphan_max_deletes` 100 (1–10.000): giá trị khởi đầu, có validation/test, **chưa đo**. Mỗi pass: resume fence dở trước (tính vào `orphan_max_deletes`), rồi list từ cursor. Chạm bound → `bound:deletes`/`bound:pages`, cursor = key cuối đã xử lý; hết listing → cursor về NULL (wrap). Trang toàn file được giữ không chặn các trang sau (kiểm 4 pass liên tiếp). Pass halt không ghi cursor (lượt sau list lại, idempotent). Hai pass đồng thời có thể ghi đè cursor của nhau: chỉ là hint. Preview không ghi cursor.

Kết quả (`dict` mã → số): `deleted`, `already_absent`, `kept:identity_changed`, `already_completed` (pass khác ghi completion), `kept:referenced|recent|foreign_key|fenced|metadata_invalid|changed`, `skipped:vanished|busy`, `listed`, `pending_fences`/`would_reconcile` (preview), `bound:*`, `halted:<mã>`. Audit `original.orphan_reconcile` chỉ khi `completed`: target fence ID, `object_outcome`, `run_id`; không key/tên file/nội dung.

## Outcome

| Cửa sổ | Hành vi đã kiểm |
| --- | --- |
| Prepare commit landed/rollback rồi exception | `halted:worker_commit_uncertain`; không S3 sau prepare, không candidate kế; fence landed chặn INSERT muộn; lượt độc lập resume/list lại rồi xóa |
| Release lỗi sau known commit (prepare/finalize) | `halted:worker_release_unavailable`; mutation giữ; prepare không S3; finalize không báo success; lượt sau hoàn tất |
| HEAD/DELETE/verify 403/5xx/timeout | `halted:storage_unavailable`, không bao giờ là vắng; fence giữ; lượt sau `deleted` hoặc `already_absent` |
| Object vắng thật trước fence / sau admission | `skipped:vanished` / `already_absent` |
| Final commit landed/rollback rồi exception | Halt, không `deleted`; landed → không còn pending; không landed → lượt sau resume không cần listing → `already_absent` |
| Gate đóng sau prepare, trước pre_delete | Không DELETE; fence giữ; lượt sau mở gate → `deleted` |
| Gate đóng sau admission (HEAD/DELETE) | Delete có thể xong, không dựng lại; finalize halt; candidate kế không được chạm; lượt sau `already_absent` |
| Gate đóng trong finalize | Halt, không completion/audit |
| Tham chiếu xuất hiện trên key đã fence (chỉ khi owner tắt trigger) | `halted:orphan_reference_conflict` trước DELETE |
| Hai pass cùng orphan | Hai DELETE (at least once), một completion/audit, một `deleted` + một `already_completed` |
| Process SIGKILL giữa fence commit và DELETE (Linux container) | Fence bền, object còn; process mới resume và xóa; lần chạy thứ ba không đổi gì |

Fault injection trong process khác với kill process thật; không có bằng chứng power-loss. Lock timeout ở prepare → `skipped:busy` (thiết kế, **chưa có test riêng**).

## Runbook quiescence (điều kiện vận hành hiện hành)

1. Dừng tiếp nhận upload/approval và mọi writer có thể PUT/copy original hoặc commit SourceFile (API, worker, script, client ngoài project/container nếu có).
2. Chờ mọi attempt, SDK/DB thread và transaction cũ settle. Dừng process/container **không** chứng minh PUT/commit từ xa đã xong: kiểm phía DB (không còn session/transaction của writer) và phía object store. Không chứng minh được → **không** chạy `--apply`.
3. Gate phải mở theo cách hợp lệ. Quiescence **không** phải recovery hold: pending/hold/journal lỗi vẫn chặn reconcile; không dùng `recover`, owner fallback, `skip_gate` hay cutover test để vượt. Trước C4, gate production luôn `auth_safety_pending`, nên lệnh chỉ chạy được trong môi trường thử có fixture cutover scoped.
4. Chạy bằng login runtime đã provision: `python -m app.cli.reconcile_originals` (preview) rồi `python -m app.cli.reconcile_originals --apply`; lặp lại tới khi không còn `bound:*`/`halted:*`.

Lệnh đã thực chạy chỉ trong môi trường cô lập: smoke container Linux (`docker run … rasc2d5e2-cli:20261010 python -m app.cli.reconcile_originals --apply` với login runtime) và CLI `main([...])` trong test. **Không** chạy trên stack dev; lệnh cũ `docker compose exec api python -m app.cli.reconcile_originals` (owner) là superseded.

## Tương thích

- Upload: success known giữ key và vẫn tải qua `GET /files/{id}/original`; orphan do lỗi put/DB bị dọn sau grace (test C2d-4b/M1c chuyển sang `--apply`/clock boundary và key đúng dạng mint).
- Approval: commit uncertain landed → bản sao có SourceFile, giữ; không landed → journal đi trước DB, gate đóng nên reconcile halt không I/O tới khi operator `recover`, sau đó bản sao mồ côi bị xóa, original private giữ.
- Common cleanup 0044 / `operator_cleanup_revoked` / `_sweep_objects` / recover: đường riêng, hồi quy theo nhóm seal. Fence common vẫn chặn INSERT sau khi trigger được thay.
- Upload/approval mint key ngẫu nhiên nên không bao giờ trùng key đã fence; fence không hợp thức hóa việc chạy khi upload còn bay.

## Còn lại / giới hạn

- Online GC, lịch beat, lifecycle bền cho upload/approval đang bay, abort multipart (multipart dở không xuất hiện trong list) — task riêng.
- Gọi routine trực tiếp bằng runtime (không qua gate Python) chỉ có thể fence key **không** có tham chiếu; hệ quả duy nhất là key đó không nhận SourceFile mới.
- SET ROLE với membership không kiểm bằng test vì role là cấp cluster dùng chung; `require_runtime` vẫn từ chối `current_user <> session_user`/membership.
- Cấu hình bound/page chưa đo; không SLO. Không exactly-once S3, không rollback S3 cùng DB.
- Discovery/citation là bước tiếp theo; search/query/rerank/RAG, C3/C4 riêng. **Safety pending tới C4.**

## Kiểm chứng

Seal cuối `sealed-2` (`scripts/c2d5e2_seal.py`) tại `9faf0d1`; production code (`api/app`, migration, scripts) giống hệt `ebbc27a`, commit sau chỉ dời barrier của `test_two_concurrent_passes_one_completion`. **2.096 API unique pass**, 0 fail/error/skip, exit 0; collect 4.690, **2.594 not-run**; mọi nhóm `selection_matches`/`selection_in_collection`; 321 source hash (`api/`, `scripts/`) trước/sau trùng.

| Nhóm | Pass |
| --- | ---: |
| Smoke CLI Linux (SIGKILL giữa fence và DELETE, resume, idempotent) | 1 |
| Orphan reconcile runtime mới | 120 |
| Cleanup runtime C2d-5e1 (trigger INSERT được thay) | 81 |
| Cleanup/publication cũ (operator có nhãn) | 23 |
| Schema: migrations/ORM, 0045, 0044, 0040, 0039, auth foundation, config | 248 |
| Revoke HTTP + migration 0042 | 172 |
| Upload/approval/download (SourceFile INSERT, reconcile cũ chuyển runtime) | 841 |
| Recovery/backup (phần không phải debt) | 15 |
| Document/common index, extraction | 233 |
| HTTP index request + publish | 362 |

**Countercheck 9/9** bắt assertion nghiệp vụ tại `ebbc27a`, chạy tuần tự trước seal, source khôi phục byte/hash sau mỗi mutant: bỏ kiểm login runtime ở entry, bỏ gate entry, bỏ zero-reference trong routine, bỏ fence orphan khỏi trigger INSERT, bỏ key lock của routine, đi tiếp sau commit uncertain, đi tiếp sau release lỗi, coi 403 là vắng, bỏ final gate của finalize.

**Debt base (25 node, deselect, not-run, không sửa/không tính pass):** 24 node mang từ PR #53 và 1 node mới lộ vì selection này thêm `test_c2d4c_original_downloads.py`: `test_audit_resource_locks_queue_writer_response_proof_sees_winner[common]` fail giống hệt (`revocation_unavailable`) tại base `b2e76ed` trong worktree cô lập. Bốn debt PR #50 nằm ngoài selection.

**Lượt không tính:** chuẩn bị p1–p11 (biên grace dùng phần lẻ giây, clock đọc sau list, tên biến che module, debt c2d4c) và `sealed-1` (exit 1: barrier ở listing cho phép một pass commit fence trước khi pass kia đọc hint, pass còn lại chờ barrier DELETE tới timeout; hành vi sản phẩm đúng, đã dời barrier sang HEAD đầu, 10/10 lần chạy lại đạt). Chi tiết, log và SHA-256 artifact trong [inventory](c2d5e2-test-inventory.json); artifact ngoài repo ở `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5e2-20261010`.

**Không chạy:** web/Chrome/npm (không đổi web/proxy), smoke worker C2d-5e1 (tag image đã gỡ, đường worker cleanup không đổi), drill backup/restore, toàn suite ngoài selection, provider/arXiv/charge/SLO, nhánh `skipped:busy`, SET ROLE qua membership, stack dev.

**Tài nguyên:** project cô lập `rasc2d5e282556039` (3 container, 3 volume ẩn danh, network) đã gỡ sau khi kiểm label/consumer; tag `rasc2d5e2-cli:20261010` đã gỡ (image gốc giữ). Container/volume/network/image có sẵn trùng baseline; `ras-worker-1`/`ras-beat-1` đã `Restarting` từ baseline và RestartCount đổi 736→901 mà task không chạm, không suy nguyên nhân. Quét secret UTF-8/UTF-16 sau dọn: 0 hit.
