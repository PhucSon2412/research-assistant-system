# M7a — kiểm chứng OpenRouter và đối soát chi phí

## Mục tiêu và base

Thực hiện **một PR hoàn chỉnh** của gói D: biến các adapter đã kiểm bằng provider giả thành luồng có chứng cứ live với **dữ liệu tổng hợp**, gồm route/model/provider, dimensions/usage, privacy/routing và charge. Xây tooling tái lập có trần chi phí, sửa các lỗi tích hợp thật tìm thấy và bàn giao cấu hình đủ điều kiện cho pilot. Không chia từng model thành một chuỗi PR.

PR #63 đã merge **`5362ace04d0d211005753aecddd760a4ad7d548b`**; đã fetch/xác minh `c1f7bcc` là ancestor và tree khớp. Source **0052**; không suy DB dev hoặc cộng pass mới tại merge. [Review M6e](../reviews/pr63-m6e-private-duplicate-retry.md) giữ kết quả reviewer/agent riêng. Đọc `AGENTS.md`, [kế hoạch MVP](../mvp-delivery-plan.md), [quyết định](../decisions.md), [C4 runbook](../c4-integrated-runtime-cutover.md), [RAG evaluation](../rag-evaluation.md) và handoff runtime document/query/rerank/generation trước sửa.

Fetch lại, tạo `feat/m7a-openrouter-live-verification` từ `origin/main`. Giữ toàn bộ docs reviewer/orchestrator modified/untracked, đưa vào PR. Không sửa migration đã merge; chỉ thêm revision nếu thay đổi lưu bền thực sự cần thiết, không dựng hệ thống billing/permission mới chỉ để chạy smoke.

## 1. Quyền thực hiện và giới hạn experiment

Đây là task riêng cho **live synthetic smoke**, theo ủy quyền tiếp tục dự án và cho phép dùng key hiện có của chủ dự án trong hội thoại. Không yêu cầu đổi key hoặc dán key vào chat. Không coi các ghi chú “no live/no .env” của những task kiểm cô lập cũ là đã thu hồi ủy quyền dùng key; các giới hạn không đụng dev/Dify/dữ liệu thật vẫn giữ.

- Ưu tiên lấy key từ process environment/secret input; khi cần chỉ đọc các trường key OpenRouter liên quan đã được cấp trong `.env` vào bộ nhớ. Không dump/import toàn bộ `.env` vào context thử, không in key, header Authorization, cookie, PHC hoặc nguyên response debug. Không sửa `.env`, key/account setting, spending cap, quyền Admin thật hoặc cấu hình stack dev.
- Chỉ dùng giấy tờ/câu hỏi tổng hợp tự viết trong project DB/roles/MinIO/Redis/journal/port riêng, runtime/C4 thật. Không dùng file private của người thật hoặc paper bên thứ ba. Không đọc/migrate/restart dev, dừng Dify, gọi arXiv hay gửi key tới Orbit/Postman/workspace bên ngoài.
- **Trần task do orchestrator chọn: tối đa $0,50 và 20 request inference có thể tính phí**, bao gồm probes, runtime và outcome chưa chắc chắn. Chạy tuần tự, concurrency 1. Read-only catalog/usage polling không tính vào 20 inference, nhưng phải có giới hạn thời gian/số lần.
- Trước mỗi request kiểm tổng charge đã xác định + trần bảo thủ của request đã gửi nhưng chưa xác định + trần request sắp gửi. Vượt trần hoặc không tính được trần thì dừng trước gửi. Request đã attempt/uncertain không tự hoàn slot/tiền hoặc gửi lại để “lấy kết quả xanh”. Không tự nâng trần khi thiếu quota/credit hoặc provider lỗi.
- Experiment có manifest/outcome bên ngoài repo để resume không quên các call cũ sau process chết; tái dùng ledger/checkpoint hiện có khi đi qua runtime, không thêm framework orchestration. Dừng khi không thể biết experiment cũ còn request đang chạy hay còn bao nhiêu trần đã dành.
- Default `pricing_verified=false` trong source/config mẫu và môi trường thật giữ nguyên. Chỉ enable từng route trên settings/context tổng hợp tường minh phục vụ kiểm chứng; không mở dev/pilot bằng một probe. Sau task dừng project thử và gỡ mọi secret override.

## 2. Preflight trước request trả phí

Tái dùng `scripts/smoke_embedding_route.py`, `scripts/smoke_rerank_route.py`, adapter production và tooling C4. Trước hết đối chiếu contract hiện hành bằng nguồn chính thức; dùng Postman skills phù hợp, không dựng workspace/collection mới nếu không giúp task. Orbit là discovery cho public contract, không là chứng cứ model live hoặc charge.

