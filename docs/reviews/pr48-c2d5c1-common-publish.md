# Review PR #48 — C2d-5c1 publish metadata

**Sau review:** PR #48 đã merge `1eede17c1eed82fa7a00f7018fe01b4362ce30a9` ngày 2026-10-09, chứa `520ac54`/`654fbac`; merge tree giống head PR. Chưa rerun tại merge. Kết quả/evidence bên dưới giữ source/thời điểm review; [C2d-5c2](../tasks/m6c2b2c2d5c2-common-revoke.md) được giao tiếp theo, safety pending tới C4.

Ngày **2026-10-09**. PR **OPEN, chưa merge** tại lúc review. Base `04434e35e2b016601d3629a2d4afe3feb6042a6b` (PR #47); code `520ac546abb4812dc1b2744e93437d4288dd5c57`; head `654fbacaf5198d75b7893904c2a8852b04a12a5e`. Working tree sạch trước kiểm chứng. Source migration **0041**, không migration/grants mới, không suy revision DB dev.

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi POST publish metadata.** Kết luận chỉ áp cho `POST /admin/common-copies/{item_id}/publish`; **safety vẫn pending tới C4**. Revoke C2d-5c2, index/provider, C3/C4 và full business HTTP chưa được chứng nhận. Reviewer chỉ bổ sung docs, không sửa ứng dụng/migration, commit/push/merge hoặc đăng comment PR.

[Task](../tasks/m6c2b2c2d5c1-common-publish.md), [handoff](../c2d5c1-common-publish.md) và [inventory agent](../c2d5c1-test-inventory.json) là nguồn phạm vi/evidence; kết quả dưới đây tách reviewer thực chạy khỏi artifact agent.

## Đối chiếu code

- Proof ngắn khóa common item SHARE → rights/version/file → journal shared, kiểm exact target/current Admin/session/head/receipt/CSRF/clock rồi release sạch. `_short` thử rollback lẫn close, invalidate khi release lỗi; không tiếp tục hash hoặc trả DTO từ proof lỗi.
- Hash theo chunk size/SHA-256 ngoài Session, connection và DB locks; attempt giữ slot/thread tới khi stream settle. Không copy/put/delete original. Storage denial lấy proof caller/gate/resource mới sau I/O, không dùng audit best-effort làm quyền trả response.
- Acceptance Admin User → item UPDATE → resources → toàn DML/FK/unique/audit flush → journal exclusive trực tiếp → frozen tuple/publication/current actor/clock → commit. Không shared upgrade, resource lock mới sau journal hoặc SELECT để suy commit thành công.
- Outcome thuộc attempt, độc lập rollback/close/invalidate. Commit exception luôn uncertain, không DTO/retry; known commit + response lỗi giữ publication/original. Response dùng transaction/proof mới và DTO hiện hành, kể cả revoked/scrubbed sau commit.
- Publish chỉ `pending → listed`, giữ `metadata_only`; không mở thêm quyền file/full-text/RAG, không tạo job/index/provider call. Literal confirmation/checksum và quyền tải/index vẫn kiểm riêng. Proxy/UI có budget riêng, unknown outcome không tự POST lại.

Đã đọc các thay đổi fixture/legacy tests: approval/publish dùng runtime thật; owner corruption/revoke/cleanup chỉ là setup/đối thủ có nhãn. Giữ các assertion ACL/private/cleanup và không nới grants để quan sát lock. Revoke trong race chưa phải triển khai runtime C2d-5c2.

## Reviewer thực chạy

| Kiểm chứng mới tại head review | Kết quả |
| --- | --- |
| Exact selection publication của agent: publish mới, HTTP pending, Chrome, legacy publication/foundation, migration/ORM | **215/215 pass**, 0 fail/error/skip; 736.890 s, 3 warning thư viện/autogenerate |
| Ca concurrent-submit từng có setup error: ba tiến trình riêng, DB mới | **3/3 lượt pass**, mỗi lượt một ca; không chứng minh nguyên nhân đã được tìm/sửa |
| Web test | **64/64 pass**, 0 fail/skip |
| Web typecheck/build | Đạt / đạt |

API có **216 unique**, **218 lượt thực thi** (hai lượt lặp không cộng thành ca mới). **283 source hashes** reviewer trước/sau bốn lượt khớp current source; không sửa source khi suite chạy. Gồm 282 file chung với inventory agent và helper `api/m4d3_same_seed.py`; hash `docker-compose.yml` của agent được đối chiếu riêng, không nằm trong snapshot hash reviewer. Trong inventory 3.923, reviewer **không chạy lại 3.707 unique node**; không tuyên bố pass toàn suite.

Chrome đã chạy qua Uvicorn socket thật và Next production build: form unticked/checksum, metadata success, ordinary User denial và cookie invalid, hash 16 s qua timeout riêng, 504 sau known commit giữ listed/UI unknown/không re-POST. PostgreSQL/MinIO/journal/runtime roles thật; business cutover chỉ fixture scoped, HTTP pending riêng giữ gate thật. Không phải API Compose image/deployment hoặc provider live.

Nhóm API còn có TTL sau SQL/lock wait, storage missing/truncated/same-size corruption/close, 30 MiB hash, slot/deadline/cancellation/teardown, commit landed/rollback/uncertain, publish/revoke hai thứ tự khóa, fresh response sau cleanup và snapshot/journal. 30 MiB và thời gian delay là regression fixture, không benchmark/SLO. Snapshot là PostgreSQL TEMPLATE cô lập, không diễn tập backup/restore Compose mới.

## Artifact agent kiểm riêng

Đã kiểm hash log/JUnit/selection và union sáu group: publication 215, approval 295, writers 220, reads-json 325, originals 330, resources 631. **2.016 unique pass**, 0 fail/error/skip; không thiếu/trùng; collect **3.923**, **1.907 not-run** khớp đầy đủ. **283 source hashes** và before/after artifacts khớp current. Sau code `520ac54` chỉ thay docs; migration/grants không đổi so với base.

Bảy XML mutant mỗi bản có đúng assertion failure, không setup error; node/selection và restored source hash khớp. Web artifacts/hash đã kiểm riêng. Reviewer **không chạy lại 2.016 ca hoặc bảy mutant** và không cộng các số đó vào 216 unique của mình.

## Hai giới hạn evidence còn mở

1. **Setup error writer chưa rõ nguyên nhân:** candidate có 219 pass/1 error ở bootstrap integrity gate trước assertion `test_concurrent_submits_one_open_partial_unique`. Artifact ca riêng và final group 220 pass được xác minh; reviewer thêm ba lượt riêng đều pass. Không tái hiện trong ba lượt này, không đồng nghĩa đã sửa nguyên nhân. Giữ failure lịch sử và follow-up riêng, không xóa bằng rerun xanh.
2. **Baseline container agent đã thay đổi qua gián đoạn:** artifacts trước/resumed khác nhau; 24 container hiện tại trước/sau cleanup agent khớp chính xác. Không đủ evidence khẳng định 24 container ban đầu nguyên trạng xuyên cả task hoặc suy nguyên nhân. Lượt reviewer mới có baseline **9 container**, kiểm riêng trước/sau; không lấy baseline agent làm baseline của reviewer.

Hai giới hạn này không tạo ra một lỗi ứng dụng mới đã tái hiện trong scope POST publish của lượt review; không dùng kết luận scoped để chứng nhận môi trường dev hay toàn ứng dụng.

## Evidence, cleanup và phần chưa kiểm

Reviewer evidence ngoài repo: `%TEMP%\ras-pr48-review-20261009`, gồm setup/runner/commands/logs/JUnit/hash, `verify-agent.py`/`agent-artifact-check.json`, `verify-review.py`/`review-results.json`, web results, cleanup và quality. Agent evidence theo inventory ở `%TEMP%\ras-c2d5c1-20261008`. Lượt chuẩn bị checker của reviewer sửa cách đọc mutant dict, prefix node `api/`, exact tên container trong precheck và giải mã BOM UTF-16 của log web agent; đều ngoài repo, không sửa app, precheck lỗi dừng trước mọi delete.

Conda `C:\Users\ADMIN\.conda\envs\ras`, PostgreSQL/MinIO mới, HTTP ngoài bị chặn, **0 provider call**. Đã kiểm exact ID/name/label/image/port/mount/sole-consumer rồi xóa **2 container/2 volume riêng** và ba file credentials. **9 container có sẵn nguyên ID/image/StartedAt**. Credential sinh cho reviewer không xuất hiện trong evidence; không đọc/sửa `.env`, query/migrate/restart dev hoặc đổi pricing/gate.

Giữ giới hạn overwrite giữa hash và commit: storage chưa immutable/versioned; ca regression cho phép publish thắng khe này, download riêng từ chối checksum sai. Không hứa phát hiện mọi overwrite. Fault injection không chứng minh mất điện/độ bền thiết bị/journal production. Không benchmark, full business cutover, production routing/retention hoặc C3/C4. PR vẫn mở để chủ dự án quyết định merge.
