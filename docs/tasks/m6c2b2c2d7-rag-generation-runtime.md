# C2d-7 — RAG và generation qua runtime

**Cập nhật triển khai:** toàn luồng trên nhánh review, source `0048`, chưa merge; [handoff](../c2d7-rag-generation-runtime.md) và [inventory](../c2d7-test-inventory.json) giữ kết quả theo source. Safety pending C4. Mốc giao và yêu cầu gốc bên dưới giữ lịch sử.

Ngày giao **2026-10-11**, sau khi chủ dự án xác nhận merge PR #57. Đã fetch và kiểm PR MERGED tại `2e948b78157bf4d498bdca4ea52ca81dd85c15c1`; head review `44f1da11b5d47f1384b8cc07d5f831662b43d2aa` là ancestor, tree khớp. Source migration **0047**, không suy revision DB dev. **Mới giao tài liệu, chưa triển khai/kiểm C2d-7. Safety vẫn pending tới C4.**

## Mục tiêu và phạm vi

Hoàn tất **một PR cho cả luồng** `POST /rag/ask`: scope item/collection/all, retrieval keyword/hybrid bằng runtime, evidence pack, generation admission/pre-send/heartbeat/settlement và response có citation. Dùng login runtime thật và transaction ngắn. Đây là phần cuối gói A của [kế hoạch MVP](../mvp-delivery-plan.md); sau đó chuyển sang operator/C3, hồi quy và tích hợp C4. Không mặc định tách tiếp theo endpoint hoặc từng pha kế toán.

Giữ API, ranking, pack, model và quota hiện hành. Không thêm streaming, tóm tắt tự động, OCR, tuning retrieval/claim, provider mới hoặc chức năng ngoài checklist MVP. C3/C4, private deletion/restore, provider thật/charge/routing/retention và vận hành triển khai vẫn ở các gói sau. Nếu phát hiện phụ thuộc thật làm scope không thể review trong một PR, nêu bằng chứng và đề xuất ranh giới cụ thể; không dùng compatibility để báo cả luồng đã chuyển.

### Đọc và chuẩn bị

