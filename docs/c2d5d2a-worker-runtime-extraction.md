# C2d-5d2a — worker runtime và extraction

**Review độc lập PR #51 (2026-10-10) tại head `b569d0c`:** không phát hiện điểm chặn merge mới trong scope extraction; **103/103 API** reviewer đạt, gồm process worker/beat thực và Redis stop/start. 293 source hashes khớp; artifact agent **549/8 countercheck** kiểm riêng. PR đã merge `9fb8890`, chưa rerun tại merge, safety pending tới C4; [report, evidence và giới hạn](reviews/pr51-c2d5d2a-worker-runtime-extraction.md). Kết quả sealed agent bên dưới giữ nguyên nguồn, không tính là reviewer chạy lại toàn bộ.

Source kiểm: `dfca84ac04a68bbce29abe0e9d1b87ac51da28ac` (production code `5b505d3`, commit sau chỉ sửa fixture/đối chứng). Base đã fetch và xác minh `origin/main` bằng merge PR #50 `03b79855221f3d9279cbe5aa677d6a50db59b5ab`; không suy revision DB dev. Source migration vẫn `0042`, không migration/grants mới. [Task](tasks/m6c2b2c2d5d2a-worker-runtime-extraction.md) · [Inventory selection/JUnit/hash](c2d5d2a-test-inventory.json). Docs orchestrator/reviewer có sẵn được giữ trong commit riêng `397803c` trước code. PR đã merge `9fb8890`; **safety pending tới C4**. [Prompt C2d-5d2b](tasks/m6c2b2c2d5d2b-document-index-runtime.md) đã giao, chưa triển khai/kiểm.

## Phạm vi và cấu hình

Worker/beat dùng engine ứng dụng từ `RAS_DB__*`, kiểm login thực trước business task và tại entry trực tiếp. `ingest/runtime.py` từ chối owner, superuser, recovery, membership, quyền DDL/TEMP/BYPASSRLS, raw protected writes và thiếu routine/grant; không thử credential khác. `worker_process_init` dispose pool kế thừa sau fork. Thiếu connection parts hoặc credential sai trả mã kiểm soát, không log exception chứa connection/data.

`scripts/compose.worker-runtime.yml` chỉ override worker/beat bằng runtime login/password bắt buộc, key provider trống, pricing false, discovery disabled. Smoke merge override này với Compose gốc và một overlay thử ngoài repo, explicit `--env-file`, project/network riêng. Container không nhận operator/recovery password. Setup/provision/migrate do host Conda riêng; runtime không provision. `scripts/c2d5d2a_test_host.py` kiểm project/host ports trước pytest và đặt settings trước import, chặn HTTP ngoài.

**Compose mặc định/API chưa cutover.** File Compose gốc không đổi; restart worker mặc định còn credential owner sẽ fail closed. Không áp override vào dev. Gate production vẫn pending: các bài business chỉ patch cutover-only, integrity/journal/hold/role vẫn thật; bài pending HTTP/task không patch. Override kiểm runtime không chứng nhận mọi task nền đã chuyển.

| Caller | Trạng thái sau PR |
| --- | --- |
| `ras.ingest.discover`, `due_job_ids`, `dispatchable` | Đã chuyển: proof mới, connection release trước broker, chỉ UUID |
| `ras.ingest.process`, `process_job`, claim/begin/finish/fail | Đã chuyển extraction private/common, checkpoint runtime |
| Validate, Markdown/PDF extract/chunk, reconcile trong extraction | Đã chuyển theo proof bất biến, I/O ngoài DB, DML trước journal |
| `awaiting_index`, `indexing`, retry `resume_state=indexing` | Giữ nguyên, mã `index_pending`; không claim/attempt/ledger/provider |
| `indexer.process_index` và các helper provider | Legacy còn trong module, không còn được worker gọi; C2d-5d2b |
| `ras.discovery.sync`, `ras.citations.reconcile` | Chỉ kiểm runtime/gate đầu; business boundary còn legacy. **Sau C2d-5f1 (nhánh review):** `ras.citations.reconcile` dùng batch boundary runtime [citation](c2d5f1-citation-reconcile-runtime.md); `ras.discovery.sync` vẫn legacy (C2d-5f2) |
| `ras.revocation.cleanup` | Tại PR này chỉ kiểm runtime/gate đầu; **sau C2d-5e1** dùng boundary runtime [cleanup](c2d5e1-revoked-common-cleanup-runtime.md) |
| Recovery/operator/CLI reconcile/cleanup/backup | Không chuyển trong PR; engine/setup theo hợp đồng riêng |
| Search/query embedding/rerank, RAG/generation | Task riêng; không kiểm provider/cutover ở PR này |

