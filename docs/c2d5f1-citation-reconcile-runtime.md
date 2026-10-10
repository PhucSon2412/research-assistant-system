# C2d-5f1 — citation reconcile qua runtime

**Trạng thái: nhánh `feat/m6c2b2c2d5f1-citation-reconcile-runtime`, PR chưa merge.** Base `origin/main` = merge PR #54 `37edd5c9c28e60ba43425f0b1bd9f42946585e28` (tree khớp head review `cf66f1c`, đã fetch/xác minh lại trước khi tạo nhánh). Source migration **giữ `0045`**: không migration, không đổi grants/provision. **Safety pending tới C4.** Discovery harvesting (`run_sync`: engine/gate/HTTP/pacing/lease/cursor/outcome) **chưa chuyển**, là C2d-5f2.

[Task](tasks/m6c2b2c2d5f1-citation-reconcile-runtime.md) · [Inventory](c2d5f1-test-inventory.json) · [Worker/extraction C2d-5d2a](c2d5d2a-worker-runtime-extraction.md) · [Cleanup C2d-5e1](c2d5e1-revoked-common-cleanup-runtime.md) · [Orphan C2d-5e2](c2d5e2-orphan-original-reconcile-runtime.md)

## Ngữ nghĩa không đổi

Link chỉ nghĩa “paper trích dẫn liệt kê công trình này trong bibliography”: không là evidence, không vào keyword/vector index, embedding/rerank/RAG payload. Matcher `arxiv-exact-v2`, ranh giới ID/host/DOI, citation version tách latest metadata version, hàng stale (hash/matcher) bị ẩn khi đọc, OAI tombstone ẩn đích — giữ nguyên. Không title/fuzzy matching, không fetch URL lấy từ citation, không network/storage/provider I/O. API owner/private/common gate, reverse lookup chỉ private của caller và final caller/read boundary không đổi. Không thêm journal cho bibliography/quan hệ trích dẫn (dữ liệu dẫn xuất).

## Caller, engine và boundary

| Caller | Boundary sau C2d-5f1 | Ghi chú |
| --- | --- | --- |
| CLI `python -m app.cli.reconcile_citations` | `app.citations.reconcile_pending(get_engine(), settings.citations)` | Engine runtime từ `RAS_DB__*`. In mã/số đếm, không citation/title/URL/credential. Exit **0** khi chạy hết/chạm bound/`busy:*`, **1** khi `halted:*`, **2** thiếu cấu hình. Không preview: đây là metadata dẫn xuất, không xóa object |
| Celery `ras.citations.reconcile` (beat `citation-link-reconcile`) | `_gate` cũ (giữ) rồi cùng `reconcile_pending` | Wrapper không còn là lớp bảo vệ duy nhất: boundary tự từ chối owner/gate đóng kể cả khi `_gate` bị vượt (kiểm) |
| Direct `app.citations.reconcile_pending(engine, policy)` | Cùng boundary | Bỏ tham số `now`: clock lấy từ `app.clock.utcnow` mỗi batch, không có tham số bypass |
| Extraction hook `reconcile_references` (`worker._extract`/`_extract_pdf` → `checkpoints.finish`) | Helper trong transaction extraction | Không commit/Session mới/journal; chỉ citation lock shared **sau** User → item `FOR UPDATE` → job của caller; caller giữ final boundary `_final` |
| Discovery hook `reconcile_arxiv_ids` (`discovery._write_page` trong `run_sync`) | Helper trong transaction page | Sửa thứ tự khóa (dưới); page/cursor/retarget vẫn atomic, không tự commit. **Engine/gate/HTTP/pacing của `run_sync` vẫn legacy (C2d-5f2)** |
| Lệnh cũ `docker compose exec worker python -m app.cli.reconcile_citations` bằng owner Compose | **Superseded** | Owner/migrator/recovery nay bị từ chối `halted:worker_runtime_required` trước mọi đọc reference |

## Entry (mỗi batch)

`entry` chạy trong transaction riêng qua `runtime.short` (rollback + close, lỗi release → invalidate + `worker_release_unavailable`):

