# Review PR #56 — discovery runtime

**Cập nhật sau merge, 2026-10-11:** PR #56 đã merge `808337dcccfa472eefb05f2c968250948bae9bc2`; tree khớp `d957872`. Các kết quả dưới đây giữ source/lượt review trước merge, chưa rerun. Mục đóng gói docs chưa được làm trước merge được chuyển sang [task C2d-6](../tasks/m6c2b2c2d6-search-query-rerank-runtime.md), không yêu cầu mở lại PR #56. Safety pending tới C4; trạng thái OPEN bên dưới là lịch sử.

Ngày **2026-10-11**. PR OPEN, chưa merge; head `d957872e20eda2b1e81256147fadb751dbf0de35`, base PR #55 merge `e6b5991dd7ea9df6b8a5d9307f100ab0b87c7faa`. Code agent kiểm `7fc6b4e`, production `0795d42`; source migration **0046**. Không truy vấn revision DB dev.

## Kết luận

**Không phát hiện điểm chặn merge trong code thuộc scope C2d-5f2.** Reviewer thực chạy **106/106 pass**: 104 ca repo và hai smoke Linux độc lập, không failure/error/skip. **Cần hoàn tất đóng gói tài liệu trước merge:** hai file task/review có sẵn vẫn untracked, khiến 17 link trong cây commit của PR không có đích. Đây là bổ sung docs vào cùng PR; không yêu cầu một task code mới hoặc chạy lại toàn suite.

**Safety pending tới C4.** Không dùng kết luận này chứng nhận live arXiv/HTTP 406, search/RAG runtime, full suite hoặc cutover. [Prompt hoàn tất docs cùng PR](../tasks/fix-pr56-include-orchestrator-docs.md).

## Một mục cần hoàn tất trước merge

**Hai file đã được tham chiếu nhưng chưa có trong commit:**

- `docs/tasks/m6c2b2c2d5f2-discovery-runtime.md`
- `docs/reviews/pr55-c2d5f1-citation-reconcile-runtime.md`

`git status --short` báo cả hai untracked; `git ls-tree -r --name-only HEAD` không có chúng. Kiểm nội dung Markdown từ `git show HEAD:<file>` thay vì đọc working tree tìm thấy **17 link** thiếu đích ở AGENTS, README, docs index, handoff C2d-5f1/5f2, decisions, dev-setup, roadmap và testing. Ví dụ link task ở dòng 3 của handoff C2d-5f2. Vì file tồn tại cục bộ, validator chỉ kiểm filesystem có thể bỏ sót lỗi đóng gói này.

**Cách hoàn tất:** commit/push các docs có sẵn vào PR #56, giữ nguyên nội dung review/task và kiểm lại link theo cây commit. Source app/test/migration không cần sửa cho mục này. Reviewer không commit/push thay coding agent.

## Kiểm code

- `app/discovery.py`: direct/CLI/Celery cùng runtime boundary, từng grant được kiểm riêng, entry/final integrity gate thật; không owner/recovery fallback. Session ngắn release trước HTTP/parse/sleep. Outcome không bị rollback/close xóa; commit exception dừng uncertain, không SELECT đoán kết quả hoặc tự retry mutation.
- `0046`/ORM: thêm cặp nonce/thời hạn lease trên coordinator hiện có; upgrade giữ cursor/data. Claim/prepare/reserve/page/error/release kiểm nonce, cursor và thời gian PostgreSQL mới trước commit; holder cũ không ghi/thả lease mới.
- HTTP dùng endpoint cố định, no redirect, total deadline/cancel, byte/record/token cap; error allowlist, Retry-After/backoff toàn installation và pacing được giữ. Không tải file/provider hoặc biến discovery thành full-text evidence.
- Page metadata/license/tombstone/citation/cursor/counter cùng transaction. Retarget probe cap + 1 trước và dưới citation exclusive, quá trần rollback page; common item trước citation, item muộn NOWAIT. Không thấy tái tạo thứ tự deadlock đã sửa ở PR #55.
- Các fixture discovery cũ đã đi qua runtime và thực chạy harvesting; không đổi kỳ vọng ACL thành 503 pending. Các giới hạn DB timeout từng statement, lease/clock và fan-out cần đo được ghi trong handoff.

