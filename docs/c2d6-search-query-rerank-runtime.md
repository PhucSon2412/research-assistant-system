# C2d-6 — search/query embedding/rerank runtime

**Cập nhật sau review:** PR #57 đã merge `2e948b78157bf4d498bdca4ea52ca81dd85c15c1`, tree khớp head `44f1da1`; chưa rerun tại merge. [Review độc lập](reviews/pr57-c2d6-search-query-rerank-runtime.md) giữ chứng cứ riêng. Giao [C2d-7 RAG/generation runtime](tasks/m6c2b2c2d7-rag-generation-runtime.md), mới giao, chưa triển khai/kiểm; safety vẫn pending C4. Nội dung kiểm chứng bên dưới giữ source/lượt trước merge.

Nhánh `feat/m6c2b2c2d6-search-query-rerank-runtime`, base PR #56 merge `808337dcccfa472eefb05f2c968250948bae9bc2` (đã fetch và kiểm tree `a5d0e02b`, khớp head `d957872`). Source **0046 → 0047**; không query revision DB dev. **Safety pending tới C4**, RAG/generation runtime còn riêng. [Task](tasks/m6c2b2c2d6-search-query-rerank-runtime.md) · [Inventory](c2d6-test-inventory.json) · [Kế hoạch MVP](mvp-delivery-plan.md).

## Caller và transaction

| Caller | Boundary hiện hành |
| --- | --- |
| `POST /search` | Dependency session/CSRF/Origin chuẩn → release dependency Session → `search_runtime.search` |
| `GET /search/chunks/{chunk_id}` | Dependency session chuẩn → release → `search_runtime.open_chunk` |
| Counter/admission/pre-send/retrieval/final/viewer | Session mới mỗi checkpoint, login runtime thật + từng grant cần thiết; actor hoặc `final_caller`, receipt/head/session generation/role/TTL mới |
| Query/rerank renew/reap/settle | `search_accounting`, authority tài chính theo nonce/tuple, không đọc/ghi content hoặc gia hạn caller |
| `rag.ask` | Giữ `search.search`, `_scope`, `hydrate`, `_lock_hits`, `original_urls`, `reserve('generation')`, `provider_capacity.reservation_heartbeat`, `rag._settle` legacy; mọi opt-in common/external giữ nguyên |
| Evaluation | `evaluation.runner.instrumented` bọc `rag.search`; vector-only `_vector_record` gọi `_rank`/`hydrate` private mặc định. HTTP evaluation đi route mới. Không chứng nhận evaluation/provider/chất lượng |
| Document worker | `document_index`/`document_accounting` giữ boundary riêng; cùng capacity advisory và app/bucket/User totals |
| Compatibility reaper/renew/settle | Legacy expiry/renew/settle không được nhận nonce mới. Shared reaper chuyển expiry nonce query sang boundary runtime riêng; generation không adopt nonce query |

Entry dùng guard runtime hiện hành (login không owner/recovery/superuser/membership/DDL/TEMP/raw auth/journal writes) và kiểm riêng từng SELECT/INSERT/UPDATE cần cho search. Không thêm grants. Mỗi checkpoint rollback **và** close; release lỗi invalidate và dừng. Commit exception luôn `search_commit_uncertain`, không đọc lại đoán landed, không provider/response bước sau hoặc tự retry. Provider/DTO exception không phân loại trả `search_checkpoint_unavailable`, giữ ceiling. Session/connection không đi qua HTTP, heartbeat join hoặc serialization FastAPI.

Thứ tự khóa thực tế:

```text
counter: User → requested item (nếu có) → collection → request-day → flush → journal exclusive
admission: User → items UUID → collection → shared capacity → usage/FK flush → journal exclusive
read/pre-send/response/viewer: items UUID → collection → journal shared
financial: shared capacity → usage UUID → flush/constraints → journal exclusive
```

Không lấy khóa User/item/capacity mới sau journal. Collection writer hiện hành cũng item → collection. Query không lấy item sau capacity; financial không lấy item/User. Không chờ provider để giữ revoke. Caller/resource SQL cuối và mọi journal wait hoàn tất trước clock cuối; áp cả empty/no-terms/degraded và viewer. Writer proof trước commit không thay proof transaction mới sau commit. Pre-send lại kiểm nonce/status/lease và caller/resource, release sạch rồi mới gửi.