1. Lập ma trận configured model/endpoint, catalog availability, pricing hiện tại, provider có thể phục vụ và support tham số. Ba model hiện có: `openai/text-embedding-3-small`, `qwen/qwen3-reranker-8b`, generation mặc định trong source `openai/gpt-6-sol`. **Không suy model tồn tại từ tên/config.** Nếu exact ID không khả dụng, ghi rõ; không tự đổi model, dùng alias gần giống, free router hoặc fallback model. Hoàn tất phần độc lập còn làm được và nêu quyết định model còn thiếu.
2. Ghi giá/config ceiling bằng Decimal, thời điểm kiểm, đơn vị token/request và phụ phí liên quan. Quote catalog/config không phải charge. Không tự tăng giá trần production theo quote mới. Nếu estimate không đủ bảo thủ cho route thì dừng route đó.
3. Tách smoke document embedding và query embedding (`input_type` thực), rerank và generation. Xác minh vector hữu hạn/dimension, thứ tự rerank đủ và JSON answer/finish reason/usage qua chính adapter; không gọi API trực tiếp rồi coi adapter đã qua.
4. Ghi requested model, response model, provider thực và ID request/generation khi có. Allowlist embedding Azure và rerank Fireworks hiện tại dựa trên quan sát cũ, không chứng minh route vẫn vậy. Không gỡ kiểm identity khi live response khác; chỉ thêm mapping hữu hạn sau khi đối chiếu tài liệu và chứng cứ route thực, có negative tests cho sai/thiếu identity. Không thay cả response bằng fixture để vượt validator.

## 3. Routing và xử lý dữ liệu

Chọn policy MVP do orchestrator đề xuất cho pilot: provider được chỉ định rõ, không fallback ngoài tập đã kiểm; không cho training/data collection và ưu tiên yêu cầu ZDR. Đây là lựa chọn theo ủy quyền, không ghi là chủ dự án đã trực tiếp xác nhận nhà cung cấp hay điều khoản cụ thể.

- Kiểm **riêng từng endpoint**: chat/embedding/rerank có nhận và thi hành policy nào, provider slug/endpoint nào đáp ứng. Không gửi field chat sang embedding/rerank rồi suy rằng server không báo lỗi nghĩa là đã thi hành. `data_collection=deny` và ZDR là các kiểm soát khác nhau; không gọi một trường là bằng chứng cho mọi yêu cầu retention.
- Nếu dùng request-level routing, pin tập provider đã kiểm và ngăn fallback ngoài tập, kiểm hỗ trợ tham số cần thiết. Phản hồi thiếu/sai provider/identity thì lỗi có kiểm soát, giữ reservation theo outcome thật; không tự nới policy để gọi thành công.
- Nếu endpoint không có cách chứng minh policy qua API, ghi nguồn/cách kiểm provider/account hiện hành và phần chưa xác minh. Không tự thay account privacy setting. Không thể đáp ứng thì **chưa đủ điều kiện gửi paper thật**; vẫn có thể hoàn tất smoke tổng hợp được phép và bàn giao phần thiếu rõ ràng.
- Thực thi bằng phần mở rộng nhỏ nhất của config/adapter có validation, default không tự mở route và tests. Không sửa ACL/common rights, upload consent, evidence policy hoặc financial nonce để tiện gọi provider. Common cần `permits_external_processing` hiện hành; common embedding application-only tiếp tục là lựa chọn orchestrator, không đổi thành quota của người upload/Admin.

