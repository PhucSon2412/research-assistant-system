# C2d-6 — search, query embedding và rerank qua runtime

**Đã triển khai và merge PR #57 `2e948b7`:** [handoff](../c2d6-search-query-rerank-runtime.md), [inventory](../c2d6-test-inventory.json) và [review](../reviews/pr57-c2d6-search-query-rerank-runtime.md) giữ chứng cứ trước merge. Task kế tiếp [C2d-7 RAG/generation runtime](m6c2b2c2d7-rag-generation-runtime.md) mới giao, chưa triển khai/kiểm. Phần giao C2d-6 bên dưới là lịch sử; safety pending tới C4.

Ngày giao **2026-10-11**, sau khi chủ dự án báo merge PR #56. Đã fetch: `origin/main = 808337dcccfa472eefb05f2c968250948bae9bc2`; head PR #56 `d957872` là ancestor và tree khớp. Source migration **0046**; chưa truy vấn revision DB dev. **Task mới giao, chưa triển khai/kiểm. Safety vẫn pending tới C4.**

## Mục tiêu và tổ chức

Hoàn tất **một PR cho cả luồng**: `POST /search` keyword/hybrid, query embedding, rerank tùy chọn và `GET /search/chunks/{chunk_id}`. Dùng login runtime thật, transaction ngắn, caller/resource proof mới và kế toán có binding. Đây là gói tiếp theo của [kế hoạch MVP](../mvp-delivery-plan.md), ước lượng **2–3 ngày làm việc** theo giả định trong kế hoạch, không deadline hoặc SLO.

Chủ dự án đã ủy quyền lựa chọn hợp lý. Tiến hành triển khai, kiểm, commit/push và mở PR tiếng Việt; không dừng ở khảo sát. Không mặc định tách thành PR theo từng endpoint/helper. Nếu gặp phụ thuộc thật khiến không thể hoàn thành gói, báo chứng cứ và phần thiếu cụ thể, không tự gọi gói đã xong.

## Đọc và xác minh trước khi sửa

- `AGENTS.md`, [mục lục](../README.md), [yêu cầu](../product-requirements.md), [quyền/dữ liệu](../access-and-data.md), [quyết định](../decisions.md), [search/RAG](../search-and-rag.md), [kế hoạch MVP](../mvp-delivery-plan.md).
- [Document index/accounting runtime](../c2d5d2b-document-index-runtime.md), [final read clock](../c2d-final-read-clock.md), [resource reads](../c2d2a-resource-json-reads.md), [caller inventory](../c2d1-caller-inventory.json), [discovery vừa merge](../c2d5f2-discovery-runtime.md).
- Source: `api/app/search.py`, `api/app/api/routes/search.py`, `keyword_query.py`, `authz.py`, `api/deps.py`, final caller helpers, `provider_capacity.py`, `provider_identity.py`, `ingest/embedding.py`, `ingest/document_accounting.py`, model `SearchUsage`/request counters, grants và migrations hiện hành; các call site trong `rag.py`, evaluation và tests; web search/chunk viewer/proxy.
- Fetch lại, xác minh main chứa PR #56, inventory caller/role/commit thực tế; tạo nhánh từ main mới nhất, gợi ý `feat/m6c2b2c2d6-search-query-rerank-runtime`. Không checkout/reset làm mất docs có sẵn.

**Docs bị sót khỏi PR #56:** giữ và **đưa vào commit/PR này** toàn bộ docs review/task/kế hoạch của orchestrator đang modified/untracked. Trong đó có `docs/reviews/pr55-c2d5f1-citation-reconcile-runtime.md`, `docs/tasks/m6c2b2c2d5f2-discovery-runtime.md`, review PR #56 và task đóng gói docs của PR #56. Task đóng gói cũ đã được thay bằng yêu cầu này; không cần mở lại PR #56. Có thể commit docs có sẵn riêng trước code. Kiểm file đích thực sự có trong cây commit, không chỉ tồn tại trên filesystem. Không đưa artifact/journal/credentials vào repo.

## Hiện trạng cần chuyển

