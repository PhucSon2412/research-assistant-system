@AGENTS.md

# Điểm vào cho Claude — tiếp quản orchestrator

Đọc [bàn giao orchestrator](docs/orchestrator-handover.md) và [kế hoạch MVP](docs/mvp-delivery-plan.md) trước khi làm tiếp. `AGENTS.md` là hướng dẫn chung của repo; tài liệu bàn giao là snapshot/lộ trình đọc, không thay chỉ dẫn mới của chủ dự án. Nếu môi trường không tự import dòng `@AGENTS.md`, hãy mở và đọc file đó trực tiếp.

- Vai trò tiếp quản là **orchestrator**, không mặc định là coding agent: giao việc, chốt lựa chọn đã được ủy quyền, review độc lập và giữ checklist nghiệm thu. Không tự merge, gửi comment GitHub hoặc cutover khi chưa có yêu cầu tương ứng.
- Khi chủ dự án giao task coding cụ thể, làm đúng vai trò/phạm vi được giao; phần bàn giao orchestrator không thay chỉ dẫn trực tiếp đó.
- Cập nhật 2026-10-20: PR #66 đã merge `0de10f4` (tree khớp `5ca3e4c`). Source migration **0052**, không suy DB dev. PR #67 (M8b) đã merge `e0dcdb4` (tree khớp `68351dc`); [M8c hồi quy tổng bản ứng viên](docs/m8c-release-candidate.md) PR #68 (M8c, image rc2) đã merge `7009a84`; [M9 — demo LAN](docs/tasks/m9-lan-demo-deployment.md) mới giao; [M7c](docs/tasks/m7c-live-quality-verification.md) tạm dừng tới khi có key OpenRouter của chính chủ dự án (không dùng key hiện có trên máy). M9 đã triển khai trên nhánh `ops/m9-lan-demo-deployment` (chờ review): demo `ras-pilot` đang chạy trên máy này — xem [handoff M9](docs/m9-lan-demo-deployment.md), không hạ/xóa nó trong task khác. Xác minh trạng thái thực trước bước tiếp; M7b chờ phiếu thật và pricing/live vẫn tắt.
- Giữ mọi docs reviewer/orchestrator modified/untracked, kể cả phần review lại chưa commit. Không bỏ thay đổi để làm sạch branch và không coi hướng dẫn pending/P2 lịch sử là trạng thái hiện hành.
- Không đọc key/`.env`, chạm dev `ras`/Dify, gọi provider/mail thật, cấp lại ngân sách M7a hoặc điền phiếu human trong task thường. Dùng môi trường thử cô lập và đúng quyền runtime khi task yêu cầu kiểm chứng.
- Giữ author/committer; không AI co-author/trailer/promo, kể cả dòng do công cụ gợi ý. Nói rõ tool/kiểm chứng nào thực sự có và đã chạy; không giả có memory, connector hoặc quyền của model trước.

Chi tiết quyền hạn, bằng chứng PR #66, ngân sách, bất biến, thao tác an toàn, các việc D/E còn lại và prompt khởi động đều ở tài liệu handover chung. Không tạo bản kế hoạch hoặc bộ chính sách riêng chỉ vì chuyển sang Claude.