1. `runtime.require_runtime` — login thực (cấm owner/superuser/recovery/SET ROLE/membership/DDL/TEMP/protected write…). Lỗi probe → `worker_runtime_unavailable`.
2. Grant citation, **mỗi quyền một `has_table_privilege`** (vì `has_table_privilege('t','A,B')` đúng khi có *bất kỳ* quyền nào — lỗi này được tìm ra và sửa trong PR): links SELECT/INSERT/UPDATE, references SELECT/UPDATE (cần cho `FOR SHARE`), library_items SELECT/UPDATE (cần cho `FOR KEY SHARE`), deletion_tombstones/discovery_papers/discovery_deleted_records SELECT. Thiếu → `worker_runtime_required`.
3. `auth_safety.require_writer` — pending/hold/missing/corrupt journal → `recovery_gate_closed`.

`enabled=false` → `{'skipped:disabled': 1}` không gửi SQL nào (kể cả probe). Không owner fallback, không provision trong đường chạy, không `force`/`now`/settings copy làm bypass.

## Batch transaction và lock graph

```
hint (pending, item mở, keyset > batch trước, ORDER BY id, LIMIT batch_size)   — chỉ là gợi ý
→ item common: FOR KEY SHARE SKIP LOCKED (UUID order)  → recheck revoked/tombstone dưới khóa
→ reference: FOR SHARE SKIP LOCKED (ID order), reload item/version/file/raw_citation
→ citation advisory SHARED (LINK_LOCK)
→ clock → discovery targets → INSERT … ON CONFLICT DO UPDATE WHERE khác … RETURNING
→ flush + SET CONSTRAINTS ALL IMMEDIATE
→ require_final(writer=True): journal advisory EXCLUSIVE trực tiếp, gate/head/cutover
→ binding scalar: link = giá trị đã quyết định, sha256(raw hiện tại) khớp, item khớp, item chưa đóng
→ clock mới ≥ clock ghi (sau SQL/lock wait cuối) → runtime.commit (known) → release sạch
```

Thứ tự toàn cục đã chọn: **item (row) → reference (row) → citation advisory → journal**. Không khóa hàng mới hoặc FK flush sau journal; không nâng shared → exclusive.

| Writer | Thứ tự thực tế | Vì sao không đảo chiều |
| --- | --- | --- |
| Extraction (worker) | User → item `FOR UPDATE` → job → rights/publication/version/file SHARE → reference INSERT (trigger 0024 `KEY SHARE` item đã giữ) → citation **shared** → link → journal exclusive (`_final`) | Item trước citation |
| Backfill (batch) | item common `KEY SHARE SKIP LOCKED` → reference `SHARE SKIP LOCKED` → citation **shared** → link (trigger: item đã giữ) → journal exclusive | Không bao giờ chờ hàng; chỉ chờ citation lock (timeout → `busy`) và journal |
| Discovery page | discovery rows → item common của link ứng viên `KEY SHARE` (**chờ**, UUID order) → citation **exclusive** → item xuất hiện muộn `KEY SHARE NOWAIT` → UPDATE link có điều kiện | Holder citation exclusive không bao giờ **chờ** item; NOWAIT bận → `RetargetBusy`, page rollback |
| Revoke (C2d-5c2) | User → item `FOR UPDATE` → … → journal exclusive | Không lấy citation lock |
| Cleanup (C2d-5e1 finalize) | tombstone → item `FOR UPDATE` → xóa link/reference → journal | Không lấy citation lock; chỉ đụng item đã revoked (writer citation bỏ qua) |
| GET references/citing-items | item SHARE → citation shared → journal shared | Cùng chiều |

**Lỗi lock graph trước PR (tái hiện tại base `37edd5c`, `DeadlockDetected`):** discovery lấy citation exclusive rồi UPDATE link của item common → trigger 0024 chờ item `KEY SHARE`; cùng lúc worker extraction giữ item `FOR UPDATE` và chờ citation shared → deadlock (PostgreSQL hủy một bên). Countercheck `lock-order` chứng minh test mới bắt được việc quay lại thứ tự cũ. Nay discovery chờ item **trước** khi lấy exclusive; kiểm bằng worker thật (common extraction) và hình extraction (runtime Session), quan sát `pg_stat_activity`/`pg_locks`: discovery chờ `FOR KEY SHARE` mà không giữ citation lock.

