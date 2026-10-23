# Review PR #71 — M10 giao diện web

**Kết luận tại `f81abd8` (2026-10-22): có ba lỗi cần sửa cùng PR trước merge.** Hai lỗi do chủ dự án thấy trên demo thật (Windows, Chrome/Edge); một lỗi CI. [Prompt sửa](../tasks/fix-pr71-vietnamese-font-wrap-ci.md). PR OPEN, chưa merge.

## 1. CI: typecheck fail trên máy sạch (P2)

- CI run `39755540725`, job *Web tests, typecheck, build* fail: `app/components/ui.tsx(4,20): error TS2307: Cannot find module './ui.module.css'`. Test web 79 pass.
- Nguyên nhân: kiểu cho `*.module.css` chỉ có trong `web/next-env.d.ts`. File này do `next build` sinh và bị `.gitignore` (dòng 22). CI chạy `tsc --noEmit` trước build nên không có file; máy agent có file cũ nên local xanh.

## 2. Chữ tiếng Việt bị tách ở trang đăng nhập (P2, chủ dự án thấy trên demo)

- "Câu trả lời có nguồn." tách giữa "ồ" và "n"; "Ý tưởng bắt đầu từ một câu hỏi." tách sau "ắ" và "ầ".
- Hai câu dùng `font-family: Georgia` (`.authStory h2`, `.paperArt blockquote` trong `ui.module.css`) kèm `letter-spacing: -.04em`. Georgia trên Windows **không có glyph tiếng Việt dựng sẵn** của khối Latin Extended Additional (U+1EA0–1EF9: ồ, ắ, ầ…). Trình duyệt lấy từng ký tự đó từ font khác, metric lệch nên tạo khoảng hở.
- `--font-reading` và `q { font-family: Georgia }` trong `globals.css` có cùng rủi ro cho trích dẫn/đoạn đọc.
- Ảnh kiểm của agent chụp bằng Chrome trong container Linux, không có Georgia thật, nên không lộ lỗi.

## 3. "Riêng" xuống dòng từng ký tự trong bảng thư viện (P2, chủ dự án thấy)

- Badge cột "Kho" vỡ thành "Riên"/"g".
- Nguyên nhân: `overflow-wrap: anywhere` đặt rộng ở `.page-content`, `h1–h3`, `li`, `dd`, alert và `.table-wrap td`. Với `anywhere`, độ rộng tối thiểu của ô bảng tính như thể mọi ký tự đều ngắt được, nên cột co dưới độ rộng một từ. `.badge` cũng không có `white-space: nowrap`.

## Đã kiểm (reviewer)

- Đọc `globals.css`, `ui.module.css`, `ui.tsx`, `page.tsx` trên `origin/feat/m10-frontend-ui`.
- Log CI job web.
- Kiểm `next-env.d.ts` bị ignore.
- Chưa chạy lại smoke/ảnh. Các kết quả 79/79, 68/16, 54 ảnh là của agent.
