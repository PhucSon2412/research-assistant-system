# Hoàn tất đóng gói docs trên PR #56

**Đã thay thế sau merge, 2026-10-11:** PR #56 đã merge `808337dc` khi docs vẫn chưa được đưa vào commit. Giữ tài liệu này làm lịch sử review; đưa các file còn thiếu cùng docs orchestrator vào PR [C2d-6 search/query/rerank](m6c2b2c2d6-search-query-rerank-runtime.md). Không mở lại PR #56 và không cần một PR code riêng cho việc đóng gói. Các chỉ dẫn “cùng PR #56” bên dưới chỉ còn là lịch sử.

PR #56 vẫn OPEN, head review `d957872`; [review](../reviews/pr56-c2d5f2-discovery-runtime.md) không phát hiện blocker code. Reviewer đã chạy 106 pass; safety pending tới C4.

Hoàn tất **trên cùng PR #56**, không tạo PR/task code mới:

1. Giữ và đưa hai file orchestrator có sẵn vào commit: `docs/tasks/m6c2b2c2d5f2-discovery-runtime.md` và `docs/reviews/pr55-c2d5f1-citation-reconcile-runtime.md`. Hiện chúng tồn tại trong working tree nhưng không có trong `git ls-tree HEAD`, gây 17 link thiếu đích trong cây commit của PR.
2. Giữ các docs review/task và cập nhật kế hoạch của lượt review này; có thể đưa cùng commit docs. Không thêm artifact, journal, credentials hoặc `.env`.
3. Kiểm liên kết **theo cây commit**, không chỉ theo filesystem; xác nhận các file đích đã tracked. Chạy `git diff --check`. Nếu chỉ đổi docs và source app/test/migration giữ nguyên, không cần chạy lại API/web suite.
4. Commit/push, cập nhật mô tả PR ngắn rằng đã bổ sung docs thiếu. Báo commit và kết quả kiểm; không merge. Không tác động dev, provider hoặc pricing.

Đây là mục đóng gói tài liệu; không yêu cầu thêm code runtime, fixture, migration hoặc nâng scope kiểm chứng.
