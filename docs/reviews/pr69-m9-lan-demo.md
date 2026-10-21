# Review PR #69 — M9 demo LAN

**Kết luận tại `209d92b` (2026-10-21): không thấy điểm chặn merge.** PR không đổi code app, `deploy/compose.yaml` hay CI. PR thêm `scripts/m9_lan_demo.py`, `deploy/lan-demo/run_ops_task.py` và docs. Bản demo `ras-pilot` đang chạy trên máy chủ dự án.

## Reviewer kiểm trực tiếp (chỉ request đọc hoặc request bị từ chối; không tạo tài khoản/dữ liệu)

- **Overlay ngoài repo** (`%USERPROFILE%\ras-pilot\pilot\override.yaml`):
  - chỉ thêm nhãn `ras.m9.demo`, edge publish `${RAS_EDGE_BIND}:8443:8443`, `RAS_PUBLIC_HOST`/site có cổng, `RAS_HTTP__ALLOWED_ORIGINS`, nối replicator/ops/ops-check vào mạng offsite;
  - không inline secret.
  - Env (chỉ đọc dòng không bí mật): Origin đúng một giá trị `https://ras-demo.lan:8443`; pricing false cả ba route; image `ras-pilot-*:rc2`.
- **HTTPS qua Python `ssl` với CA demo, SNI `ras-demo.lan`:**
  - `/login` 200, `/api/auth/session` 401;
  - `/api/admin/dev-outbox`, `/readyz`, `/healthz`, `/docs`, `/openapi.json` đều 404 ở edge;
  - HSTS `max-age=86400`; trust store hệ thống từ chối (`CERTIFICATE_VERIFY_FAILED`).
  - `curl.exe` (Schannel) bỏ qua `--cacert` nên ra 000; đó là lỗi công cụ của reviewer, không tính.
- **CSRF:** cookie `ras_csrf` từ `/api/auth/csrf` có `Secure; SameSite=lax`. POST login kèm double-submit:
  - Origin `https://ras-demo.lan:8443` → **401 `invalid_credentials`** (qua CSRF);
  - Origin thiếu cổng hoặc lạ → 403 `csrf_failed`.
  - Lượt đầu thiếu token nên 403 ở mọi Origin; đó là thiếu sót của request thử, không phải lỗi demo.
- **Bind:** 8443 chỉ nghe trên IP Ethernet; kết nối thử tới 9 IPv4 khác của máy (Tailscale/Radmin/WSL/loopback…) đều đóng.
- **CA demo:** name constraints critical, chỉ cho phép `ras-demo.lan`, 10/8, 172.16/12, 192.168/16; `CA:TRUE pathlen 0`, hạn 2029-10-20. Thư mục `ca\` chỉ user hiện tại, không kế thừa quyền.
- **Container:** 13 container có nhãn demo đều Up/healthy hoặc one-shot exit 0. Scheduled task `RAS pilot backup`/`check`: Ready, user thường (Limited), lastResult 0.
- **Script:** `uninstall` cần `--yes`, kiểm nhãn `ras.m9.demo` + project trước `down -v`, chỉ ba project cố định. Quét diff `7009a84..209d92b`: không có địa chỉ Gmail thật, IP VPN, khóa riêng; chỉ placeholder.

## Ghi chú, không chặn

- Rule `Docker Desktop Backend` có sẵn cho phép mọi cổng trên Public/Private. Lớp giới hạn thực là bind IP Ethernet; mọi máy cùng subnet `/22` đều mở được trang login.
- Chủ dự án còn phải làm:
  - firewall rule tường minh;
  - bootstrap Admin;
  - cất `age-identity.txt` ra ngoài máy (hiện vẫn còn trên máy);
  - bật Docker Desktop tự khởi động;
  - nghiệm thu từ thiết bị LAN khác.
- Điện thoại cần DNS nội bộ, nên demo bằng laptop Windows.
- CI run `39546428086`: deploy contract và web pass; job API còn chạy lúc review.