## Reviewer thực chạy

Môi trường PostgreSQL/MinIO/Redis/journal cô lập, Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe` (3.12.14, pip 26.2.1), login runtime thật; chỉ scoped cutover readiness trong fixture, integrity/hold/journal vẫn thật. External HTTP bị chặn.

| Selection | Kết quả |
| --- | --- |
| `test_c2d5f2_discovery_runtime.py`, `test_c2d5f2_retarget.py`, `test_discovery.py`, `test_c2d5f1_worker_hooks.py`, ORM/schema | **104 pass**, 303,05 s, ba warning thư viện/autogenerate |
| Bản sao test process smoke ngoài repo, CLI và Celery/Redis, SIGKILL giữa fetch, resume/idempotence | **2 pass**, 58,79 s, một warning thư viện |

Smoke chỉ thay tag dependency image bằng chính immutable image ID agent đã dùng (`sha256:7e1e6db56c089acfd5aec6c54d8010603bcae5372863c262a97d4e07737d3268`), source app mount read-only. Không build/chứng nhận image production mới. Cả hai kiểm exit 137, trạng thái resume `(2, 2, NULL)`, request start cách nhau ít nhất ba giây, pool checkout 0 và identity runtime không raw auth/journal write.

Selection/JUnit của từng lượt khớp, **335 source hash trước/sau không đổi** và khớp source agent kiểm. **4.724/4.828 node repo chưa được reviewer rerun**; hai smoke reviewer là bản sao ngoài repo, không cộng vào số node repo đã chạy. Không chạy lại web/browser, full suite, drill backup/restore hoặc các debt ngoài selection.

## Đối chiếu evidence agent — không tính là reviewer rerun

Đã kiểm **388 artifact SHA-256**, collection/selection/JUnit và source manifests. Seal `sealed-3` khớp **389 unique pass = 387 API + hai smoke repo**, collect 4.828, not-run 4.439, không failure/error/skip; các nhóm không thiếu/trùng node. **335 source hash** khớp trước/sau và source hiện tại. Bảy XML countercheck đều có một test failure đúng assertion và không setup error; không rerun mutant trong lượt review này. Hai smoke result record khớp identity, SIGKILL, pacing và cursor. Các seal/preparation lỗi giữ lịch sử, không cộng pass lặp.

## Evidence, cleanup và giới hạn

Evidence reviewer nằm ngoài repo tại `C:\Users\ADMIN\AppData\Local\Temp\ras-pr56-review-20261011`: selection/log/JUnit, source manifests, agent-evidence-verification, head-missing-links và review-results. Không commit artifact/journal/credentials.

Đã dọn đúng ba container, ba volume và network của project thử; smoke tự dọn các process container. Credentials/Compose sinh cho task đã xóa sau quét secret 0 hit. Chín container có sẵn giữ ID/name/image; image/volume baseline khớp. Worker/beat dev đã Restarting trước review, RestartCount 86 → 93 và StartedAt đổi trong thời gian review; không xác định nguyên nhân và không thao tác chúng. Không đọc/sửa `.env`, query/migrate/restart dev, gọi arXiv/provider thật hoặc đổi pricing.

Hard cap retarget 2.000 và lease/deadline là lựa chọn scoped cần đo, không SLO. HTTP có thể lặp sau crash; không chứng minh exactly-once hoặc độ bền mất điện. Live HTTP 406, debt hồi quy/restore và C3/C4 còn riêng. Sau khi đóng gói docs, bước kế tiếp vẫn là **search/query embedding/rerank trong một gói hoàn chỉnh** theo kế hoạch MVP.