- Đọc [mục lục](../README.md), [product requirements](../product-requirements.md), [access/data](../access-and-data.md), [search/RAG](../search-and-rag.md), [quyết định](../decisions.md), [kế hoạch MVP](../mvp-delivery-plan.md).
- Đọc [handoff C2d-6](../c2d6-search-query-rerank-runtime.md), [review PR #57](../reviews/pr57-c2d6-search-query-rerank-runtime.md), [document index/accounting](../c2d5d2b-document-index-runtime.md), [final read clock](../c2d-final-read-clock.md), [auth restore](../auth-restore-safety.md) và caller map hiện hành.
- Fetch lại, xác minh base/head migration thực tế; tạo `feat/m6c2b2c2d7-rag-generation-runtime` từ `origin/main`. **Giữ và đưa vào commit/PR mọi docs orchestrator modified/untracked**, gồm review PR #57, task này và cập nhật kế hoạch. Không reset/clean/stash làm mất chúng; không lẫn thay đổi ứng dụng ngoài task.
- Lập map entry/caller, DB login, helper tự commit, provider dispatch, lease/reaper/settle và evaluation hooks trước sửa. Ghi rõ các chỗ legacy được bỏ/thay và nơi compatibility còn dùng, không chỉ quét tên hàm.

## Hiện trạng đã đối chiếu

`api/app/api/routes/rag.py` còn truyền Session của dependency thẳng vào `app.rag.answer`. `rag.py` còn dùng `search.search`, `reserve('generation')`, `_limit_request`, `_fresh`, `_settle`, `provider_capacity.reservation_heartbeat` và `db.get_bind()` trong cùng orchestration; có các nhánh empty/insufficient trả sớm. Không suy chúng đã runtime chỉ vì HTTP search/viewer được chuyển ở PR #57.

C2d-6 đã có `search_runtime`, `search_boundary`, `search_accounting`: query/rerank nonce + frozen proof, pre-send transaction mới sau admission commit, resource/caller/clock và financial checkpoints. Revision `0047` chỉ cho binding mới trên task query/rerank; generation lịch sử còn all-NULL. `evaluation.runner.instrumented` đang bọc `rag.search`/`rag.generate`; đổi orchestration phải chuyển các hook thực sự được gọi.

Đây là khảo sát source, không phải kết quả chạy C2d-7. Không gọi các lỗi tìm thấy trong quá trình triển khai là baseline nếu chưa tái hiện ở base.

## Hợp đồng triển khai

### 1. Entry, caller và transaction

- Route dùng dependency session/CSRF/Origin chuẩn, clock dependency mới và real runtime engine. Release sạch dependency Session trước orchestration dài; không dùng owner/migrator/recovery fallback hoặc mở raw auth/journal grant.
- Mỗi checkpoint dùng Session mới, runtime/grants kiểm từng quyền cần thiết, recovery/safety gate thật. Chỉ giữ scalar/frozen DTO giữa pha. Luôn thử rollback và close; lỗi release invalidate connection và dừng bằng JSON 503 có mã ổn định. Proof chỉ có hiệu lực khi release sạch.
- Giữ thứ tự khóa tương thích C2d-6: writer có User trước resource; item theo UUID → collection/membership → capacity/usage nếu cần → journal cuối. Financial checkpoint chỉ capacity → usage → journal, không lấy khóa User/item để lấy lại quyền nội dung. Không thêm khóa hàng sau journal; không giữ Session/connection/khóa qua HTTP, sleep, heartbeat join hoặc serialize response.
- Caller hiện hành gồm latest status/credential head + receipt, generation của session, current role và idle/absolute expiry. Đọc clock **sau SQL cuối và sau journal wait**; không tái dùng thời gian đầu request. Áp cho answered, insufficient/no-terms, pricing unavailable và những nhánh trả dữ liệu/quyết định scope; không chỉ đường thành công có evidence.
- Request counter ghi nguyên tử, có final actor/binding/clock trước commit và proof mới nếu tiếp tục. Giữ chính sách đếm request hiện hành; không tăng hai lần do ghép lại các wrapper HTTP.
- Commit exception là uncertain, không SELECT để đoán landed, không tự retry, không provider/answer bước tiếp. Outcome known/uncertain không bị teardown ghi đè. Sau known commit vẫn cần transaction/proof mới trước dispatch hoặc response; release lỗi sau commit giữ dữ liệu/ceiling và trả lỗi.

### 2. Retrieval, evidence và citation

- Dùng lại runtime retrieval/query embedding của C2d-6 qua interface nội bộ rõ ràng. Có thể tách core dùng chung, nhưng không gọi HTTP search lồng, không quay về legacy để tiện fixture, không double reservation/counter hoặc tự gửi lại query/provider. Hồi quy HTTP search/viewer phải giữ nguyên.
- Giữ ACL trong SQL **trước keyword/vector top-k**, active generation/version/file/checksum/epoch, model/dimension và collection chỉ thu hẹp quyền. RAG opt-in common ở mọi scope/search/hydrate; private chỉ owner, Admin không có nhánh đọc private thông thường. ID trái quyền ban đầu giữ `404`.
- Snapshot scope và evidence đủ exact tuple, text/locator/coverage/title/visibility/`provider_ok`, membership và cặp paper đã phân giải. Kiểm lại trước pack, trước từng external dispatch liên quan và trước response. Sau khi attempt đã quan sát scope, thay đổi binding/rights/membership trả `409 rag_scope_changed`, không âm thầm chạy lại hoặc trả câu trả lời cũ; auth hết hiệu lực giữ lỗi auth tương ứng.
- Common chỉ có quyền search không vào generation, pack hoặc citation: hydrate mọi hit, lọc `provider_ok`, giữ `evidence_withheld`. Không có evidence hoặc câu so sánh thiếu đúng hai phía thì không reserve/gọi generation. Pricing gate vẫn có hiệu lực theo thứ tự kiểm quyền hiện hành; không lấy nhánh unavailable làm lý do bỏ final caller/resource.
- Giữ pack đã sửa M4d-3: giới hạn cấu hình paper/chunk/byte; phân giải đúng title/alias, ambiguity không chọn tùy hạng; scope có hai paper không tự thành cặp nếu câu hỏi không quy chiếu. Một claim so sánh phải cite đúng hai paper đã phân giải. Giữ no-terms, coverage PDF partial và reason hiện hành.
- Paper text là dữ liệu không tin cậy. Bibliography, citation link, discovery metadata và paper ngoài quyền không vào content evidence/payload. Giữ kiểm cấu trúc claim, quote exact và locator đúng file/version; không mô tả đó là chứng minh ngữ nghĩa đúng.
- Citation mở qua viewer runtime đã có. `original_url` chỉ từ gate tải riêng dưới final resource proof, không suy từ quyền RAG/search. Không trả answer/citation/quote khi final caller hoặc scope đổi. Response đã qua final boundary có thể tiếp tục tới client sau release; không hứa lấy lại bytes hoặc payload đã gửi provider.

### 3. Generation và kế toán

- Tạo financial binding generation có nonce/operation/User/session/task/model/provider/day/ceiling, frozen proof và DB constraint độc lập NULL-safe. Nếu dùng chung `SearchUsage`, thêm revision mới sau head thực tế để cho phép binding generation; **không sửa `0047` hay migration đã merge**. Đồng bộ ORM/schema, upgrade dữ liệu lịch sử và quy tắc downgrade. Không adopt row legacy all-NULL hoặc nonce query/rerank/document để dispatch/settle generation.
- Reuse phần kế toán phù hợp hoặc tạo boundary generation riêng; không ép generation qua giá/cột token của query. Admission nguyên tử kiểm application total, bucket generation, quota User hỏi, warning committed + reserved + uncertain và concurrency hiện hành. Common RAG vẫn tính generation cho người hỏi; document common owner NULL/kế toán hiện hành không đổi.
- Sau admission known commit và release sạch, mở **pre-send checkpoint mới**, kiểm caller/resource/external rights, exact nonce/proof/status/lease bằng clock mới; release sạch rồi mới gọi provider. Test thay đổi quyền giữa admission commit và pre-send, không chỉ trong hook đã bị mutant xóa cùng guard.
- Provider chạy ngoài DB, timeout hữu hạn, không resend. Heartbeat/reaper có binding, thời gian mới sau wait, không hồi sinh lease; hết lease giải slot và giữ ceiling. Settlement đúng nonce tối đa một lần trên ngày gốc, kể cả trả muộn sau expiry/logout/disable/revoke; financial authority không cấp lại content authority. Integrity/hold/safety vẫn chặn financial checkpoint theo hợp đồng hiện hành.
- Valid trusted usage input/output tính theo **giá ước tính** cấu hình trong ceiling; rejected chắc chắn xử lý theo hợp đồng adapter hiện hành; timeout/malformed/model/provider/usage không đáng tin giữ uncertain và ceiling. Không suy charge=0 vì không thấy usage. Financial commit/release/heartbeat lỗi không được nuốt để trả answered/degraded success. Nếu chưa gửi mà checkpoint lỗi, giữ reservation bảo thủ tới expiry/đối soát, không đoán kết quả commit.
- Giữ config model, gate `pricing_verified`, xác minh identity/usage/response size hiện hành; xử lý malformed nested values, bool/numeric overflow và lỗi transport bằng mã kiểm soát. Không tự mở allowlist/routing hoặc tuyên bố retention/charge đã kiểm live. Có kiểm transport giả qua **adapter thật**, không chỉ patch `generate` bỏ toàn bộ parser.
- Dọn heartbeat/thread/tài nguyên trên mọi đường. Proxy timeout/client disconnect không chứng minh FastAPI/provider đã bị hủy; outcome/ceiling vẫn được bảo toàn nếu xử lý còn chạy. Không hứa exactly-once provider hay rollback chi phí cùng DB.

### 4. UI, compatibility và docs

- Giữ form ba scope và citation viewer. Thông báo tiếng Việt phân biệt pricing unavailable, scope đổi và outcome/response uncertain; không hứa đã thất bại hoặc tự POST lại khi chưa biết. Giữ timeout proxy 120s hiện hành trừ khi có bằng chứng cần đổi; không thêm streaming.
- Chuyển test/evaluation capture sang runtime call site thật, giữ network deny và mọi expectation ACL/pack/citation. Compatibility direct entry nếu còn phải đi cùng boundary; không giữ một production path legacy chỉ để test/evaluation xanh. Không chấm lại/tuning trên holdout hoặc nâng nhãn `pending_human_review` trong PR này.
- Cập nhật caller map, handoff/inventory, hợp đồng search/RAG và kế hoạch MVP. Header tài liệu chỉ trạng thái/link ngắn; giữ lịch sử review có source, không nhân bản toàn bộ số liệu vào mọi file.

## Kiểm chứng hữu hạn theo rủi ro

Chọn selection đủ chứng minh luồng này và hồi quy giao diện dùng chung; không mặc định chạy hàng nghìn ca không liên quan mỗi PR. Hồi quy tổng/debt còn lại ở gói B/C4, không miễn lỗi baseline khỏi phát hành. Sửa fixture/debt trực tiếp cản RAG/runtime, giữ kỳ vọng nghiệp vụ; liệt kê node chưa chạy và lỗi base có bằng chứng riêng.

1. **Luồng và quyền:** PostgreSQL/runtime/journal thật, fake provider, item/collection/all × keyword/hybrid, A/B/Admin, private cùng checksum, common search-only/external/download/revoked; top-k=1 với hit trái quyền đứng mạnh hơn; no-terms/empty/partial/bibliography/injection. Kiểm payload, answered/insufficient, quote/locator và viewer.
2. **Race tất định:** đúng biên idle/absolute sau SQL/journal wait; logout/reset/disable/role hoặc generation/epoch/rights/membership đổi trước retrieval, giữa admission commit và pre-send, trong provider, trước final response. Revoke/closure commit được khi HTTP bị chặn; request mới sau revoke đóng, không phát answer đã mất quyền.
3. **Tài chính:** concurrent generation và query/document/rerank chung application budget; bucket/User warning, giới hạn generation, lease/ngày cũ, heartbeat fault, timeout/malformed/usage/identity sai, late/duplicate settle, nonce sai task/User/session/operation. Không double charge hoặc provider resend; giữ ceiling khi không rõ outcome.
4. **Fault và tài nguyên:** counter/admission/pre-send/settlement/response commit/release, biết commit rồi teardown lỗi, rollback/close lỗi, cancellation/heartbeat join. JSON lỗi ổn định, không IDs/answer/citation khi proof lỗi, không rò pool. Quan sát connection checkout khi transport bị chặn, không chỉ assert helper `commit` đã được gọi.
5. **Hồi quy:** RAG/common RAG/pack/comparison, search/keyword/viewer và query/rerank accounting; shared document capacity/financial totals; evaluation instrumentation/validation. Migration/ORM/config nếu đổi schema. Countercheck tập trung ACL trước top-k, pre-send, final clock, exact evidence/citation, nonce/capacity/late settlement; mutant phải làm fail assertion bảo vệ tương ứng. Giữ weak run riêng, khôi phục bytes trước seal.
6. **Chrome thật qua Next proxy:** form hỏi cả ba scope với fake generation qua adapter/runtime thật → answered → mở citation đúng nguồn; A/B/Admin denial và common; ít nhất một lần revoke giữa attempt; pricing unavailable/uncertain không tự resend. HTTP pending riêng giữ gate production thật: route `503`, health sống, ready chưa sẵn sàng. Không bypass safety trong production để chạy smoke nghiệp vụ.

Chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước Python. DB/login/project/port/bucket/journal/credentials/evidence cô lập ngoài repo. **Không đọc/sửa `.env`, query/migrate/restart dev, gọi arXiv/OpenRouter thật hoặc bật pricing.** Settings copy và transport giả trong fixture được dùng để kiểm nghiệp vụ. Chụp baseline, dọn đúng tài nguyên tự tạo, báo drift có bằng chứng.

## Tiêu chí hoàn thành và bàn giao

- Một PR tiếng Việt hoàn tất RAG runtime + generation accounting/dispatch + citation response; không owner fallback/legacy production path. Safety vẫn pending C4; không báo end-to-end readiness bằng toàn request 503.
- Exact selection/JUnit/pass/fail/error/skip/not-run theo node, source hash trước/sau; không sửa code trong seal hoặc cộng pass lịch sử. Artifact ngoài repo, secret scan và cleanup có phạm vi. Lỗi chuẩn bị/weak countercheck giữ riêng, không là pass mới.
- Handoff ngắn ghi base/head/source kiểm, caller/role/lock/binding map, lỗi đã sửa, semantics/error thay đổi và giới hạn provider/proxy/restore. Kiểm docs orchestrator đã **tracked trong cây Git commit**, liên kết nội bộ và `git diff --check` sạch.
- Commit/push và mở PR để review; **không merge**, không AI attribution. Kết quả provider giả không chứng minh grounded/citation quality, charge hoặc p95.
- Cập nhật gói A theo phần thực hoàn thành; bàn giao inventory caller/debt còn lại cho **C3 + hồi quy/tích hợp C4**, không tự tạo thêm gói runtime vi mô hay mở tính năng ngoài kế hoạch.
