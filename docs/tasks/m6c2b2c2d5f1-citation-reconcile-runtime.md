# Prompt C2d-5f1: citation reconcile qua runtime

## 1. Nhiệm vụ và base

Bạn là coding agent chính. Hoàn thành **một PR C2d-5f1**: khảo sát, triển khai, kiểm thử, cập nhật docs, commit/push và mở PR **tiếng Việt** để chủ dự án review; **không merge**. Chuyển background citation reconcile sang runtime thật; giữ đúng quan hệ metadata “paper này trích paper kia”, không biến bibliography thành content evidence.

PR #54 đã merge tại **`37edd5c9c28e60ba43425f0b1bd9f42946585e28`**; head review `cf66f1c`, merge tree khớp head. Source migration **0045**, không suy revision DB dev. Fetch/xác minh lại rồi tạo nhánh `feat/m6c2b2c2d5f1-citation-reconcile-runtime` từ `origin/main`. Giữ mọi docs review/task chưa commit của orchestrator, gồm [review PR #54](../reviews/pr54-c2d5e2-orphan-original-reconcile-runtime.md); có thể commit docs có sẵn riêng. Không reset/clean hoặc ghi đè thay đổi của người khác.

Đọc [AGENTS](../../AGENTS.md), [mục lục](../README.md), [yêu cầu](../product-requirements.md), [quyền/dữ liệu](../access-and-data.md), [quyết định giao](../decisions.md#giao-c2d-5f1-sau-merge-pr-54-2026-10-10), [worker/extraction](../c2d5d2a-worker-runtime-extraction.md), [cleanup common](../c2d5e1-revoked-common-cleanup-runtime.md), [orphan reconcile](../c2d5e2-orphan-original-reconcile-runtime.md), [nền C1](../c1-auth-safety-foundation.md) và [kiểm thử](../testing.md).

**Safety pending tới C4.** Không đọc/sửa `.env`/key, query/migrate/restart dev hoặc tài nguyên có sẵn, gọi provider/arXiv thật, đổi pricing/cutover hoặc làm C3/C4. Chỉ Python Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước chạy/cài, không tạo venv. PG/MinIO/Redis/journal/roles và artifact thử phải cô lập. Worker/beat dev đang restart từ trước: không khắc phục hoặc dùng chúng để thử; chụp trạng thái và báo drift nếu có, không suy nguyên nhân.

## 2. Phạm vi đã chốt

Orchestrator chọn **citation trước, discovery harvesting sau** trong nhóm roadmap “discovery/citation”. Đây là lựa chọn scoped theo ủy quyền, không phải xác nhận trực tiếp mới của chủ dự án hoặc luật kiến trúc vĩnh viễn.

| Trong PR C2d-5f1 | Task riêng |
| --- | --- |
| `citations.reconcile_pending`, CLI `reconcile_citations`, Celery `ras.citations.reconcile`, caller trực tiếp tương ứng | `discovery.run_sync`, fetch OAI, request pacing/lease/cursor/backoff: C2d-5f2 |
| Hợp đồng transaction/lock và phần sửa cần thiết của `reconcile_references` / `reconcile_arxiv_ids` để giữ extraction/discovery hooks an toàn | Search/query/rerank, RAG/generation, mở thêm citation graph chung hoặc ACL mới |
| Runtime/gate/final binding, bounded batches, outcome/release, concurrency và idempotence | Đổi matcher, fuzzy/title matching, tải paper/full text/OCR hoặc gọi provider |
| Migration/grants hẹp nếu thật sự cần; fixtures, hồi quy và handoff | Production operator/delivery/storage, cutover C3/C4, sửa hàng loạt baseline debt |

Không gọi cả discovery là đã chuyển runtime chỉ vì hook citation được sửa. Không chạy mạng arXiv để xử lý HTTP 406 trong PR này. Giữ API/UI hiện hành; chỉ sửa nếu phải phản ánh thay đổi hợp đồng cụ thể đã chứng minh.

## 3. Khảo sát trước sửa

Lập call/engine/transaction/lock map cho `app/citations.py`, `cli/reconcile_citations.py`, `ingest/{celery_app,worker,checkpoints,runtime}.py`, `discovery.py`, `db_roles.py`, models reference/link/discovery và fence derived `0024` cùng các revision hiện hành. Tìm mọi caller/test/script, không chỉ entry Celery.

Hiện trạng đã đọc, phải tái kiểm:

- `ras.citations.reconcile` chỉ có runtime/gate ở đầu task; `reconcile_pending` và CLI chưa tự kiểm, từng batch dùng Session/commit legacy. Task wrapper không bảo vệ caller trực tiếp hoặc gate đóng giữa các batch.
- `reconcile_references` là helper trong transaction extraction đã có User/item/job locks và final checkpoint. Không được thêm internal commit, Session mới hoặc journal lock sớm làm tách link khỏi transaction extraction.
- `_write_page` của discovery gọi `reconcile_arxiv_ids` trong transaction metadata/cursor. Hook lấy citation advisory lock exclusive; extraction lấy shared. Khi update link common, derived trigger có thể lấy item lock. **Phải dựng lock graph và kiểm nguy cơ đảo chiều citation lock → item so với worker item → citation lock**, không coi “đã có advisory lock” là đủ.
- `not_revoked_reference_clause` hiện lọc publication state; DB derived fence còn xét tombstone. Kiểm cả predicate thật và race với revoke/cleanup, không bỏ trigger khi chạy runtime để làm test xanh.
- M5b có serialization để tránh mất link khi reference và metadata tới đồng thời. Đổi lock/caller phải giữ bảo đảm này cho cả hai thứ tự arrival, kể cả khi metadata chưa có lúc extract.
- Discovery harvesting vẫn giữ connection/session lock xuyên HTTP và sleep trong transaction request clock. Ghi rõ đó là nợ C2d-5f2, không gọi là đã sửa trong PR này.

Docs/code lệch thì báo hành vi, ảnh hưởng và quyết định trước sửa phần phụ thuộc; không âm thầm đổi expectation hoặc mở grants để che lỗi. Không cần hỏi lại các lựa chọn scoped đã chốt ở đây.

## 4. Hợp đồng triển khai

### 4.1. Ba entry phải dùng chung runtime boundary

- CLI, Celery và direct `reconcile_pending` đều kiểm **actual runtime login**, quyền hiện hành và recovery/auth-safety gate trước đọc private references hoặc ghi batch. Owner/migrator/recovery, SET ROLE/membership không hợp lệ, thiếu grant hoặc lỗi probe/release đều bị từ chối; không operator fallback/provision trong đường chạy.
- `enabled=false` có thể trả no-op nhưng không được truy vấn private data hoặc mutation. Real pending/hold/missing/corrupt journal giữ đóng; không dùng `force`, tham số `now` hoặc settings copy làm bypass production.
- Entry/checkpoint trả Session/connection sạch; lỗi bất ngờ có mã kiểm soát. CLI in mã/counts, không raw citation/title/token/URL DB/credential, exit khác 0 khi halted. Không đổi ngầm CLI thành preview nếu không có lý do nghiệp vụ; đây là reconcile metadata, không orphan object deletion.

### 4.2. Batch có giới hạn, rebind dưới khóa và final boundary

- Candidate lookup chỉ là hint. Trước mutation, khóa/reload đúng tập item/reference/link và kiểm lại item–version–file/provenance, matcher/raw-citation hash và target/tombstone hiện hành. Không dùng ORM identity-map cũ làm proof; batch có ID sai/stale không được ghi vào item/file khác.
- Đối với common, khóa item đủ để serialize với revoke/cleanup **trước khi** lấy citation advisory lock; nhiều item khóa theo thứ tự UUID ổn định. Xác minh chế độ khóa thực tế với PostgreSQL. Không có row/key lock mới hoặc FK flush sau journal boundary, không shared→exclusive upgrade. Bảo toàn thứ tự User → item → job của extraction khi helper được gọi trong transaction đó.
- Ghi link, flush DML/FK/constraints, lấy journal exclusive trực tiếp cho writer, kiểm lại gate và scalar binding/state/hash/target; đọc clock mới sau SQL/wait cuối rồi commit xác định. Helper dùng trong transaction caller không tự final/commit; caller chịu final boundary. Không ép reference phải `indexed`/có active generation để nối bibliography: extraction và metadata-only link giữ hợp đồng hiện hành.
- Common revoked/có tombstone bị loại trước batch selection và kiểm lại dưới khóa. Nếu revoke thắng, không chèn/cập nhật dẫn xuất; nếu batch thắng, revoke chờ rồi cleanup xóa link theo FK. Không hồi sinh link/vector/item sau `t_rev` hoặc purge; private cùng bytes/checksum giữ nguyên.
- Giữ `reconcile_batch_size` / `reconcile_max_batches` trong config, có test biên. Busy/removed/stale candidate không gây vòng lặp vô hạn hoặc chặn vĩnh viễn các candidate phía sau. Không thêm lease/queue/cursor bền nếu batch DB ngắn đã giải quyết được concurrency và tiến độ.

### 4.3. Outcome và resume

- Mọi commit exception là uncertain: dừng pass, không SELECT/replay để suy thành công, không tự retry batch, không ghi error/audit phụ trên Session lỗi. Counts thành công chỉ từ commit đã biết và release sạch; phân biệt halted/busy với chạy hết bound.
- Outcome không bị rollback/close ghi đè. Lỗi rollback/close/invalidate được kiểm; không tiếp tục batch sau lỗi release. Batch đã commit vẫn giữ dữ liệu; invocation độc lập đọc state mới để tiếp tục idempotently.
- Hai process cùng chạy không tạo hai link cho cùng reference, không làm lùi matcher/hash/target theo snapshot cũ, không đếm link cũ thành mutation mới nếu báo cáo gọi đó là số thay đổi. Nêu chính xác nghĩa mỗi count; không hứa exactly-once task execution.
- Không thêm journal chứa bibliography hoặc quan hệ trích dẫn. Đây là dữ liệu dẫn xuất; journal quyền/auth và cleanup fences hiện hành giữ nguyên. Nếu cần schema mới, thêm revision sau head thực tế (hiện `0045`, dự kiến `0046`); không sửa revision đã merge, giữ ORM/schema xanh và grants tối thiểu.

### 4.4. Hooks extraction/discovery và ngữ nghĩa citation

- Giữ reference/link cùng transaction extraction; không bỏ hook hoặc chuyển toàn bộ sang backfill để né lock graph. Race hai thứ tự metadata/reference arrival vẫn phải hội tụ đúng, không cần người dùng thao tác lại.
- Với discovery hook, chỉ sửa phần citation transaction/lock cần thiết để tương thích. Giữ metadata/page cursor và retarget atomic như hiện hành, helper không tự commit. Không chứng nhận engine/gate/HTTP/pacing/outcome toàn bộ `run_sync`; bàn giao cụ thể cho C2d-5f2. Không thêm một “đường legacy fallback” vào entry runtime để chiều fixture owner.
- Nếu retarget theo một page có fan-out chưa được bound, ghi giới hạn và contract cần xử lý trong C2d-5f2; không âm thầm cắt link updates nhưng vẫn advance cursor như đã hoàn tất. Thay atomic retarget bằng eventual queue là thay hợp đồng, không tự thêm trong task này.
- Giữ matcher `arxiv-exact-v2`, explicit ID/host/DOI boundaries, citation version tách latest metadata version, stale hash/matcher hidden, OAI tombstone hidden. Không mở title/fuzzy matching hoặc coi giấy phép metadata là AccessRights.
- Không network/storage/provider I/O; không fetch URL lấy từ citation. Bibliography/link/discovery metadata không vào keyword/vector content index, embedding/rerank/RAG evidence hoặc payload. API owner/private/common gates, reverse lookup chỉ private của caller và final caller/read boundaries giữ nguyên.

## 5. Kiểm chứng bắt buộc

Kiểm bằng runtime login thật, PostgreSQL/journal cô lập; MinIO/Redis riêng khi fixture/process cần. Privileged setup/adversary và business cutover fixture phải có nhãn; giữ riêng test HTTP/task pending thật, không production bypass.

1. **Entry:** CLI/direct/Celery với runtime hợp lệ và owner/recovery/profile sai/thiếu grant/probe/release lỗi; gate pending/hold/missing/corrupt trước entry và giữa batch. Không private read/mutation sau refusal; health sống, readiness đóng như hợp đồng.
2. **Đúng dữ liệu:** private A/B cùng bytes/citation; common allowed/revoked/tombstone/purged; reference version/file/provenance khác; raw hash/matcher stale; exact ID forms/look-alikes/multiple/version conflict; metadata tới muộn, OAI deleted và newer metadata. Snapshot item/file/rights/index/ledger/private khác không đổi ngoài link được chọn.
3. **Concurrency:** hai reconcile process cùng candidate, extraction ↔ backfill, extraction ↔ discovery retarget hai thứ tự arrival, revoke/cleanup ↔ backfill/retarget hai thứ tự khóa; barrier/lock-wait thật, không chỉ random loop. Không deadlock, late write hoặc lost link. Khóa item/citation/journal được quan sát theo graph đã chọn.
4. **Fault/outcome:** commit landed/not landed nhưng mất response; lỗi rollback/close sau known commit; journal/gate đổi trước final; batch sau không chạy. Invocation mới tiếp tục đúng, không nhân link/mutation/count. Busy/lock-timeout và batch/page bounds có test rõ.
5. **Process smoke:** CLI và Celery task qua worker runtime thật trong Linux/Redis cô lập. Chạy lại cùng dữ liệu idempotent; dừng/restart giữa batch để chứng minh resume. Có thể kill process thật tại barrier trước/sau commit; fault injection chỉ gọi đúng tên mô phỏng, không coi là chứng minh mất điện.
6. **Hồi quy hữu hạn:** citation matcher/links hiện có, extraction private/common, discovery page+target hook, GET references/citing-items/final caller, revoke/derived cleanup fences, config và ORM/schema; migration upgrade có dữ liệu/grants/downgrade nếu thêm schema. Canary bibliography không vào provider/content evidence. Không chạy thêm web chỉ vì đã đổi docs; nếu sửa web/proxy thì test/build và smoke đúng luồng bị ảnh hưởng.
7. **Countercheck:** bỏ từng guard quan trọng có test bắt assertion nghiệp vụ (runtime, gate cuối, binding/hash, common fence, lock order/arrival serialization, uncertain/release). Chạy riêng, giữ raw evidence, khôi phục byte/hash trước lượt seal cuối; không sửa code khi suite đang chạy.

Không bắt chạy lại hàng nghìn test không liên quan. Lập selection theo call graph, collect đầy đủ và đối chiếu exact node/JUnit không thiếu/trùng/uncollected, source hash trước/sau, ghi rõ not-run. 25 debt trong inventory PR #54 và bốn debt PR #50 giữ riêng; fail mới phải tái hiện ở base trước khi gọi là baseline. Không sửa expectation quyền/ready để làm xanh; lỗi thuộc phạm vi task phải sửa. Một rerun xanh không tự giải thích fail trước đó.

## 6. Bàn giao và hoàn thành

- Tạo `docs/c2d5f1-citation-reconcile-runtime.md` và `docs/c2d5f1-test-inventory.json`: call/engine/transaction/lock map, grant/migration nếu có, meaning counts/outcome, entry/runbook đã chạy, source/selection/JUnit/artifact hash, các lượt lỗi và not-run, giới hạn.
- Cập nhật caller inventory, AGENTS, docs index, roadmap, decisions/testing/dev-setup và README khi trạng thái/hành vi liên quan đổi; ghi PR #54 đã merge, giữ số review/agent theo source lịch sử, không tính lại là pass tại merge. Không thêm lệnh chưa chạy, không ghi lựa chọn orchestrator thành “chủ dự án trực tiếp xác nhận”.
- Handoff C2d-5f2 phải nêu rõ contract hook, lock order, fan-out bound, engine/role, discovery lease/pacing/cursor/outcome còn legacy; tiếp đó search/query/rerank → RAG → tổng hồi quy C2d → C3/C4 theo điều kiện cũ. Không gọi các phần chỉ kiểm tương thích là đã chuyển.
- Artifact/log/credentials ngoài repo; quét secret. Dọn đúng tài nguyên thử đã kiểm label/consumer và đường dẫn, giữ resources có sẵn; báo drift ID/image/StartedAt/RestartCount riêng. Không commit paper, journal hoặc credentials.
- Commit/push/mở PR tiếng Việt cho chủ dự án review; không merge. Không thêm AI vào author/committer/trailer hay dòng quảng bá trong PR. Báo base/branch/code SHA/head, files, pass/fail/error/skip/not-run, hạn chế và phần chưa làm. **Safety vẫn pending tới C4.**