Serialization M5b (reference và metadata tới cùng lúc) giữ nguyên ở cả hai thứ tự, kiểm với worker thật: extraction trước (giữ shared, link chưa commit) → page chờ exclusive rồi retarget; page trước (giữ exclusive) → extraction chờ shared rồi thấy metadata đã commit. Khi metadata chưa có lúc extract, link `no_discovery_record` được page sau retarget thành `linked` mà không cần thao tác lại.

## Common revoked/tombstone

`not_revoked_reference_clause` (dùng cho hint backfill và discovery) nay loại item `revoked` **hoặc có tombstone** (alias riêng, không auto-correlate). Dưới khóa item, batch kiểm lại; revoke commit giữa hint và khóa → `skipped:revoked`, không chạm fence. Hai thứ tự đã kiểm bằng lock wait thật:

- **Batch/retarget trước:** giữ `KEY SHARE` → revoke chờ hàng item (quan sát) → batch/page commit trước `t_rev` → revoke xong → cleanup runtime xóa link/reference theo FK (kiểm với `cleanup_revoked` + MinIO thật: `deleted`, `ras_common_copy_derived_left = []`), private cùng bytes giữ link; sau đó backfill/retarget không ghi lại gì.
- **Revoke trước:** giữ item `FOR UPDATE` → backfill bỏ qua (`skipped:busy`, không chờ); page chờ item mà **không** giữ citation lock (backfill/extraction vẫn chạy), sau revoke thì loại item đã đóng; không lỗi fence, không ghi muộn.

Trigger 0024 vẫn là lớp chặn cuối; test không tắt trigger.

## Counts và outcome

`reconcile_pending` trả `dict` mã → số:

| Mã | Nghĩa |
| --- | --- |
| `batches` | Batch có ít nhất một reference đã ghi/so khớp, **commit known + release sạch** |
| `references` | Reference đã khóa, tính và bind trong các batch trên (gồm hàng đã đúng sẵn) |
| `links_written` | Hàng thực sự INSERT hoặc đổi giá trị (`RETURNING`); hàng giống hệt **không** tính, kể cả khi pass khác ghi trước |
| `skipped:busy` / `skipped:revoked` / `skipped:removed` | Bỏ qua trong pass này (keyset đi tiếp), lượt sau thử lại |
| `bound:batches` | Hết `reconcile_max_batches` với batch đầy (có thể còn việc) |
| `busy:citation_lock` | Lock timeout ở citation lock/INSERT trùng sau khi lấy được hàng; batch rollback, pass dừng; **không** phải halt (exit 0) |
| `halted:<mã>` | Dừng pass, không suy diễn/retry: `worker_runtime_required`, `worker_runtime_unavailable`, `recovery_gate_closed`, `worker_commit_uncertain`, `worker_release_unavailable`, `citation_binding_changed`, `citation_clock_invalid`, `worker_checkpoint_unavailable` |
| `committed_unreleased` | Commit known nhưng release lỗi: dữ liệu giữ, không cộng vào counts trên |
| `skipped:disabled` | `enabled=false`, không SQL |

Commit exception luôn uncertain: không SELECT/replay để suy thành công, không retry batch, không ghi lỗi/audit phụ; Outcome giữ trong `facts` độc lập rollback/close. Lượt độc lập sau đọc state mới và tiếp tục idempotent (link landed không pending; không landed thì ghi lại). Không có cursor/lease/queue bền: keyset chỉ trong bộ nhớ một pass; batch DB ngắn + `SKIP LOCKED` đủ để candidate bận/bị xóa không chặn phía sau và không lặp vô hạn. Không hứa exactly-once task execution.