`search.py` hiện trộn retrieval với các helper tự commit (`limit_request`, `reserve`, `settle`), heartbeat/reaper và HTTP provider. Session có thể còn transaction khi chờ provider; route/viewer chưa có final caller theo hợp đồng C2 mới. `SearchUsage` còn dùng chung cho `embed_query`, `rerank`, `generation`. `rag.py` gọi `search` và dùng kế toán generation từ đây. Docstring route nói RAG chỉ private đã lệch hành vi M6b-2b3b; kiểm source và sửa docs đúng phạm vi.

Không mặc định coi helper cũ, quyền owner hoặc test fixture là hợp đồng đúng. Đối chiếu hành vi, ghi mâu thuẫn thực sự và lựa chọn vào docs hợp đồng.

## Yêu cầu triển khai

### 1. Boundary search và mở chunk

- Route lấy session/principal/CSRF qua dependency chuẩn. POST giữ kiểm Origin/CSRF; caller hiện hành phải chứng minh owner, credential/status latest head/receipt, session generation/expiry và role bằng helper C2, không dùng role hoặc `now` từ đầu request cho lần kiểm cuối.
- Dùng engine/runtime guard thật; kiểm từng quyền cần thiết. Không fallback owner/recovery hoặc cấp DDL/TEMP/raw auth writes để chạy được. Sai login/grant/gate dừng trước mutation/provider.
- Tách các checkpoint ngắn: entry/scope và request limit → admission query nếu cần → retrieval/hydrate → admission rerank nếu bật → final response. Proof/payload/cost DTO được detach, không lazy ORM khi đã đóng Session. Luôn thử rollback và close; lỗi release phải invalidate và dừng với JSON lỗi ổn định.
- Không giữ connection, transaction, khóa hàng/capacity/journal xuyên HTTP, chờ thread, sleep hoặc serialize response. Heartbeat dùng transaction ngắn độc lập. Trước gửi provider, admission/proof phải known commit và release sạch. Không báo thành công sau release lỗi.
- Lần cuối lấy clock **sau SQL cuối và chờ journal**; kiểm current caller và scope/resource ngay tại boundary. Áp cả kết quả rỗng, không có terms, chỉ degraded, lỗi nghiệp vụ có dữ liệu và viewer. Không dùng principal hoặc DTO trước commit thay proof mới khi đã có mutation commit.
- Khóa theo thứ tự tương thích với collection/revoke/document accounting, UUID có thứ tự khi nhiều item; journal sau khóa resource/capacity cần thiết. Không lấy User/item sau journal. Không giữ khóa qua provider để ngăn revoke. Ghi sơ đồ ngắn thứ tự khóa thực tế, tránh tạo vòng với writer/cleanup.

### 2. Giữ nguyên quyền và retrieval

- Lọc private owner, collection owner/membership, `indexed`, active generation, đúng version/file/checksum/epoch và common rights **trong SQL trước count/top-k** cho keyword lẫn vector. Collection chỉ thu hẹp quyền. Item ngoài quyền vẫn cùng `404`; không đổi policy sang 200 rỗng.
- Giữ keyword query builder M4d-1, model/dimension compatibility, RRF, giới hạn/config và thứ tự degraded rerank hiện hành. Không tuning recall, thay model, reindex/ANN hoặc đổi corpus/nhãn trong gói này.
- Hydrate/recheck exact tuple trước payload rerank và trước response; common chỉ search được không đồng nghĩa được gửi provider. Chỉ `provider_ok` còn hiệu lực được gửi; giữ `rerank_withheld`/trạng thái degraded và thứ tự nhóm chưa rerank. Không gửi reference list, discovery metadata hoặc private khác owner.
- Viewer kiểm lại exact active chunk, locator/page/line/offset và quyền nguồn; trái quyền/stale trả `404`. `original_url` lấy từ gate tải file độc lập, không suy từ quyền search hoặc tự dựng link.
- Caller mất hiệu lực thì dùng lỗi auth hiện hành; scope/rights/membership/generation đổi trong attempt thì dừng bằng lỗi ổn định, không trả hit cũ hoặc tự chạy lại cả search. Response đã qua final boundary vẫn có thể tới client sau khi nhả khóa; ghi đúng giới hạn, không hứa thu hồi bytes đã gửi.

