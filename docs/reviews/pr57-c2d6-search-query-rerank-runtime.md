# Review PR #57 — search/query embedding/rerank runtime

**Cập nhật sau review:** chủ dự án đã merge PR #57 tại `2e948b78157bf4d498bdca4ea52ca81dd85c15c1`; đã kiểm head dưới đây là ancestor và tree khớp. Không chạy lại test tại merge. [C2d-7 RAG/generation runtime](../tasks/m6c2b2c2d7-rag-generation-runtime.md) mới giao, chưa triển khai/kiểm; safety pending C4. Trạng thái OPEN và số liệu dưới đây giữ thời điểm review.

Ngày **2026-10-11**. PR **OPEN, chưa merge**, head `44f1da11b5d47f1384b8cc07d5f831662b43d2aa`, base PR #56 merge `808337dcccfa472eefb05f2c968250948bae9bc2`. Agent kiểm tại `5da2cd38`, production `c41218b`; reviewer kiểm head hiện hành, **394 source hash khớp** source seal. Source migration **0047**, không truy vấn revision DB dev. [Handoff](../c2d6-search-query-rerank-runtime.md) · [Inventory agent](../c2d6-test-inventory.json) · [Kế hoạch MVP](../mvp-delivery-plan.md).

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi HTTP search, query embedding, rerank và chunk viewer của PR #57.** Có thể merge trong phạm vi đã review. **Safety vẫn pending tới C4**; kết luận không chứng nhận RAG/generation runtime, full regression, provider thật, charge, p95, restore hoặc readiness triển khai.

Docs orchestrator bị sót ở PR #56 đã có trong commit của PR này. Cây HEAD được kiểm trực tiếp: **91 Markdown, 1.810 liên kết nội bộ, không thiếu đích** (bao gồm root README/AGENTS); kiểm anchor/control character và `git diff --check` cũng đạt trước khi ghi docs review mới. Các docs review của lượt này giữ uncommitted cho PR kế tiếp.

## Kiểm độc lập thực chạy

Môi trường riêng do reviewer tạo: PostgreSQL/MinIO/Redis, journal và credentials riêng; Conda `ras` Python 3.12.14, pip 26.2.1. Business fixture chỉ patch kết quả pending như hợp đồng C2, dùng login runtime/receipts/generations thật; pending HTTP kiểm riêng bằng gate thật. Provider chỉ transport giả; không gọi arXiv/OpenRouter thật.

| Nhóm | Kết quả |
| --- | --- |
| Runtime fault/race/binding, common search, search/keyword, ORM/schema và shared document/query capacity/budget | **130/130 pass**, 0 fail/error/skip; 3 warning, 989,13 giây cho pytest/fixtures, không phải latency ứng dụng |
| Chrome 154.0.8037.98 qua Next proxy + uvicorn/runtime thật | **1/1 pass**, 24,07 giây; keyword → viewer, hybrid/rerank, uncertain không retry, unavailable, B/Admin denial và pending HTTP |
| Hai regression độc lập đổi epoch ngay sau admission commit | **2/2 pass**; query embedding và rerank đều chưa gửi payload khi quyền đổi |
| Đối chứng hai regression trên fixture cố ý bỏ pre-send | **2/2 bị bắt** đúng assertion provider đã nhận dữ liệu; không sửa source production |
| Web | **70/70 pass**, typecheck và build exit 0 |

Tổng reviewer **133 ca repo/smoke/regression đạt**, gồm **131 node repo không thiếu/trùng** và hai ca ngoài repo. Đối chiếu selection với collection agent 4.900 node; **4.769 node repo không được reviewer chạy lại**, không tính hai ca ngoài repo vào collection. Không cộng các failure cố ý của đối chứng vào kết quả code thực.

Các kiểm chính: ACL trước top-k; common search-only không vào rerank; download gate riêng; User/session/head/receipt/role/clock mới, kể cả empty/no-terms; revoke/membership/generation đổi trong lúc provider chạy; reservation commit/release fault không gửi bước sau; nonce, expiry, late/duplicate settlement và combined capacity/budget. Static review đối chiếu lock order, release Session, migration/ORM, compatibility callers và response materialization.