Task nền legacy có thể bị runtime role từ chối protected write khi gate sau này mở; không cấp thêm quyền để tránh điều đó. Smoke chỉ cho lịch extraction, không giả định vận hành background đầy đủ.

## Claim, checkpoint và clock

Candidate lookup không khóa chỉ là gợi ý. Private khóa owner User qua routine → item UPDATE → job UPDATE → version/file SHARE; common item → job → rights/publication/version/file SHARE, không khóa User/session Admin tạo job. Item SKIP LOCKED trước job khi claim/dispatch. Scalar job scope được so lại sau khóa. Common dùng `common_index.block_reason` gồm publication/tombstone/rights/audience/exact file/checksum/epoch/fulltext/external/conditions; private dùng exact owner/scope và `ras_auth_status_live` (latest head + receipt), không suy quyền từ trùng bytes.

`checkpoints.entry` kiểm integrity/cutover ở short transaction đã release trước khóa resource. Writer flush toàn DML/FK/unique/citation, `SET CONSTRAINTS ALL IMMEDIATE`, rồi lấy journal exclusive trực tiếp qua `auth_safety.require_final(writer=True)`. Đọc lại binding/state/cancel/epoch/quyền/status, clock mới sau SQL cuối và journal wait; không khóa resource sau journal. Claim chỉ nhận lease đã hết, renew/finish/fail phải còn live theo deadline cũ; đúng biên là hết. Binding bao gồm job/item/version/file và snapshot rights/publication, storage key/checksum/actual expected size/mime/page count. Proof source/sections/pages detach, frozen đến nested JSON.

Dispatch kiểm lại gate ngay trước `delay(str(job_id))`, consumer tự proof claim. Broker exception dừng tick, DB job không bị đánh dấu đã gửi. Hai claimant/message trùng chỉ một token tăng attempt. Retry/backoff/exhausted dùng config hiện hành; index states không bị reset và reservation không bị đụng.

## I/O và kết quả

Mỗi stage kiểm proof mới trước mở S3; actual size và SHA-256 ở mỗi lần đọc, kể cả overwrite cùng kích thước. Sau read kiểm lại gate/binding/lease trước parser/chunk CPU; transaction finish mới kiểm lại toàn bộ trước ghi/commit. S3/parser không giữ connection/row/journal locks. Markdown/PDF parser/version, 70%/100/2.000 threshold, subprocess timeout/memory/output/page limits giữ nguyên; không OCR. PDF không đủ body hoặc bibliography đáng ngờ vẫn rejected/review với coverage rõ.

Reuse đối chiếu text/hash/locator/pipeline của đúng file; PDF đối chiếu page geometry/layout và offsets. Không dùng `layout-v1` thay `layout-v2`, không chia sẻ derivatives giữa owners/common/private. Reference reconcile có locks và guard exact-ID/stale/tombstone trước journal; reference chunks vẫn mang kind riêng, không trở thành body evidence. Fence `0024` và cleanup giữ nguyên, không bảng dẫn xuất mới.

Thành công: job `awaiting_index`, có `extraction_finished_at`, `finished_at=NULL`, lease release; item `metadata_only`, active generation NULL. Terminal mới ghi `finished_at`. Stream đọc đóng trong `finally`; parser subprocess hiện hành kill/wait khi timeout, temp được xóa sau settle. Test cancellation dùng KeyboardInterrupt ở storage; test timeout cho child thật ngủ tại boundary subprocess. Đây không là chứng cứ SIGKILL/mất điện hay resource limit đầy đủ trên mọi OS.

## Pause, thu hồi và uncertain