## Quyền và retrieval

Giữ builder keyword M4d-1, SQL ACL trước top-k cho cả keyword/vector, active tuple, dimension/model compatibility, RRF, config và ranking nhóm withheld. Scope snapshot gồm tuple item/epoch/visibility/owner/generation/version/file/checksum/size/model/dimension/provider/pipeline/index-version/coverage/external và membership; so lại tại mỗi pha kể cả không có hit. Scope đổi giữa attempt trả `409 search_scope_changed`, không tự chạy lại search. Ban đầu item/collection trái quyền vẫn `404`; viewer stale/trái quyền `404`; caller mất hiệu lực vẫn lỗi auth hiện hành.

Hydrate exact tuple và toàn bộ scalar text/locator/visibility/provider_ok được so trước rerank và response. Chỉ chunk `provider_ok` được gửi; common search-only được withheld và giữ thứ tự nhóm chưa rerank. Download URL luôn từ `original_urls` với gate riêng dưới khóa response, không suy từ search. Không references/discovery metadata/private khác owner vào payload. Response đã qua boundary có thể tới client sau release và sau revoke kế tiếp; không hứa thu hồi bytes đã gửi.

Đổi expectation cũ ở test scope race: 200 rỗng → 409 theo task. Hai corruption setup common chuyển sang owner fixture có nhãn; HTTP vẫn runtime thật. Các hook race reserve/response cũ chuyển sang checkpoint mới, không xóa assertion quyền. Keyword/RAG import fixture `installation` còn thiếu được bổ sung, giữ kỳ vọng nghiệp vụ. Test shared document/query đồng bộ clock fixture để lease dùng cùng mốc (trước đó worker dùng giờ thật còn HTTP dùng 2026-10-03).

## Reservation, budget và outcome

`0047` thêm `reservation_token`, `operation_id`, `session_id` nullable trên `search_usage`; all-NULL dành lịch sử/generation, binding mới phải đủ và chỉ query/rerank. Token unique, downgrade từ chối khi binding đã tồn tại. Session ID không FK: financial ownership không mất khi session bị dọn. Không lưu câu hỏi/vector/payload trong ledger.

Proof frozen bind usage/User/session/operation/nonce/task/model/provider/day/ceiling. Không nhận usage ID tùy ý để renew/settle. Lease có clock mới sau wait/SQL, heartbeat muộn không hồi sinh. Expiry batch hữu hạn giải slot và giữ ceiling; không debit ngân sách lần nữa. Late/duplicate settlement đúng nonce trên ngày gốc sau expiry/disable/revoke/logout không cấp quyền content. Safety/integrity/hold vẫn chặn financial checkpoint; không bypass recovery.

Giữ document + query concurrency, app total gồm generation, document owner NULL và private User quota; rerank bucket/quota/concurrency riêng. Committed + reserved + uncertain và warning fractions giữ nguyên. Admission dùng ceiling cấu hình, không giảm để test qua. Usage × giá chỉ **ước tính**, không charge thực. Adapter kiểm model/provider, finite vector/dimension, usage/ceiling và mapping rerank; boolean/overflow numeric bị từ chối.

Financial checkpoint lỗi không bị bắt thành degraded success. Chỉ 429 budget/concurrency đã biết và provider failure đã settlement known theo hợp đồng rerank cũ mới degraded. Admission/pre-send bị chặn giữ reservation bảo thủ (chưa gửi), không tự giải để suy charge=0. Timeout/cancellation giữ ownership/ceiling; heartbeat được stop/join trước kết thúc attempt. Không provider resend. Query commit/release/heartbeat lỗi có JSON ổn định, UI nêu chi phí có thể chưa rõ; proxy timeout/allowlist không đổi.

## Kiểm chứng và giới hạn

Seal `sealed-1` trên code/test **`5da2cd38dbade63dd34451157931e1491eeba498`** (production code `c41218b`): **336 unique pass** gồm 335 API/schema và một smoke Chrome, **0 fail/error/skip**, exit 0. Full collect **4.900**, **4.564 not-run** theo exact node ID; **394 source hashes** trước/sau trùng, không sửa source trong suite.