Config giữ trong `CitationPolicy`: `reconcile_batch_size` 500 (1–5000), `reconcile_max_batches` 20 (1–1000), `reconcile_poll_seconds` 300 (30–86400); có test biên. Lock wait dùng `recovery.gate_lock_timeout_ms` (2000 ms). Giá trị là khởi đầu, **chưa đo**.

## Discovery hook — hợp đồng bàn giao C2d-5f2

- `reconcile_arxiv_ids(db, ids, now)` chạy trong transaction page của caller, **không commit**; caller (page) commit paper/topic/license/tombstone/cursor/link cùng nhau. Thứ tự: discovery rows → item common (chờ) → citation exclusive → NOWAIT item muộn → UPDATE link có điều kiện (Core, `rowcount`; hàng bị xóa đồng thời không gây StaleDataError).
- `RetargetBusy` (item xuất hiện muộn đang bị khóa): transaction page đã hỏng, `run_sync` rollback, ghi `last_error='RetargetBusy'` và backoff; **token/page counter không advance**, không có paper/link nào commit (kiểm với `run_sync` thật + fetcher giả).
- **Fan-out chưa bound:** một page (tối đa số record OAI của page) có thể retarget không giới hạn số link (mọi reference cùng arXiv ID của mọi User). Không cắt bớt rồi advance cursor; C2d-5f2 phải chọn bound/chia page hoặc hàng đợi (đổi hợp đồng atomic cần quyết định riêng).
- Còn legacy trong `run_sync`: engine theo cấu hình worker (không entry runtime riêng), chỉ `_gate` của Celery ở đầu, session advisory `SYNC_LOCK` giữ connection xuyên HTTP, `_reserve_request` sleep trong transaction request clock, page transaction không lock_timeout/final journal/binding, lỗi ghi `str(exc)`/tên lớp, HTTP 406 thật chưa xử lý. Không chạy mạng arXiv trong PR này.

## Kiểm chứng

Commit: docs orchestrator `0c6c4d8` → production code `8fb9671` → `8e2a500` (chỉ thêm `settle()`/`@settling` vào hai file test mới để thả pause/join thread khi assertion fail; `api/app`, migration và script khác giống hệt `8fb9671`). Môi trường: PG/MinIO/Redis/journal/role cô lập project `rasc2d5f17be2b23a` qua `scripts/c2d5f1_test_host.py`, login runtime `c1_runtime` thật, chỉ cutover nghiệp vụ scoped trong fixture (pending thật kiểm riêng); không `.env`, dev stack, provider hoặc arXiv.

Seal cuối `sealed-1` (`scripts/c2d5f1_seal.py`) tại `8e2a500`: **1.203 API unique pass**, 0 fail/error/skip, exit 0; collect 4.737, **3.534 not-run** (gồm 27 debt deselect); mọi nhóm `selection_matches`/`selection_in_collection`; 327 source hash (`api/`, `scripts/`) trước/sau trùng.

| Nhóm | Pass |
| --- | ---: |
| Smoke Linux: CLI container + Celery worker/beat Redis (SIGKILL giữa batch, resume, idempotent) | 2 |
| Citation runtime mới + hook với worker thật | 45 |
| Citation/discovery/GET references/citing-items/final caller/library ACL | 486 |
| Revoke HTTP C2d-5c2 | 170 |
| Extraction private/common, Markdown/PDF, common index | 138 |
| Cleanup runtime C2d-5e1 + cleanup/publication cũ | 104 |
| Schema: migrations/ORM, 0045/0044 migration, auth foundation, config | 244 |
| Recovery (phần không phải debt) | 14 |

**Tái hiện lỗi ở base `37edd5c`** (worktree tạm, đã gỡ): hook discovery cũ đối đầu transaction dạng extraction giữ item common `FOR UPDATE` rồi lấy citation shared → discovery được quan sát giữ citation exclusive và chờ khóa hàng → PostgreSQL `DeadlockDetected` phía discovery.

