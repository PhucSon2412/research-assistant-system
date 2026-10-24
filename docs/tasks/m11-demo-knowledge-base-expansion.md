# M11 — chuyển sang dùng cá nhân trên máy và mở rộng kho paper

Bạn là agent tiếp theo. **Cập nhật 2026-10-23:** chủ dự án không cần demo LAN nữa; dự án đã xong chức năng, từ nay dùng cho học tập cá nhân trên chính máy này. Làm **một PR** gồm script, manifest và docs, với hai việc:

1. Chuyển bản `ras-pilot` đang chạy sang **chỉ truy cập từ máy này** (mục 0).
2. Nạp thêm khoảng **300 paper arXiv giấy phép mở** cùng chủ đề vào **tài khoản user demo hiện có** (mục 2–4), theo chỉ dẫn mới của chủ dự án ngày 2026-10-23 (thay mục tiêu 150 trước đó).

Không đổi code ứng dụng, image hay `deploy/`.

**Mở rộng do chủ dự án yêu cầu ngày 2026-10-24:** sau khi mở Docker Desktop trở lại,
chủ dự án yêu cầu “nạp thêm khoảng 1000 paper” và đã xác nhận **thêm 1.000 ngoài lô 300,
tổng mục tiêu khoảng 1.300 paper mới**. **Không tăng trần $30/dừng $24**, không nới quota ứng dụng.
Cùng tài khoản, consent theo lô, tiêu chí license/version/chủ đề và ledger M11 hiện có.
Giữ 300 paper đầu và checkpoint; không nạp lại receipt đã có.

**Cập nhật ngân sách mới hơn ngày 2026-10-24:** chủ dự án yêu cầu dùng **$50 cho index**.
Áp dụng trần tổng M11 **$50**, giữ quy tắc dừng ở **$40 (80%)**, tính cả ledger cũ;
không tự tăng quota/budget ngày của ứng dụng hoặc đổi model để tiêu hết tiền.
Chủ dự án đã chọn **tiếp tục mở rộng tới giới hạn ngân sách**, vượt mốc 1.300 nếu còn trần.
Giữ $0,813 trần cho 15 câu thử; phần index dừng nhận mới ở $39,187 và drain job đã có receipt.
Không đặt mục tiêu tiêu hết đúng $50 bằng đổi model hoặc nạp lại file đã có.
Probe `/key` qua env demo xác nhận `limit_reset=daily`: số khoảng $96 là hạn mức key còn trong ngày,
không phải bằng chứng số dư ví. Những trần $30/$24 phía dưới là lịch sử trước chỉ dẫn mới này.

## 0. Chuyển sang chỉ truy cập trên máy

- Edge chỉ publish `127.0.0.1:8443`, không còn bind IP Ethernet/LAN, để demo chạy được dù máy đang dùng Wi-Fi hay Ethernet và không mở ra mạng.
  - Mở rộng `scripts/m9_lan_demo.py` một chế độ local tường minh (ví dụ `rebind --local`): lưu vào state để `provider`/`web-image` không quay về LAN, cấp lại leaf certificate từ CA demo hiện có cho `ras-demo.lan` (không đổi CA), cập nhật env/overlay ngoài repo.
  - Chỉ recreate `edge` (và service nào thật sự cần). Dữ liệu, tài khoản, 12 paper, backup và lịch giữ nguyên.
- Kiểm:
  - `127.0.0.1:8443` nghe;
  - IP Ethernet/Wi-Fi/Tailscale/Radmin **đóng**;
  - HTTPS `https://ras-demo.lan:8443` trả 200 khi hosts trỏ `127.0.0.1`.
- Chuẩn bị lệnh cho chủ dự án tự chạy (cần quyền Administrator): sửa dòng hosts trên Windows thành `127.0.0.1 ras-demo.lan`; gỡ firewall rule `RAS pilot LAN demo HTTPS` (không còn cần). Ghi luôn lệnh gỡ CA/hosts trên máy Ubuntu cũ. Agent không tự sửa hosts/firewall.
- Cập nhật handoff M9: chế độ hiện hành là local; giữ hướng dẫn LAN như tùy chọn lịch sử, không xóa.

## 1. Base và ủy quyền

