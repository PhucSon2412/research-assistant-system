# M10 — làm mới giao diện web cho bản demo

Bạn là frontend agent. Giao diện web hiện gần như chưa có thiết kế: HTML mặc định, khoảng 69 chỗ `style={{…}}` inline, không có stylesheet. Mục tiêu do chủ dự án nêu (2026-10-22): một giao diện **chuyên nghiệp, ấn tượng khi demo cho CV**, không đổi hành vi hệ thống. Làm **một PR hoàn chỉnh**.

Dùng skill `ras-frontend` (nguyên tắc thiết kế và ràng buộc) và skill `playwright` (kiểm trực quan) nếu môi trường của bạn có.

## 1. Base

- PR #70 đã merge: `origin/main = 0dd5a9c07ac0561c55b97da52555b2777d291817`. Source migration **0052**.
- Fetch lại, xác minh, tạo `feat/m10-frontend-ui` từ `origin/main`.
- Working tree có docs orchestrator chưa commit (`AGENTS.md`, report review PR #70, task này): mang vào PR, không reset/stash/discard.
- Đọc `AGENTS.md` (bất biến sản phẩm), [handoff M8a](../m8a-pilot-entry-hardening.md) (proxy/cookie/Origin, smoke HTTPS), [handoff M9](../m9-lan-demo-deployment.md) (demo đang chạy) và `web/` hiện tại.

## 2. Phạm vi

Toàn bộ trang trong `web/app`:

- đăng nhập, mời, quên/đặt lại mật khẩu, tài khoản;
- thư viện/trang chủ + upload, chi tiết item;
- bộ sưu tập, tìm kiếm & hỏi đáp RAG, khám phá, nguồn;
- Admin (mời, tài khoản, review, publications).

Việc cần làm:

- Hệ thống thiết kế: CSS variables cho màu/spacing/typography/radius, global stylesheet + CSS Modules, component dùng chung (button, input, card, badge trạng thái, alert, empty state, dialog xác nhận, bảng).
- App shell có điều hướng theo vai trò (user/admin), responsive tới 360 px, tôn trọng `prefers-reduced-motion`, dark mode nếu làm nhất quán.
- Màn hình trọng điểm cho demo:
  - **tìm kiếm & RAG:** chọn chế độ keyword/hybrid, phạm vi, kết quả có đoạn trích, trang/vị trí; câu trả lời có trích dẫn đánh số; panel nguồn mở đúng file/trang;
  - **thư viện + upload:** bước đồng ý rõ ràng, trạng thái xử lý bằng badge;
  - **chi tiết item:** phiên bản/file, độ phủ trích xuất, hành động có xác nhận;
  - **Admin:** bảng gọn, chip trạng thái, xác nhận trước thao tác nguy hiểm.
- Thay toàn bộ `style={{…}}` ad-hoc bằng class/token. Giữ text tiếng Việt; thông báo đồng ý/giới hạn/lỗi được trình bày lại, **không** bỏ hoặc làm nhẹ nghĩa.

**Không làm:**

- đổi API, route API, body request, xử lý CSRF/Origin, proxy `web/lib/proxy.ts`, logic quyền;
- thêm tính năng mới (streaming RAG, graph, tóm tắt AI…);
- font/ảnh/script từ mạng ngoài;
- thêm dependency npm runtime (dev dependency chỉ khi thật cần, ghi lý do).

Logic trong `web/lib/*` chỉ được tách/di chuyển khi test giữ xanh và hành vi giữ nguyên.

## 3. Kiểm chứng

Python Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe`; context ngoài repo; không đọc `.env`; không đụng dev `ras`/Dify/Tailscale/Radmin; không `taskkill` theo tên process.

1. `npm ci|test|run typecheck|run build --prefix web`: tất cả pass. Test hiện có không được làm lỏng kỳ vọng.
2. **Smoke HTTPS cô lập M8a** (`scripts/m8a_smoke.py`, build web từ source, CA/SMTP thử, provider tắt): HTTP và Chrome phải pass.
   - Nếu selector của smoke Chrome dựa vào text/cấu trúc đã đổi có chủ đích, chỉ cập nhật selector, giữ nguyên ý nghĩa từng kiểm; ghi rõ từng chỗ.
3. **Kiểm trực quan** bằng Playwright/Chrome trên stack cô lập: mọi trang chính ở 1440 px và 390 px; khách / user / admin; trạng thái rỗng, đang tải, lỗi, nội dung dài.
   - Màn RAG có câu trả lời: dùng stack C4 với provider giả (`scripts/c4_stack.py … fake-on`); không gọi provider thật.
   - Kiểm bàn phím/focus ở đăng nhập, upload, tìm kiếm/RAG và một trang Admin; kiểm tương phản màu chính.
4. Chọn **6–10 ảnh chụp đẹp** từ stack tổng hợp, lưu vào `docs/screenshots/` để dùng cho README/CV. Ảnh chỉ chứa dữ liệu tổng hợp hoặc paper giấy phép mở; không email/Gmail thật, IP, token hay secret.

## 4. Đưa lên bản demo `ras-pilot`

- Build web image mới từ source của PR, ví dụ tag `ras-pilot-web:ui1`; **API image rc2 giữ nguyên**.
- Cập nhật demo qua `scripts/m9_lan_demo.py`: thêm tùy chọn đổi web image vào state, để `rebind`/`provider` sinh lại env không quay về image cũ. Recreate **chỉ** service `web`, kiểm readiness và HTTPS `ras-demo.lan:8443` (IP Ethernet hiện tại).
- Ghi lệnh quay lại `ras-pilot-web:rc2` nếu cần.
- Không hạ/xóa demo, không đụng dữ liệu, 12 paper hay tài khoản chủ dự án đã tạo.

## 5. Bàn giao

- Một PR tiếng Việt, không merge, giữ author/committer, không AI co-author/trailer/promo.
- Handoff `docs/m10-frontend-ui.md` gồm:
  - cấu trúc style/component;
  - ảnh chụp trước/sau (ngoài repo, chỉ bộ chọn lọc trong `docs/screenshots/`);
  - lệnh đã chạy, kết quả pass/fail/not-run;
  - chỗ cập nhật selector smoke;
  - cách đổi web image của demo và quay lại.
- Cập nhật kế hoạch MVP và mục lục docs ngắn gọn.

**Inventory khi giao:** chỉ đọc Git/docs/`web/` và xác minh merge PR #70; chưa chạy app/test. Task **mới giao/chưa triển khai**.