Nguồn phải kiểm lại lúc triển khai: [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection), [embeddings contract](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request), [RAG/rerank](https://openrouter.ai/docs/guides/evaluate-and-optimize/rag), cùng policy provider thực. Routing docs mô tả provider preferences cho chat; không tự khẳng định toàn bộ trường áp dụng giống nhau cho endpoint khác.

## 4. Charge thực và outcome

- Đối soát từng call khi có thể: request/generation ID, requested/actual model/provider, token/usage, quote estimate, app reservation/settlement và charge do provider báo. Tách **estimated**, **provider-reported billed**, **chênh lệch account/key**, **chưa xác định**; không ghi cost 0 vì thiếu trường hoặc số dư chưa đổi.
- Dùng nguồn phù hợp cho từng route. [Generation usage endpoint](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation) là một nguồn đối soát generation; không mặc định nó hỗ trợ ID embedding/rerank. [Current-key usage](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key) có thể làm đối chứng tổng, không đủ quy chiếu từng request khi key có traffic khác hoặc số liệu trễ.
- Không sửa `cost_usd` ước tính thành “actual” chỉ bằng đổi nhãn. Nếu bổ sung receipt/observer, chỉ lưu ID/model/provider/token/cost/time có giới hạn; không raw prompt, vector, nội dung private, Authorization hoặc full key metadata. Observer phải delegate request thật, không sửa response hay thay transport bằng fake trong lượt gọi được ghi live.
- Poll read-only có backoff và deadline rõ, tối đa 5 phút cho một lượt đối soát; không gọi thêm inference để làm số dư thay đổi. Nếu còn pending, lưu state/IDs đã che dữ liệu không cần thiết và lệnh tiếp tục đối soát sau; không loop chờ vô hạn hoặc đánh dấu verified khi chưa có chứng cứ.
- Retryability và uncertain reservation hiện có giữ nguyên: 429 chắc chắn bị từ chối khác timeout/5xx/response invalid; commit uncertain/release lỗi không success/retry; kết quả về muộn chỉ settlement tài chính một lần, không resurrect dữ liệu đã xóa/thu hồi.

## 5. Một smoke runtime tích hợp nhỏ

Sau probes, thực hiện phần đủ điều kiện trên **cùng image/revision/runtime roles và context tổng hợp**, không chỉ gọi adapter raw rồi báo end-to-end:

1. A/B/Admin tổng hợp qua bootstrap/invite/login thật; upload có consent/intent, ít nhất một Markdown và một PDF body hợp lệ. Giới hạn corpus **tối đa tám chunk được embed tổng cộng**, kể cả bản common nếu dùng; preflight số chunk/call trước khi dispatch để không vượt trần.
2. Worker/index tới indexed với embedding thật, keyword/hybrid search với query embedding thật; một lượt rerank thật nếu route đủ điều kiện. Giữ một bản same-bytes của B để kiểm scope/không tự liên kết xuyên owner nếu còn trong giới hạn.
3. Ba câu RAG nhỏ: một paper, so sánh hai paper, không đủ bằng chứng. Mở citation đúng version/file/locator qua API/proxy. Không chấm grounded/citation semantics bằng model tự chấm hoặc suy từ JSON hợp lệ; lưu answer tổng hợp làm mẫu chấm sau.
4. Có control B/Admin bị từ chối private và một revoke/delete sau khi có evidence/citation; request mới không mở nguồn cũ. Nếu kiểm common live, đi qua review/approval/publication/rights thật với nguồn tổng hợp tự viết và quyền external rõ, không SQL bypass rights. Không bắt thêm common corpus nếu làm vượt cap; bằng chứng common accounting/rights có thể giữ ở test mô phỏng và ghi chưa live.
5. Một happy path qua Chrome/Next proxy trên image thật tới answer hoặc lỗi provider được phân loại đúng; không thay backend bằng host fake trong bằng chứng live image. Không auto-retry request vì proxy timeout hoặc câu trả lời chưa đúng kỳ vọng.

Nếu route/model/key/charge chưa đủ, không ép cả chuỗi bằng cách bật flag hoặc fake response: hoàn tất tooling/tests và các route độc lập, báo chính xác ô ma trận **blocked/not verified**. Không gọi gói D hoàn tất khi charge/routing hoặc human review còn thiếu.

## 6. Kiểm chứng offline trước live và bàn giao

Chạy tests bằng provider mô phỏng trước: cap request/USD và resume sau crash, secret redaction, pricing mặc định off, validation routing, no-fallback, identity sai/thiếu, usage/cost thiếu/sai, polling trễ, timeout/429/5xx, uncertain/late settlement. Reuse tests/contract hiện có; theo skill dùng simulation phù hợp cho lỗi upstream, không gọi provider thật để thử timeout/lỗi. Giữ rõ phần mô phỏng so với live.

Chạy selection backend theo các adapter/runtime/config/accounting bị tác động; web test/typecheck/build nếu đổi UI. Không full >5.000 test hoặc mọi restore drill cho thay đổi này trừ rủi ro shared schema/gate cụ thể. Không sửa code/countercheck trong lúc seal đang chạy. Không chạy live trong pytest/CI mặc định, không cần key để suite offline đạt. Giữ lịch sử fail/rerun/not-run, hai failure M6d chưa rõ nguyên nhân không bị xóa bằng nhãn “đã giải thích”.

Commit/push, mở **một PR tiếng Việt**, không merge. Giữ author/committer, không AI trailer/promo. Đưa docs reviewer/orchestrator vào PR; tạo handoff và inventory ngắn theo commit/image/route, actual calls/USD, evidence redacted và lệnh tiếp tục đối soát. Tạo ma trận **đã chứng minh / chưa chứng minh / điều kiện để enable** cho từng route và runbook pilot giữ default off.

Chỉ commit corpus/answer tổng hợp sạch và receipt tối thiểu không secret; raw evidence/secret context bên ngoài repo có ownership và cleanup. Cuối task gỡ container/volume/secret của chính task sau kiểm ownership, giữ dev/Dify và config thật.

Phần còn lại của D (M7b): người đọc duyệt nhãn và chấm grounded/citation/abstention trên corpus phù hợp, thêm paper thật có quyền gửi provider sau khi policy/charge đủ. E: journal độc lập, HTTPS/delivery/rate-limit/CI/backup/alert và cutover nhóm thử. Không làm các gói này ngầm trong M7a.

**Inventory lượt giao:** chỉ kiểm Git/source, public API discovery Orbit và tài liệu OpenRouter; không đọc key/`.env`, gọi inference, đổi pricing hoặc thao tác dev. Task **mới giao/chưa triển khai hoặc kiểm live**; nguồn công khai và quote không phải xác minh tính khả dụng/charge của key cụ thể.
