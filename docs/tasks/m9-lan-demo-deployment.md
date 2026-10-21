# M9 — triển khai demo trong mạng LAN trên máy của chủ dự án

Bạn là coding/ops agent tiếp theo. Dựng bản **rc2** thành một bản chạy thật, dùng được để **demo**. Bản này chạy trên chính máy Windows của chủ dự án, chỉ truy cập trong mạng LAN, có HTTPS, gửi mail qua Gmail của chủ dự án, có backup/alert theo lịch.

Mục tiêu do chủ dự án nêu (2026-10-21): dự án để làm portfolio/CV. Cần chạy ổn định và demo được các tính năng chính; không phục vụ nhóm lớn hay lâu dài. Không thêm tính năng.

## 1. Base và ủy quyền

- Base: `origin/main = 7009a8483e4f9891f11a966f114df136c90c4953` (merge PR #68), source **0052**.
- Fetch lại, tạo `ops/m9-lan-demo-deployment` từ `origin/main`. Mang mọi docs orchestrator modified/untracked vào PR; không reset/stash/discard.
- **Chủ dự án đã quyết định trong hội thoại (2026-10-21):**
  - host trên máy này;
  - **chỉ LAN**;
  - **không đụng tailnet** (Tailscale) hay Radmin VPN;
  - **chấp nhận chỉ lưu trên máy này**: máy hỏng thì các thao tác thu hồi/xóa sau lần backup cuối có thể mất;
  - gửi mail qua một Gmail mới với app password.
- **Được phép**, chỉ ở mức cần cho deploy này:
  - tạo project Docker riêng;
  - tạo thư mục dưới `%USERPROFILE%\ras-pilot\`;
  - tạo **một** rule Windows Firewall có tên bắt đầu `RAS pilot`;
  - tạo scheduled task Windows có tên bắt đầu `RAS pilot`;
  - gửi mail thật tới **địa chỉ của chính chủ dự án** để kiểm.
- **Không:**
  - đụng dev `ras`, Dify (đang giữ 80/443), Tailscale, Radmin;
  - đổi network category của Windows;
  - đọc `.env`;
  - dùng hoặc đọc bất kỳ key OpenRouter nào. Provider để trống, `pricing_verified=false`: search/RAG cần provider sẽ hiện thông báo chưa khả dụng, đúng hành vi đã kiểm.

## 2. Image

- Dùng đúng image rc2 đang có trên máy: `rasm8c-api:rc2` (`c0936a81…`), `rasm8c-ops:rc2` (`22656406…`), `rasm8c-web:rc` (`59cb5141…`), cùng hạ tầng đã pin digest trong `deploy/compose.yaml`.
- **Không build lại.** Kiểm `docker image inspect` ra đúng ID trước khi chạy.
- Nếu buộc phải sửa code app: dừng, báo orchestrator. Không tự build rc3.

## 3. Mạng, HTTPS, tường lửa

- Edge chỉ publish trên IP Ethernet LAN của máy, kiểm tại thời điểm chạy (lúc giao: `192.168.69.97/22`). Không publish `0.0.0.0`, vì máy còn interface Tailscale/Radmin.
- Cổng **8443** (HTTPS); nếu cần redirect thì **8080**. Không dùng 80/443.
- Sau khi dựng, kiểm:
  - cổng **không** mở trên IP Tailscale (`100.x`) hay Radmin (`26.x`);
  - từ một thiết bị khác trong LAN thì vào được.
- HTTPS bằng **CA riêng cho demo** (như CA thử của smoke M8a, hoặc Caddy internal CA):
  - certificate cho một hostname demo, ví dụ `ras-demo.lan` (kèm IP SAN nếu hữu ích);
  - private key CA giữ ngoài repo, quyền hẹp;
  - hướng dẫn cài CA cert và thêm dòng hosts trên thiết bị demo (Windows, Android/iOS nếu được).
  - Không tắt kiểm tra certificate để đi tắt; cookie `__Host-`/Secure cần HTTPS thật.
- Ethernet đang ở profile **Public**. Tạo một inbound rule duy nhất:
  - TCP 8443 (và 8080 nếu dùng);
  - `RemoteAddress LocalSubnet`, đúng profile hiện tại, tên `RAS pilot …`;
  - ghi lệnh gỡ.
- IP DHCP có thể đổi: ghi cách cập nhật (đổi bind + CA SAN/hosts), khuyên chủ dự án đặt DHCP reservation trên router.

## 4. SMTP Gmail

- Chủ dự án tự tạo hai file trong `%USERPROFILE%\ras-pilot\secrets\` (thư mục đã có, chỉ user/SYSTEM/Administrators truy cập được):
  - `smtp_username`: địa chỉ Gmail;
  - `smtp_password`: app password, đã bỏ khoảng trắng.
- Agent **không** in, log hay copy nội dung `smtp_password` ra ngoài thư mục secret; chỉ mount qua Compose secret.
  - Đọc địa chỉ Gmail từ `smtp_username` để điền `RAS_SMTP_USERNAME`/`RAS_SMTP_SENDER` vào env file ngoài repo.
  - Không ghi địa chỉ đó vào repo.
- Cấu hình: `smtp.gmail.com`, port 465, `implicit_tls`, xác minh certificate bằng CA hệ thống.
- Người nhận alert mặc định là chính địa chỉ Gmail đó, trừ khi chủ dự án nêu địa chỉ khác.
- Kiểm thật: một mail alert test và một lời mời tới địa chỉ của chủ dự án.
  - Lỗi đăng nhập Gmail thì báo mã lỗi; không thử lại liên tục.

## 5. Vận hành theo quyết định "chỉ trên máy này"

- Giữ cơ chế M8b. Đích replica journal và đích backup là **một MinIO riêng trên cùng máy** (project và volume tách khỏi pilot), ghi rõ **không** độc lập miền sự cố, đúng rủi ro chủ dự án đã chấp nhận.
- Khóa `age`:
  - tạo cặp khóa, chỉ public key cho deploy;
  - private key giao chủ dự án cất ngoài máy (USB/password manager);
  - ghi rõ mất private key là mất khả năng restore backup.
- Lịch bằng **Windows Task Scheduler** (thay systemd): `ras_ops.py backup` hằng ngày lúc 03:00 giờ máy, `ras_ops.py check` mỗi 15 phút, chạy bằng Python Conda `ras`.
  - Docker Desktop chỉ chạy khi có người đăng nhập: ghi rõ giới hạn.
  - Ghi cách tắt sleep và cảnh báo về Windows Update.
- Chạy một lượt backup, một lượt check và một drill restore trên project scratch. Drill dùng thư mục riêng; công cụ đã từ chối thư mục dùng chung.

## 6. Cài đặt và nghiệm thu demo

- Cài mới theo runbook M8c mục 8, với: env file ngoài repo không tên `.env`, project `ras-pilot`, image rc2, các khác biệt Windows/LAN ở trên.
- Bootstrap Admin: **chủ dự án tự chạy** lệnh TTY và nhập mật khẩu; agent chỉ chuẩn bị lệnh.
- Nghiệm thu từ một thiết bị LAN khác qua HTTPS (đã cài CA):
  - login/logout;
  - mời user qua Gmail thật tới địa chỉ chủ dự án → accept → login; reset mật khẩu;
  - upload có xác nhận, thư viện, collection, tải original;
  - review → approve → publish → revoke;
  - thông báo search/RAG khi chưa có provider.
- Kiểm thêm:
  - edge 503 trong cửa sổ backup;
  - mail alert tới Gmail.
- **Ghi rõ những gì chưa demo được khi chưa có provider**: index full-text, search hybrid, RAG có citation. Các phần này cần key OpenRouter thuộc tài khoản của chủ dự án; làm ở M7c sau.

## 7. Bàn giao

- Một PR tiếng Việt, không merge, giữ author/committer, không AI co-author/trailer/promo.
- Handoff `docs/m9-lan-demo-deployment.md` gồm:
  - topology thực tế (IP, cổng, project, thư mục, digest);
  - lệnh đã chạy, kết quả nghiệm thu;
  - **hướng dẫn cho chủ dự án**: bật/tắt, sau khi khởi động lại máy, cài CA trên thiết bị demo, kịch bản demo 5–10 phút;
  - cách gỡ sạch: project/volume, firewall rule, scheduled task, thư mục.
- Không đưa secret, địa chỉ Gmail, private key CA/age hay IP cá nhân dư thừa vào repo. Handoff dùng placeholder nếu cần.
- Cập nhật kế hoạch MVP: checklist "Vận hành nhóm thử" ghi rõ là bản demo LAN, lưu trữ cùng máy theo quyết định chủ dự án.
- Docker inventory trước/sau, chỉ tạo/dọn tài nguyên của task. Không `taskkill` theo tên process.

**Inventory khi giao:** đã kiểm read-only: tài nguyên máy, cổng 80/443 do Dify giữ, 8080/8443 trống, IP Ethernet và profile Public; đã tạo thư mục rỗng `%USERPROFILE%\ras-pilot\secrets`. Chưa chạy deploy. Task **mới giao/chưa triển khai**.