- Owner disabled/held/head-receipt không chứng minh được: pause trước I/O/commit, giữ durable state/lease để hết tự nhiên; không `_fail`, không terminal, không tự enable. Khi status hợp lệ và lease hết dispatcher tiếp tục được.
- Common revoke, cancel/epoch/binding thay hoặc token lease mất: dừng và bỏ kết quả; không content I/O stage sau/derived commit. Khi revoke đã thắng trước claim, không ghi cancellation thêm; writer revoke/recovery/cleanup sở hữu việc đó.
- Gate hold/pending/journal lỗi ở bất kỳ checkpoint: skip mã rõ, không dùng failure checkpoint để vượt gate. Không replay journal hay mở recovery connection trong worker.
- `Outcome(commit_started, committed)` thuộc attempt độc lập teardown. Mọi exception sau bắt đầu commit là `worker_commit_uncertain`, kể cả landed/rollback; không SELECT suy success, stage sau, failure mutation, retry trong cùng attempt hoặc xóa object.
- Known commit rồi rollback/close lỗi: invalidate, mã `worker_release_unavailable`, giữ durable mutation và dừng. SQL/release lỗi không bị chuyển thành parser lỗi. Rollback/close đều được thử; secondary teardown không xóa uncertain fact.
- Lượt dispatch mới sau Redis/process mất có thể claim theo durable state và resume idempotent, khác với tự retry sau uncertain trong attempt cũ.

## Kiểm chứng và lỗi chuẩn bị

Selection cuối, JUnit unique, source hash trước/sau, raw logs, đối chứng và not-run được ghi trong inventory, không cộng các pass lịch sử PR #50. PostgreSQL/MinIO/journal/Redis riêng; smoke worker + beat thật, identity/permissions bằng connection của chính tiến trình, Redis stop/start, ba job Markdown private/PDF/promotion. Kiểm role sai, năm task pending/hold/missing journal, health/ready, gate/status/binding/cancel/lease thay trong storage/parser, hai thứ tự claim/revoke, clock sau SQL/wait, stale token, retry idempotent, commit landed/rollback và teardown faults. Tám mutant đều bị assertion nghiệp vụ/no-I/O/no-write bắt, source khôi phục byte trong `finally`, không chạy song song suite.

Lỗi chuẩn bị giữ raw XML/log riêng: fixture `installation` thiếu khi chọn test riêng; setup network internal không publish port (một lần connection thử `task_admin` tới default 5432 bị authentication từ chối trước SQL, sau đó host guard từ chối port absent/default); duplicate Origin header; helper lấy scalar trên DML; assertion bibliography body; corruption bị trigger hợp lệ từ chối; rights CHECK đòi external tắt cùng fulltext; smoke network label/Redis password expansion/DNS khi Redis dừng/identity PID1 trùng; gate observer gắn nhầm engine; unit PDF truyền cả URL giả và DB env; assertion cleanup dùng `purged` thay mã thật `deleted`. Các sửa fixture không nới production grants/gate/classifier. Docker build native wrapper từng trả exit1 dù image build thành công; image ID được kiểm riêng. Baseline container inspect đầu có env trong temp đã giảm ngay về field công khai, không đưa env vào inventory.

Lượt sealed đầu tại `5b505d3` có core **75 pass**, extraction **110 pass**, PR #50 **169 pass/2 fail**, rồi dừng. Hai fail trực tiếp do fixture claim cũ: một ca còn dùng owner; ca còn lại giả định claim luôn thắng sau khóa legacy, trong khi response mới có thể giữ item SHARE và claim runtime phải SKIP. Sửa fixture dùng runtime, kiểm job/attempt không đổi khi SKIP rồi claim ở lượt mới; không nới gate/quyền hay bỏ ca. Năm ca nhắm vào sửa này và race claim/revoke đạt **5/5** trước sealed run mới. Các XML/log/source đầu giữ trong `attempt-1`, không ghi đè.

Đối chứng item/job đầu chỉ ném Halt lock timeout trước assertion dữ liệu, được giữ là **lượt yếu, không tính đạt**. Ca mạnh hơn thu outcome có mã, đợi hai phía settle, kiểm cancellation/attempt/derived/no-I/O rồi assert claim SKIP thay vì lỗi khóa; mutant thất bại ở assertion nghiệp vụ đó. Tổng **8 cơ chế đạt / 9 lượt raw**, không có setup error được tính làm đạt.

