# Review PR #58 — RAG/generation runtime

**Cập nhật merge 2026-10-11:** PR #58 đã merge tại `58d059ab7a383d5f97e1b04e909e9628a11bf78a`; head `aa9c8a2` là ancestor, tree khớp. Không chạy lại kiểm thử tại merge. Report dưới đây giữ chứng cứ lúc review; tiếp [task C3](../tasks/m6c2b2c3-operator-auth-recovery.md), safety vẫn pending C4.

Ngày **2026-10-11**. PR **OPEN, chưa merge**, head `aa9c8a24f6059cef0572c9f3d7e34871446767c6`, base merge PR #57 `2e948b78157bf4d498bdca4ea52ca81dd85c15c1`. Agent seal tại `0cee567ac2706f5c1fda41c844d127d73423a6a1`, production `8ecd979`; cây `api/scripts/web` head khớp seal và **401 source hash khớp**. Source migration **0048**, không truy vấn DB dev. [Handoff](../c2d7-rag-generation-runtime.md) · [Inventory agent](../c2d7-test-inventory.json) · [Task](../tasks/m6c2b2c2d7-rag-generation-runtime.md) · [Kế hoạch MVP](../mvp-delivery-plan.md).

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi RAG/runtime, generation binding/accounting và citation response của PR #58. Có thể merge trong phạm vi đã review.** Safety vẫn **pending tới C4**. Kết luận không chứng nhận full regression, C3/operator, cutover/readiness, restore, provider thật/charge/routing/retention, ngữ nghĩa câu trả lời hoặc p95.

Route và direct wrapper đi cùng runtime; retrieval dùng core C2d-6, không quay về legacy orchestration. Kiểm caller/scope/evidence, pre-send sau known commit và release, final response/clock; generation có nonce-bound financial proof riêng, không adopt row all-NULL/task khác. Shared capacity/budget và late settlement tách content authority. Revision `0048` mở binding generation, giữ upgrade lịch sử và chặn downgrade mất binding. Các fixture/hook cũ đổi theo boundary mới; expectation reservation bị chặn trước dispatch giữ ceiling bảo thủ là thay đổi có chủ ý.

## Kiểm độc lập của reviewer

Conda ras; PostgreSQL/MinIO/Redis/journal cô lập, runtime login thật, provider transport giả và chặn HTTP ngoài loopback. Không đọc/sửa `.env`, gọi provider/arXiv thật, mở pricing hoặc thao tác stack/dữ liệu dev.

| Nhóm thực chạy | Kết quả |
| --- | ---: |
| `test_c2d7_rag_runtime.py` + ORM/schema | 112 pass, 0 fail/error/skip |
| Search keyword/hybrid × rerank bật/tắt, adapter và viewer | 4 pass |
| Chrome `154.0.8037.98` qua Next proxy + uvicorn/runtime thật | 1 pass |
| Hai regression độc lập: thay binding ngay sau admission commit | 2 pass |
| HTTP pending dùng hàm cutover production không patch | 1 pass |
| **Tổng reviewer** | **120 pass: 117 node repo + 3 ca ngoài repo** |
| Web | **70/70**, typecheck/build exit 0 |

Nhóm 112 ca mất **663,08 giây**, có ba warning thư viện/autogenerate, không failure/error/skip. Đây là thời gian suite có fixture, không latency ứng dụng. Selection/JUnit exact; 117 node repo đều nằm trong full collection 5.012 node, reviewer **không chạy lại 4.895 node**. Không cộng pass agent vào số reviewer.

Chrome kiểm ba scope, hybrid, citation/source viewer, common cho B/Admin và private denial, actual HTTP revoke trong generation, unavailable/uncertain không tự POST lại. Scope nghiệp vụ dùng cutover test fixture, không chứng nhận C4.