**Countercheck 10/10** tại `8e2a500`, tuần tự trước seal, source khôi phục byte/hash sau mỗi mutant, mỗi mutant đúng một node fail bằng AssertionError, không error: bỏ kiểm login runtime ở entry; bỏ probe grant từng quyền; bỏ gate entry; bỏ final journal/gate; bỏ binding scalar; bỏ kiểm lại common dưới khóa item; discovery lấy exclusive trước khi chờ item (thứ tự cũ); bỏ citation shared của hook extraction (arrival); đi tiếp sau commit uncertain; đi tiếp sau release lỗi. Lượt `counterchecks-1` tại `8fb9671` có 9/10: mutant lock-order bắt đúng assertion nhưng thread pause treo làm teardown báo thêm error — chỉ sửa test rồi chạy lại cả 10.

**Debt base deselect (27 node, not-run, không sửa/không tính pass):** 25 node mang từ PR #54 và 2 node mới lộ vì selection này thêm `test_discovery.py` (`test_checkpoint_retry_overlap_and_api_acl`, `test_persistent_oai_deleted_record_is_hidden_and_checkpointed`): fail giống hệt tại base `37edd5c` trong worktree cô lập — fixture legacy `bootstrap_admin` gặp gate C1 đóng trước mọi code discovery/citation. Bốn debt PR #50 nằm ngoài selection.

**Lượt không tính:** p1–p7 và prep-1/2/3 (sửa probe grant `has_table_privilege` dạng danh sách, fixture common/tombstone, pause theo executor thread, guard pool của fixture worker, đăng ký DB đang sống cho test 0019, tên file identity, chuẩn hóa node id có backslash trong ID tham số của script seal), chi tiết và SHA-256 log trong [inventory](c2d5f1-test-inventory.json). Artifact ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5f1-20261010`.

**Không chạy:** web/Chrome/npm (không đổi web/proxy), smoke process của C2d-5d2a/5e1/5e2 (đường của chúng không đổi; tag image đã gỡ), `test_common_search.py`/debt PR #50, drill backup/restore, toàn suite ngoài selection, mạng arXiv/OAI (HTTP 406 không xử lý), provider/charge/SLO, SET ROLE qua membership, stack dev.

**Tài nguyên:** project cô lập (3 container, 3 volume ẩn danh, network) và 3 volume tên `rasc2d5f17be2b23a_{db,minio,redis}-data` (smoke Celery tạo do khai báo compose; đã kiểm label project và 0 consumer) đã gỡ; image `rasc2d5f1-worker:20261010` tự build đã xóa. Container/volume/network/image có sẵn trùng baseline. `ras-worker-1`/`ras-beat-1` đã `Restarting` từ baseline; RestartCount 963/962 → 1076/1075 mà task không chạm, không suy nguyên nhân. Quét secret UTF-8/UTF-16 sau dọn: 0 hit.

### Lệnh đã chạy (chỉ môi trường cô lập, từ gốc repo)

```powershell
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py scripts/c2d5f1_test_host.py --context <ctx ngoài repo> setup
& $py scripts/c2d5f1_test_host.py --context <ctx> test api/tests/test_c2d5f1_citation_runtime.py -q
docker build -q -t rasc2d5f1-worker:20261010 api   # image cho smoke CLI/Celery, gỡ sau
& $py scripts/c2d5f1_counterchecks.py --context <ctx> --out <thư mục ngoài repo>
& $py scripts/c2d5f1_seal.py --context <ctx> --name <tên lượt>
```

Không chạy `python -m app.cli.reconcile_citations` trên stack dev; lệnh CLI chỉ được chạy trong container smoke với login runtime cô lập và trong test qua `main([])`.

## Không làm / giới hạn

- Discovery harvesting runtime (C2d-5f2); search/query/rerank → RAG/generation → tổng hồi quy C2d → C3/C4.
- Không migration/grant mới; production operator/delivery/storage/cutover không đổi. Trước C4 gate production luôn `auth_safety_pending`, nên backfill chỉ chạy được trong môi trường thử có cutover scoped.
- SET ROLE qua membership không kiểm bằng test (role cấp cluster dùng chung); `require_runtime` vẫn từ chối.
- Fault injection trong process ≠ mất điện; kill process thật chỉ chứng minh resume sau SIGKILL.
- **Safety pending tới C4.**