Cleanup runner có ba lượt bị guard chặn trước mọi lệnh xóa: snapshot volume là nhiều khối JSON; so ID ngắn với ID đầy đủ; quét thấy credential sinh riêng trong XML setup cũ `prep-worker-2.xml`. XML đó được che đúng sáu chỗ credential, giữ node/status/count/trace còn lại và SHA-256 bản gốc + bản redacted trong `redaction.json`; không giữ bytes gốc chứa credential. Đây là giới hạn raw evidence được nêu tường minh, không gọi file đó là raw không sửa. Quét lại không hit; cleanup cuối xóa đúng **6 container/9 volume** của hai project đã xác minh, giữ 9 container có sẵn và toàn bộ volume baseline, ports thử đóng, bốn file credential/Compose thử xóa. Image thử giữ để đối chiếu/reproduce, không đổi tag image dev.

Bốn lỗi baseline PR #50 **không sửa/không tính pass**, không rerun trong selection này: hai corruption fixture `session_replication_role` ở common search, readiness expectation cũ và common-original helper revoke 409. Không chạy toàn suite index/provider hoặc full business cutover; ca citation nối search/RAG/provider cũng nằm trong not-run. Web/proxy không sửa nên không chạy Chrome/npm/typecheck/build. Không provider thật, arXiv, charge, pricing change, operator C3 hoặc SLO/benchmark.

## Bàn giao C2d-5d2b

`indexer._gate/_reserve/_verified_unlocked/process_index/_provider_failure/_publish` vẫn legacy, chưa được chứng nhận bằng runtime checkpoint. PR sau cần dispatch/claim riêng cho ba index states đang giữ, không thêm cờ nối lại legacy trong extraction. Kiểm actual runtime grants, owner/status hoặc common rights exact, binding/job/call lease và fresh clock ở reservation/heartbeat/provider-outcome/publish; không giữ connection khi storage/provider.

Giữ kế toán common `owner_user_id=NULL`, app/bucket budget và private quota theo quyết định hiện hành (phần common chưa là xác nhận trực tiếp của chủ dự án). Không đổi pricing/model/allowlist để làm test xanh; pricing thật vẫn phải đối soát charge. Document/query dùng cùng active model ID/dim/version; unchanged chunks không gọi lại. Làm rõ uncertain provider/commit reuse, không tự giải reservation chưa rõ charge; checksum toàn object trước mỗi provider và ngay trước final publish kể cả reuse call đã hoàn tất, HEAD không thay hash. Cần fault/race/cancellation và smoke provider giả riêng cho PR đó, chưa có ở đây.

Khe overwrite sau lần hash cuối tới commit vẫn còn. Private deletion/membership journal, delivery/operator, storage/journal production/backup, các background task khác và tổng hồi quy/C3/C4 còn riêng. **Safety pending tới C4**.


## Kết quả sealed cuối tại dfca84a

**549/549 API unique pass**, 0 failure/error/skip; collect **4343**, **3794 not-run**. Selection và JUnit khớp đủ, thiếu/trùng/extra 0. SHA-256 của **293 file source/config/test/script** trước/sau bằng nhau; không sửa source hoặc chạy mutant trong suite.

| Nhóm | Pass |
| --- | ---: |
| core | 75 |
| extraction | 110 |
| request | 171 |
| revoke | 170 |
| upload | 9 |
| recovery | 11 |
| schema | 3 |

Raw log/JUnit/selection/source và các lượt lỗi giữ ở `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5d2a-20261009-run`; inventory có hash từng artifact. Chỉ kết quả sealed này được cộng; lượt đầu 169 pass/2 fail của nhóm PR #50 và một mutant yếu được giữ riêng. Tám cơ chế đối chứng hợp lệ, chín lượt raw. Cleanup xác minh label/project/sole volume consumer trước xóa; snapshot 9 container có sẵn giữ nguyên ID/image/StartedAt, volume có sẵn được giữ, ports thử đóng, credentials thử xóa, quét credential sinh riêng không hit. Không gọi toàn API inventory xanh hoặc bốn lỗi baseline đã sửa.