### 3. Query/rerank admission, lease và settlement

- Chuyển request counter, reservation, heartbeat, reaper và settlement cần cho query/rerank sang checkpoint runtime. Tận dụng hợp đồng `document_accounting`; không gọi legacy helper tự commit hoặc broad operator từ route mới.
- Reservation có nonce/binding kiểm được cho đúng usage, User/session hoặc request operation, task, model/provider, ngày, ceiling và lease. Không nhận một usage ID tùy ý để renew/settle/adopt. Nếu cần schema/routine hẹp, thêm revision sau head thực tế (dự kiến `0047`), giữ revision đã merge và ORM/schema khớp; không yêu cầu migration nếu source hiện có đã đủ.
- Giữ document + query dùng chung concurrent embedding capacity và app/embedding/User totals; rerank có bucket/quota/concurrency riêng. Tính committed + reserved + uncertain theo hợp đồng hiện hành, cả cảnh báo, ngày cũ và common document owner NULL. Không đổi ngân sách/chính sách kế toán để test qua. Generation còn lại không được bỏ khỏi app total.
- Reservation hết lease giải phóng slot nhưng giữ tiền dự phòng; heartbeat muộn không hồi sinh lease đã hết. Late financial settlement có thể đối soát sau caller/revoke/gate nội dung đổi, theo boundary tài chính đã chứng minh; không cấp lại quyền trả text, không chạy provider hoặc ghi content. Settlement lặp không double charge, không ghi vector query/payload vào ledger/log.
- Outcome commit độc lập rollback/close. Commit exception luôn uncertain: dừng attempt, không SELECT đoán kết quả hoặc tự retry. Admission không chắc thì không gọi provider; kết quả provider chưa chắc giữ ceiling và không gửi lại. Lỗi financial checkpoint không được biến thành degraded search thành công để tiếp tục provider/response. Chỉ các lỗi provider/ngân sách đã phân loại và biết kết quả mới theo degraded contract hiện hành.
- Kiểm adapter đúng model/provider, vector finite/dimension, rerank mapping/results và usage/ceiling. Usage × giá cấu hình vẫn chỉ là estimate; không gọi đó là charge thật. Cancellation/timeout không chứng minh provider chưa nhận; giữ reservation/ownership đúng, kết thúc heartbeat/release tài nguyên, không tự gửi lần hai.

### 4. Tương thích RAG và shared callers

- Inventory mọi call site của `search`, scope/hydrate, counter/reserve/settle/reaper/heartbeat, gồm RAG/evaluation/worker. Giữ hành vi/cờ common/external và callable contract cho task RAG tiếp theo; không để refactor làm mất common RAG hoặc bỏ generation khỏi budget.
- HTTP search/viewer đã chuyển không được rơi lại nhánh legacy. Nếu cần giữ compatibility cho RAG chưa chuyển, cô lập và ghi tên call site/hợp đồng còn mở; không chứng nhận RAG runtime. Shared query financial reaper phải tiếp tục tương thích document accounting; generation cũ không được adopt vào nonce mới hoặc tăng grant chỉ để mở đường cũ.
- Gói RAG/generation tiếp theo xử lý final evidence/citation, generation admission và response toàn luồng. Không thêm streaming, đổi evidence pack hoặc semantic claim policy trong PR này.

### 5. Web và pending gate

- Search page dùng proxy same-origin; rerank tắt mặc định, giữ thông báo rõ payload gửi provider. Hiển thị keyword/hybrid/degraded/withheld/lỗi một cách đúng; không retry paid request khi outcome không rõ. Timeout UI không được nói chắc chưa tốn phí.
- Không thay timeout/allowlist nếu không có lý do và test trực tiếp; giữ route tải original streaming riêng. Keyword không gọi provider. Thiếu key/pricing giữ hành vi unavailable/degraded hiện hành, không bật giá để demo.
- Production safety **vẫn pending**: HTTP thật chưa cutover trả 503, health sống/readiness chưa ready. Business tests dùng fixture đã provision receipt/generation và role runtime thật trong môi trường cô lập theo cách C2 hiện có; ghi rõ fixture pending khi cần. Không thêm bypass production, biến môi trường “test mode” hoặc đổi expectation thành 503 để gọi nghiệp vụ đạt.

