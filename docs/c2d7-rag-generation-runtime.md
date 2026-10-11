# C2d-7 — RAG/generation qua runtime

Triển khai trên nhánh `feat/m6c2b2c2d7-rag-generation-runtime`, **chưa merge**. Base đã fetch và kiểm PR #57 MERGED `2e948b78157bf4d498bdca4ea52ca81dd85c15c1`; head `44f1da1` là ancestor và tree khớp. Source **0047 → 0048**; không query revision DB dev. **Safety pending tới C4**.

[Task](tasks/m6c2b2c2d7-rag-generation-runtime.md) · [Inventory](c2d7-test-inventory.json) · [Kế hoạch MVP](mvp-delivery-plan.md) · [C2d-6](c2d6-search-query-rerank-runtime.md)

## Caller, login và transaction

| Caller | Boundary hiện hành |
| --- | --- |
| `POST /rag/ask` | Session/CSRF/Origin dependency chuẩn → release Session → `rag_runtime.answer` với runtime engine, clock và CSRF |
| Direct `rag.answer` | Wrapper release rồi đi cùng `rag_runtime.answer`; không còn orchestration legacy |
| Scope/pricing/counter/pack/admission/pre-send/response | Session mới mỗi checkpoint, guard actual runtime login + từng grant, recovery/safety thật; actor/session/head/receipt/generation/current role và TTL mới |
| `rag_runtime.retrieve` | Interface nội bộ tới retrieval C2d-6; một search counter, không HTTP lồng, không rerank hoặc generation reservation trùng |
| Generation renew/reap/settle | `search_accounting` theo frozen financial proof; không đòi caller/content còn sống và không cấp quyền nội dung |
| Citation `/sources/{chunk}` | Web viewer gọi `/search/chunks/{chunk}` qua runtime C2d-6; original URL chỉ từ download gate riêng |
| Evaluation | Capture `rag_runtime.retrieve` và `rag.generate` ở call site thật; HTTP search không bị tính thành RAG capture; network deny giữ nguyên |

Không owner/migrator/recovery fallback hoặc grants mới. Guard runtime/grants được kiểm trong **mỗi checkpoint** shared search/RAG/financial. Chỉ DTO scalar/bất biến qua pha; scope/evidence RAG được freeze sâu. Rollback và close đều được thử; release lỗi invalidate và dừng. Commit exception luôn uncertain, không SELECT đoán landed, tự retry hay provider/answer tiếp. Known commit rồi teardown lỗi giữ mutation/reservation và trả lỗi.

Thứ tự khóa:

```text
counter: User → requested item → collection → request-day → flush → journal exclusive
admission: User → items UUID → collection → shared capacity → usage/FK → flush → journal exclusive
pre-send: items UUID → collection → usage SHARE → journal shared
read/pack/response/viewer: items UUID → collection → journal shared
financial: shared capacity → usage UUID → flush/constraints → journal exclusive
```

Không lấy khóa hàng mới sau journal. Pre-send khóa ledger trước journal để nonce/status ổn định tới release; resource proof mới ở transaction **sau** admission known commit và clean release. Không Session/connection/lock qua HTTP, heartbeat join hoặc serialize response. Counter giữ chính sách cũ: pricing generation chưa khả dụng không đếm; mỗi attempt hợp lệ đếm một RAG và một search request, kể cả no-terms/empty. Clock dùng sau SQL cuối/journal wait, cả nhánh answered/insufficient/pricing unavailable; day counter/admission đổi giữa checkpoint thì dừng.

## Evidence và response

Giữ ranking, keyword builder, RRF, pack M4d-3, cấu hình paper/chunk/byte, phân giải title/alias/ambiguity và kiểm claim so sánh đúng hai phía. Item/collection/all × keyword/hybrid đi cùng runtime retrieval, SQL ACL trước top-k và opt-in common ở scope/rank/hydrate. Private chỉ owner, Admin không bypass. Scope ban đầu trái quyền vẫn 404; sau khi đã quan sát, thay đổi scope/binding/rights/membership trả `409 rag_scope_changed`. Caller hết hiệu lực giữ lỗi auth, không biến thành lỗi scope.

Snapshot gồm active tuple, checksum/size/epoch, model/dimension/provider/pipeline/index/coverage, live common rights/publication binding và membership. Hydrate mọi hit; exact title/text/file/version/locator/coverage/visibility/provider_ok được kiểm trước pack và so lại trước external dispatch/response. Common search-only giữ `evidence_withheld`, không pack/generation/citation. Không evidence hoặc thiếu hai phía so sánh thì không reserve/gọi generation. Bibliography/reference/discovery metadata không là content evidence; paper text vẫn không tin cậy.

Quote exact, locator và claim structure giữ nguyên; không tuyên bố đó là kiểm chứng ngữ nghĩa. Download URL lấy dưới final resource proof qua download gate riêng. Final caller/scope lỗi không trả answer/citation/quote. Response đã qua final proof có thể tới client sau release và revoke tiếp theo; payload/bytes đã gửi không lấy lại được.

## Binding và kế toán

`0048` chỉ mở CHECK của `search_usage` cho binding generation đầy đủ; không sửa `0047`, không backfill/adopt lịch sử all-NULL, không thêm grant. Nonce unique toàn ledger; partial NULL bị DB từ chối. Downgrade từ chối khi đã có generation binding, giữ nguyên query/rerank binding và dữ liệu lịch sử.