| Nhóm seal | Pass |
| --- | ---: |
| Runtime/fault/race/binding | 71 |
| Search/keyword | 42 |
| Common rights/viewer/revoke | 12 |
| Document accounting/shared capacity | 111 |
| RAG compatibility | 11 |
| Migration/ORM/config | 88 |
| Chrome qua Next proxy + API thật | 1 |

Countercheck `counterchecks-2`: **7/7** fail assertion cần bảo vệ rồi restore byte-for-byte (ACL trước top-k, final clock, pre-send, nonce, late settle, capacity, scope error). Lượt đầu có năm control hợp lệ rồi runner dừng vì target ACL khớp hai vị trí; giữ riêng, không cộng. Mutant scope làm mất mã 409 ổn định (ra 503), không được mô tả là đã chứng minh leak text. Web: typecheck/build exit 0, **70 test pass**. Faulthandler 60 giây in diagnostic trong ma trận common dài; nhóm hoàn tất 12/12, không là pytest timeout/failure.

Kết quả cuối, exact selection/JUnit/not-run, manifests trước/sau, countercheck và các lượt chuẩn bị được ghi riêng trong [inventory](c2d6-test-inventory.json). Chỉ seal cuối được tính unique pass. Chrome dùng bản cài sẵn qua Playwright trong Conda vì connector Chrome không khả dụng; chạy Next proxy thật + uvicorn + runtime PostgreSQL + journal, transport provider giả. Web source được copy từ tracked paths sang thư mục ngoài repo, không copy `.env`; process/DB/bucket task được dọn.

Conda ras, project/port/credentials/journal/evidence ngoài repo. Không đọc/sửa `.env`, không thao tác dev, không provider/arXiv thật, không bật pricing môi trường. Chỉ settings copy gắn MockTransport trong fixture có pricing để kiểm nghiệp vụ. Baseline dev API unhealthy và worker/beat restarting là quan sát có sẵn, không suy nguyên nhân hoặc coi đã sửa.

Provider giả không chứng minh charge, quality, p95 hoặc readiness production. Scope snapshot đọc metadata tuple của audience hiện hành, chưa benchmark corpus lớn; không ANN/reindex/tuning. RAG legacy vẫn giữ transaction/provider orchestration cũ, chưa được chứng nhận runtime. C3/C4, private deletion/restore, provider thật và nghiệm thu nhóm thử theo kế hoạch còn mở.

## Lệnh và evidence

Interpreter đã kiểm là Conda ras; context thực chạy `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d6-20261011`, project `rasc2d63c5bc924`, port động loopback. Các lệnh đã chạy từ repo:

```powershell
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py scripts/c2d6_test_host.py --context "$env:LOCALAPPDATA\Temp\ras-c2d6-20261011" setup
& $py scripts/c2d6_counterchecks.py --context "$env:LOCALAPPDATA\Temp\ras-c2d6-20261011" --out "$env:LOCALAPPDATA\Temp\ras-c2d6-20261011\counterchecks-2"
& $py scripts/c2d6_seal.py --context "$env:LOCALAPPDATA\Temp\ras-c2d6-20261011" --name sealed-1
npm run typecheck --prefix web
npm test --prefix web
npm run build --prefix web
```

Không chạy lại `setup` trên context đã có credentials; lần tái lập dùng thư mục ngoài repo mới. `sealed-1` có full collection, exact selection/JUnit cho từng nhóm và source manifests trước/sau; các file này được hash trong inventory. Lượt chuẩn bị, lỗi fixture/hook/Chrome harness và runner countercheck lần đầu giữ riêng, không cộng vào seal cuối. Typecheck exit 0 được ghi trong tool transcript; web test/build có log ngoài repo.

Cleanup đã kiểm đúng label/ID project task rồi `down --volumes`: không còn task container/network/volume. Snapshot sau giữ đủ container/volume baseline và không đổi ID/image/mount/label; status restart/health có sẵn chỉ là quan sát. Evidence/JUnit/manifests ngoài repo được giữ, **89 artifact hashes** trong inventory. Không đặt credentials, journal, paper hay artifact vào commit.