**Hai regression ngoài repo:** việc đổi epoch được gắn vào **sau commit admission**, độc lập với hook `checkpoint('presend')`. Bản thực trả `409 search_scope_changed`, 0 payload provider và không giữ connection. Fixture bỏ pre-send vẫn gặp thay đổi epoch, nhưng gửi payload trước khi bị phát hiện; cả hai assertion fail đúng mục tiêu. Điều này bổ sung cho countercheck agent vốn bỏ checkpoint và đồng thời làm hook đổi dữ liệu của test không chạy.

## Đối chiếu chứng cứ agent

- **89/89 artifact hash** khớp; selection/JUnit/collection có **336 unique pass**, không thiếu/trùng, 0 fail/error/skip; **4.564 not-run** giữ riêng.
- **394 source hash** trước/sau seal bằng nhau và khớp head review. Reviewer cũng giữ 394 hash trước/sau toàn lượt kiểm bằng nhau; không sửa source app/test/script/web trong lúc kiểm.
- XML của **7/7 countercheck** có failure ở mục tiêu kiểm, không setup error. Mutant scope chỉ bắt **503 thay vì 409**, không chứng minh leak text; mutant pre-send có giới hạn hook nêu trên, đã bổ sung chứng cứ độc lập. Không chạy lại các mutant sửa source của agent.
- Không suy 111 ca document accounting hoặc 11 ca RAG compatibility của agent thành số reviewer thực chạy; chúng giữ provenance riêng trong inventory agent.

## Giới hạn và phần tiếp theo

- `rag.py` còn dùng orchestrator `app.search.search`/generation legacy và compatibility maintenance. PR mới không rơi lại legacy ở HTTP search/viewer; RAG/generation runtime là gói kế tiếp sau merge.
- Provider giả không xác minh charge/routing thực, semantic quality hoặc p95. Snapshot scope và ma trận common chưa là benchmark corpus lớn. Response đã qua final boundary có thể tới client sau khi nhả khóa; không thu hồi được bytes/payload đã gửi.
- Không chạy full inventory, backup/restore drill, C3/C4 hoặc stack dev. Debt còn lại phải được xử lý ở tích hợp; không coi baseline failure là đã được miễn khỏi nghiệm thu MVP.

## Evidence, cleanup và lỗi công cụ review

Evidence ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr57-review-20261011` — `agent-evidence-verification.json`, `scope.xml/log`, `browser.xml/log`, `chrome-smoke.json`, `presend.xml/log`, `presend-control.xml/log`, `repo-selection.json`, `source-before/after.json`, `git-links.json`, `baseline.json`, `docker-after.json`, `review-results.json`, web logs và cleanup log. Scripts kiểm độc lập ở cùng thư mục; credentials/compose/empty.env đã xóa sau quét secret **0 hit**.

Đã gỡ đúng **3 container/3 volume/network** của reviewer. **9 container có sẵn giữ nguyên ID/image/mount/labels**; tập volume và image giữ nguyên sau chuẩn hóa thứ tự label và trường thời gian tương đối. Worker/beat vốn `restarting`, RestartCount **215 → 233** trong lượt kiểm; một số health check timestamp cũng đổi. Không có lệnh tác động stack dev, không suy nguyên nhân hoặc coi trạng thái dev đã được sửa.

Lỗi công cụ của reviewer không tính là kết quả sản phẩm: verifier ban đầu dùng sai tên trường inventory; một lệnh Python inline bị PowerShell chặn cú pháp và một lần gọi nhầm helper chưa tồn tại; kiểm cleanup đầu tiên so chuỗi label Docker và `CreatedSince` nên báo lệch dù ID/tập tài nguyên không đổi. Đã sửa verifier/canonicalization và kiểm lại; cleanup chỉ chạy một lần, các lượt sau chỉ đối chiếu. Không đọc/sửa `.env`, đổi pricing, commit/push/comment hoặc merge PR.