- Base: `origin/main = 1f7bfac970fa0015a6d3211d3514a4136df4d4d6` (merge PR #71). Fetch lại, xác minh, tạo `ops/m11-demo-knowledge-base` từ `origin/main`.
- Mang theo mọi docs đang modified/untracked: README mới, `docs/status-log-archive.md`, report review PR #71, task này, cập nhật AGENTS/decisions/kế hoạch. Không reset/stash/discard.
- **Chủ dự án xác nhận (2026-10-23):**
  - nhóm đồng ý dùng phần còn lại của key OpenRouter (khoảng $30,9 lúc kiểm);
  - chủ dự án đồng ý xử lý ngoài cho **đúng lô paper giấy phép mở của task này**. Đây là sự đồng ý của chính chủ tài khoản upload, thay cho việc bấm xác nhận từng file trên UI.
- **Trần của task** (chủ dự án cập nhật 2026-10-23): ≤ **$30** tổng; ledger fail-closed, dừng gọi mới ở **$24 (80 %)**. Budget ứng dụng $10/ngày và quota hiện có của demo giữ nguyên; nếu quota ngày làm chậm việc index thì chia nhiều ngày, không nới budget.
- Theo chỉ dẫn M11 mới: key chỉ dùng **qua env đã có sẵn trong demo**; agent M11 không đọc file key hay `.env`.
- Không đụng dev `ras`/Dify/Tailscale/Radmin. Không hạ, xóa hay restore demo. Không `taskkill` theo tên process.

## 2. Chọn paper

- Khoảng **300 paper bổ sung**, chủ đề *LLM & information retrieval*:
  - retrieval/dense retrieval, reranking, RAG và đánh giá RAG;
  - hallucination/grounding, long context, embedding, agent dùng công cụ tìm kiếm.
- Không trùng 12 paper đang có (so arXiv ID và SHA-256).
- Chỉ version có license **CC BY 4.0, CC BY-SA 4.0 hoặc CC0**, kiểm theo metadata arXiv của **đúng version** (như M7c). PDF ≤ giới hạn upload của cấu hình (kiểm `IngestPolicy`); bỏ file quá lớn và ghi lý do.
- Tải lịch sự (≥ 3 giây giữa request arXiv, User-Agent mô tả); có checkpoint để chạy tiếp, không tải lại file đã có.
- Lưu PDF ngoài repo tại `%USERPROFILE%\ras-pilot\demo-papers\` kèm `ATTRIBUTION.md` cập nhật. Repo chỉ giữ manifest (ID, version, title, tác giả, license URL, SHA-256, kích thước, chủ đề) và script tải lại; **không commit PDF/toàn văn**.

## 3. Nạp vào demo qua API thật

- **Tài khoản upload:**
  - dùng **tài khoản user demo hiện có** (`…+demo@gmail.com`), để mọi paper nằm chung một thư viện với 12 paper cũ;
  - email và mật khẩu nằm trong `%USERPROFILE%\ras-pilot\secrets\library_user_email` và `library_user_password` (chủ dự án tự tạo);
  - không in/log/copy mật khẩu; đăng nhập một lần và giữ phiên;
  - tôn trọng rate limit đăng nhập.
- Đi đúng luồng sản phẩm cho từng file: lấy notice → tạo upload intent có acknowledgement → PUT file → theo dõi job tới `indexed` hoặc trạng thái cuối. Không gọi endpoint/SQL nội bộ, không bỏ bước intent/consent. Ghi rõ trong handoff rằng consent là sự đồng ý theo lô của chủ dự án.
- Sau khi index: đặt **tiêu đề thật** theo metadata arXiv (route sửa tiêu đề hiện có).
- Tạo 5–8 **collection theo chủ đề** và thêm paper qua API collection.
- Theo dõi chi phí qua ledger. Index đồng thời ở mức an toàn: không làm demo chậm rõ rệt, không vượt giới hạn concurrent.
- Item lỗi (PDF không trích xuất được, cần xem lại) giữ nguyên trạng thái sản phẩm, liệt kê trong báo cáo; không xóa để đẹp số.

## 4. Kiểm và cập nhật tài liệu

- Đếm theo trạng thái: indexed / cần xem lại / lỗi / bỏ qua. Ghi tổng trang, chunk, dung lượng object và chi phí thật.
- Chạy 10–15 câu hỏi demo: một paper, so sánh hai paper nêu tên, không có đáp án; tìm kiếm hybrid.
  - Ghi kết quả và các câu nên dùng khi demo vào kịch bản demo của [handoff M9](../m9-lan-demo-deployment.md).
  - Không chạy lại đánh giá M7c, không đổi dữ liệu đánh giá.
- Chạy một lượt backup sau khi nạp (`ras_ops backup`); ghi thời gian cửa sổ bảo trì mới và dung lượng backup. Kiểm `ras_ops check` không có alert mới ngoài dự kiến.
- Cập nhật `README.md`: số paper/collection của demo (không đổi phần đánh giá M7c). Cập nhật kế hoạch MVP ngắn gọn.

## 5. Bàn giao

- Một PR tiếng Việt, không merge, giữ author/committer, không AI co-author/trailer/promo. Đợi CI GitHub chạy xong và báo đúng kết quả.
- Handoff `docs/m11-demo-knowledge-base.md`:
  - tiêu chí chọn paper, số lượng theo trạng thái, danh sách collection;
  - chi phí ledger (billed/cam kết);
  - backup trước/sau;
  - lệnh chạy lại;
  - các câu hỏi demo gợi ý.

**Inventory khi giao:** chỉ đọc Git/docs, xác minh merge PR #71; chưa tải paper hay gọi provider. Task **mới giao/chưa triển khai**.