Hai ca ngoài repo thay `operation_id` của reservation hoặc title của item **sau `boundary.commit` đã trả về**, không dùng hook presend để gây thay đổi. Cả hai chặn trước generation, giữ reserved ceiling, pool trả sạch; trả lần lượt `503 rag_reservation_lost` và `409 rag_scope_changed`. Bỏ riêng presend bằng monkeypatch trong process kiểm đối chứng làm **2/2 fail đúng assertion provider đã nhận evidence**. Không sửa source production; hai failure có chủ ý này không là lỗi sản phẩm hoặc pass mới.

**Phân biệt chứng cứ pending:** cuối smoke browser dùng lambda trả `auth_safety_pending`, chưa phải khôi phục hàm production như docstring mô tả. Reviewer bổ sung ca dùng chính `auth_safety.cutover` production, không patch: RAG `503`, health `200`, ready `503`, không provider hoặc generation row. Vì vậy không dùng riêng đoạn browser đó để tuyên bố đã kiểm gate thật; cũng không suy gate đang đóng thành readiness end-to-end. Khi cập nhật harness/C4, nên giữ hàm thật cho phép thử pending.

## Đối chiếu chứng cứ agent

- **194 artifact hash** đều khớp. JUnit từng nhóm khớp selection và full collection; **496 unique pass**, 0 fail/error/skip, **4.516 not-run** đúng inventory. Trong 496 đã có một Chrome, không cộng lại.
- **401 source hash** seal trước/sau trùng và khớp source head; reviewer trước/sau cũng trùng. Không sửa code trong lượt kiểm độc lập.
- Tám XML countercheck đều có assertion failure mục tiêu, không setup error; đã đọc mutation/target. Reviewer đối chiếu artifact, **không chạy lại tám mutant**. Weak control và hai seal lỗi được giữ riêng trong inventory; không tính như seal cuối.
- Trên cây HEAD sạch trước ghi review: **94 Markdown, 1.864 liên kết nội bộ, 0 lỗi**, control-character/anchor và `git diff --check` đạt. Docs orchestrator của lượt giao C2d-7 và review PR #57 đã tracked trong PR.

## Cleanup, giới hạn và bàn giao

Evidence reviewer ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr58-review-20261011` (`agent-evidence-verification.json`, JUnit/log từng nhóm, `chrome-smoke.json`, source manifests, `repo-selection.json`, `review-results.json`, baseline và cleanup). Lệnh reviewer dùng `review.py prepare|verify|scope|browser|compat|extra|extra-control|real-pending|web|finish`; runner repo là `scripts/c2d7_test_host.py --context <reviewer-context> setup|test …`. Không thêm lệnh này vào runbook vận hành.

Đã kiểm ID/label trước khi gỡ đúng **3 container, 3 volume và network** task. Chín container có sẵn giữ ID/image/mount/label; volumes/images khớp baseline. Worker/beat vốn `restarting`, RestartCount **503→515 / 503→514**; health state của một số service thay đổi theo probe. Chỉ ghi drift quan sát được, không suy nguyên nhân hoặc đã sửa dev. Scan 33 log/XML/JSON ngoài file credentials/Compose tạm: **0 task-secret hit**; đã xóa ba file credentials/Compose/env rỗng của task.

Một lệnh khảo sát đầu dùng Python inline bị PowerShell bỏ dấu nháy (`NameError`); chuyển sang file helper ngoài repo, không là lỗi ứng dụng hay kết quả pytest. Các đối chứng chỉ monkeypatch process, không thay file source. Không commit/push/comment/merge trong lượt review này; docs review mới giữ uncommitted cho PR kế tiếp.

Provider giả không chứng minh charge hoặc chất lượng grounded/citation/abstention. Ranh giới hash/commit, payload đã gửi không thu hồi, journal độc lập và debt lịch sử giữ giới hạn hiện hành. Sau merge, chuyển **C3 + hồi quy/debt/tích hợp C4** theo kế hoạch MVP; không mở thêm gói runtime vi mô chỉ vì full inventory chưa chạy trong PR này. Private deletion/restore và nghiệm thu provider/triển khai vẫn còn riêng.