Proof bind usage/User/session/operation/nonce/task/model/provider/day/ceiling. Generation entry không nhận proof query/rerank/document. Shared capacity serialize application total (document + query + rerank + generation), generation bucket/User quota/warning và concurrency hiện hành. Common generation tính cho người hỏi; document common owner NULL và quota không đổi. Không giảm ceiling để test qua; không bật pricing môi trường.

Generation usage hợp lệ tính cả input/output theo giá **ước tính** cấu hình, trong ceiling. Adapter thật kiểm model/selected provider/shape/usage/response size, nested malformed/bool/overflow và transport timeout. Definite rejection theo hợp đồng cũ; timeout/malformed/identity/usage không tin cậy giữ uncertain và ceiling. Không suy charge=0 từ usage vắng. Financial commit/release/heartbeat lỗi không thành answered/degraded success. Pre-send bị chặn giữ reservation bảo thủ tới expiry/đối soát.

Lease hết giải slot và giữ ceiling, không renew hồi sinh. Late/duplicate settlement đúng nonce tối đa một lần trên ngày gốc dù logout/reset/disable/revoke hoặc caller expiry; không cấp lại content authority. Safety/integrity/hold vẫn đóng financial checkpoint. Provider không resend; thread stop/join ở mọi đường, cancellation giữ ceiling. Client disconnect/proxy timeout không chứng minh provider/FastAPI đã dừng; không hứa exactly-once provider.

## Compatibility và thay đổi test

Bỏ `rag._limit_request`, `_fresh`, `_checked_hits`, `_settle`, legacy `search.search`/`reserve('generation')` và `provider_capacity.reservation_heartbeat` khỏi luồng RAG. `rag.answer` chỉ wrapper vào boundary mới. Legacy search/reserve/lease helper còn cho các direct compatibility test ngoài HTTP; không dùng để chứng nhận RAG hoặc làm fallback.

Hook test chuyển tới internal retrieval/checkpoint thật, giữ ACL/pack/citation assertion. Pre-send denial trước khi gửi nay giữ reserved ceiling thay vì legacy rejected/no-charge. Test CHECK C2d-6 cập nhật theo migration mới cho phép generation, vẫn kiểm partial/duplicate nonce độc lập; generation entry kiểm không nhận query proof. Không chấm/tuning holdout hoặc đổi `pending_human_review`.

Ma trận common nhiều attempt độc lập phải đi qua expiry giữa các hàng: fixture tăng clock và gọi reaper thật, kiểm row thành uncertain với ceiling còn giữ. Không tăng concurrency, xóa reservation hoặc đổi pre-send denial thành rejected để làm test xanh. Lượt seal đầu phát hiện thiếu bước này được giữ riêng trong inventory, không gọi là lỗi baseline.

Hook revoke trước rerank ở common search chuyển tiếp keyword arguments của checkpoint (`domain`) mới; assertion 409/no payload/reservation bảo thủ giữ nguyên. Lượt seal thứ hai phát hiện hook cũ được giữ riêng; không gộp số pass của nó vào seal cuối. Selection không đổi, Chrome chạy riêng rồi tối đa ba pytest process với DB/bucket/journal độc lập.

## Kiểm chứng và giới hạn

Kết quả seal cuối, source SHA/hashes trước/sau, exact selection/JUnit/pass/fail/error/skip/not-run và countercheck ở inventory. Các lượt chuẩn bị giữ riêng, không cộng pass lịch sử. Hồi quy chọn theo rủi ro gồm RAG/common/pack/comparison, search/keyword/viewer/query/rerank, document shared capacity, migration/ORM/config và evaluation capture/validation.

Chrome cài sẵn chạy bằng harness trong Conda khi connector không có browser: Next proxy + uvicorn + PostgreSQL restricted login + journal thật, transport giả qua adapter thật. Ba scope, hybrid, citation viewer, B/Admin private denial, common, actual HTTP revoke trong generation, pricing/uncertain không tự POST lại; pending HTTP kiểm riêng route 503/health 200/ready 503. Scoped business cutover chỉ trong fixture, không sửa production pending.

Môi trường: Conda `C:\Users\ADMIN\.conda\envs\ras`, context ngoài repo `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d7-20261011`, project `rasc2d7452d8b7c`, port loopback riêng. Không đọc/sửa `.env`, query/migrate/restart dev, gọi OpenRouter/arXiv thật hoặc bật pricing. Web source smoke copy từ Git paths ra ngoài repo, không copy `.env`. Baseline container metadata được chụp trước seal; không suy là baseline trước toàn task. Cleanup chỉ tài nguyên task, đối chiếu ID/image/mount/label; health/restart tự nhiên được báo riêng.

Provider giả không chứng minh charge/routing/retention, chất lượng grounded/citation, p95, mất điện, restore/cutover hoặc readiness production. Scope snapshot chưa benchmark corpus lớn. Gói A hoàn tất phần code RAG sau review; **C3 + hồi quy/debt/tích hợp C4**, private deletion/restore, provider/chất lượng và vận hành nhóm thử còn mở. Không tự tách thêm gói runtime vi mô.

Lệnh đã chạy và kết quả cuối sẽ ghi trong inventory; không dùng lệnh chưa chạy làm bằng chứng.