## Kiểm chứng đủ cho gói

Chọn nhóm kiểm theo rủi ro; không mặc định chạy lại hàng nghìn test không liên quan mỗi PR. Full inventory/hồi quy tổng trước phát hành vẫn thuộc gói B/C4. Sửa fixture/debt search trực tiếp cản kiểm luồng này, giữ expectation nghiệp vụ; debt còn lại liệt kê theo node, không xóa hoặc làm yếu test để đạt số.

1. PostgreSQL thật, login runtime thật: keyword/hybrid/rerank bật/tắt, viewer, request limits và no-terms; A/B/Admin, private trùng checksum, common search-only/external/download, scope item/collection/all và common revoked đứng đầu với top-k=1. Kiểm payload/response không lộ nội dung ngoài quyền.
2. Race tất định: session idle/absolute đúng biên sau SQL/journal wait; disable/logout/role hoặc generation/rights/epoch/membership đổi trước query/rerank/final response; revoke commit được trong lúc provider bị chặn. Trường hợp không có hit cũng kiểm caller.
3. Concurrent document/query/rerank admission, combined budget/warning, expired lease/ngày cũ, heartbeat lỗi, response malformed/identity/dimension/usage sai, timeout/cancel, duplicate/late settle; nonce sai bị chặn. Không double charge hoặc provider resend.
4. Fault commit/release tại counter/reserve/pre-send/settle/final proof: JSON lỗi ổn định, không leak pool/transaction; uncertain không provider/content bước sau. Quan sát connection checkout trong lúc fake HTTP bị chặn, không chỉ mock việc commit.
5. Hồi quy search/keyword/common/citations viewer, document accounting/shared capacity, RAG compatibility và migration/ORM nếu đổi schema. Vài countercheck có mục tiêu ACL trước top-k, final caller/clock, pre-send proof, capacity/nonce/late settlement; mutant phải fail assertion cần bảo vệ, rồi khôi phục source trước lượt cuối.
6. Chrome thật qua proxy trên project cô lập: keyword → hit → viewer, hybrid và rerank với transport giả → kết quả, A/B/Admin denial, thông báo unavailable/uncertain không tự gửi lại. Kiểm pending HTTP riêng với gate thật. Không dùng browser chỉ để kiểm HTML build.

Chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước Python. Credentials/journal/artifact ngoài repo, project/port/volume riêng. Không đọc/sửa `.env`, query/migrate/restart dev, gọi arXiv/OpenRouter thật hoặc đổi `pricing_verified`. Chụp baseline và dọn đúng tài nguyên tự tạo; drift có sẵn báo theo bằng chứng, không suy nguyên nhân.

## Hoàn thành và báo cáo

- Một PR tiếng Việt, không merge, không AI attribution. Code mới đi hết runtime search/query/rerank/viewer, không còn fallback legacy trên các entry đã chuyển.
- Handoff/inventory ngắn: base/head/source kiểm, caller/role/lock/binding map, lỗi đã tái hiện/sửa, exact selection/JUnit và not-run/debt, source hash trước/sau. Không sửa code trong suite cuối; không cộng pass lịch sử vào lượt mới. Lưu evidence ngoài repo, giữ riêng lỗi chuẩn bị/weak mutant.
- Cập nhật [kế hoạch MVP](../mvp-delivery-plan.md) và hợp đồng thực sự bị đổi; đầu docs chỉ trạng thái ngắn/link, không sao chép toàn lịch sử kiểm. Kiểm liên kết trong **cây Git đã commit**, docs có sẵn đã tracked và `git diff --check`.
- Báo kết quả/giới hạn đúng phạm vi; provider giả không chứng minh charge, quality hoặc p95. Safety pending C4; RAG/generation → C3/hồi quy/C4 → private deletion/restore → provider/chất lượng → vận hành nhóm thử theo kế hoạch.
