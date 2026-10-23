# Sửa PR #71 — font tiếng Việt, ngắt dòng trong bảng, typecheck CI

Bạn là frontend agent của PR #71 (`feat/m10-frontend-ui`, head `f81abd8`). Sửa **trên cùng PR** ba lỗi trong [report](../reviews/pr71-m10-frontend-ui.md). Giữ nguyên phạm vi "chỉ trình bày" của M10. Giữ docs reviewer chưa commit và đưa vào PR.

## 1. Typecheck trên máy sạch

- Thêm file khai báo kiểu cho CSS Modules trong repo (ví dụ `web/types/css-modules.d.ts` với `declare module "*.module.css"`) và đảm bảo `tsconfig` include nó.
- **Không** dựa vào `next-env.d.ts` bị ignore.
- Kiểm bằng clone/worktree sạch không có `next-env.d.ts` và `.next/`: `npm ci`, rồi `npm run typecheck`, `npm test`, `npm run build` theo đúng thứ tự CI.

## 2. Font hỗ trợ đủ tiếng Việt

- Bỏ Georgia khỏi mọi chỗ có thể hiện tiếng Việt: `--font-reading`, `.authStory h2`, `.paperArt blockquote`, `q`.
- Dùng font hệ thống có đủ Latin Extended Additional trên Windows/macOS/Linux, không tải font ngoài. Ví dụ serif: `"Times New Roman", Cambria, "Noto Serif", "DejaVu Serif", serif`; sans giữ `Segoe UI`/system.
- Bỏ hoặc giảm `letter-spacing` âm trên tiêu đề tiếng Việt.
- **Kiểm trên chính Windows này**, bằng Chrome/Edge của host hoặc Playwright chạy trên Windows, không chỉ container Linux:
  - trang đăng nhập;
  - tiêu đề, trích dẫn trong RAG;
  - một chuỗi kiểm có đủ dấu: `ồ ắ ầ ẫ ữ ỵ ẽ ở ợ Ý Ặ Ổ`.
- Chụp ảnh trước/sau.

## 3. Ngắt dòng không cắt giữa từ

- Thay `overflow-wrap: anywhere` diện rộng bằng `overflow-wrap: break-word` (không làm co min-content). Chỉ giữ `anywhere` cho chuỗi dài không dấu cách đã biết (URL, ID, hash, tên file) bằng class riêng.
- Badge/chip trạng thái và nút: `white-space: nowrap`.
- Cột ngắn trong bảng (Kho, Trạng thái, ngày): độ rộng tối thiểu hợp lý.
- Ở màn hẹp, bảng cuộn ngang trong `.table-wrap` hoặc chuyển dạng thẻ, không vỡ chữ.
- Kiểm bảng thư viện của **user** (cột Kho "Riêng"/"Chung", trạng thái) và bảng Admin ở 1440, 1024, 768, 390, 360 px, có tiêu đề paper dài thật.

## 4. Kiểm chứng và bàn giao

- Web test/typecheck/build pass trong môi trường sạch; smoke HTTPS cô lập M8a pass (HTTP + Chrome).
- Build lại web image, ví dụ `ras-pilot-web:ui2`, và cập nhật demo bằng `scripts/m9_lan_demo.py web-image`. Chỉ recreate `web`, API rc2 giữ nguyên. Kiểm HTTPS 200 trên IP Ethernet hiện tại; nếu IP đổi thì `rebind` theo handoff M9.
- **Đợi CI GitHub trên commit sửa chạy xong**, báo run ID và kết quả từng job.
- Cập nhật handoff M10 (mục sửa review) và ảnh chọn lọc nếu thay đổi. Push cùng PR, không merge, giữ author/committer, không AI co-author/trailer/promo.
