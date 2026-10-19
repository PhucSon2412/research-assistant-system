# M8a — đường vào nhóm thử: HTTPS, SMTP TLS, rate limit auth, CI

Task: [M8a](tasks/m8a-pilot-entry-hardening.md). Base `origin/main = 3b59a1db4e784df0891a844ba968ba85e2797746`
(merge PR #65), nhánh `feat/m8a-pilot-entry-hardening`. Source migration giữ **0052**: không thêm migration,
grant hay bảng auth. Kết quả kiểm ở [inventory M8a](m8a-test-inventory.json).

**Trạng thái:** đã triển khai và kiểm trên project Docker cô lập với CA/SMTP thử; **chưa có** domain, host,
tài khoản SMTP, operator hay nơi lưu journal độc lập thật; **chưa cutover, chưa nghiệm thu production**.
Workflow CI đã viết và các bước của nó đã chạy local; **chưa có run trên GitHub** tại thời điểm viết. Không đọc
`.env`/key, không gọi provider/arXiv, không gửi mail thật, `pricing_verified` giữ false, không đụng dev `ras`/Dify.
Không điền phiếu human M7b.

## Topology và ranh giới tin cậy

`deploy/compose.yaml` là cấu hình riêng cho nhóm thử, không phải `docker-compose.yml` dev được bật bằng biến.
Mọi lệnh: `docker compose --env-file <pilot.env ngoài repo> -p <project> -f deploy/compose.yaml <lệnh>`;
mẫu biến ở [`deploy/pilot.env.example`](../deploy/pilot.env.example).

| Mạng | Thành viên | Ghi chú |
|---|---|---|
| `public` | edge (Caddy) | bridge thường; chỉ edge publish `80`/`443` (`RAS_EDGE_BIND`) |
| `edge` | edge, web | internal |
| `backend` | web (IP cố định), api, worker, beat, db, redis, minio, migrate/provision/recovery-init | internal, subnet cố định `RAS_BACKEND_SUBNET` |
| `egress` | api, worker | đi ra SMTP relay (và provider/arXiv chỉ khi được bật sau này) |

DB/API/Redis/MinIO/web không có port host. Edge chặn `/api/admin/dev-outbox`, `/docs`, `/redoc`,
`/openapi.json`, `/healthz`, `/readyz` (404); HTTP `:80` chỉ redirect 308 sang HTTPS. HSTS
`max-age=RAS_HSTS_MAX_AGE` (mẫu 86400, khởi đầu khi domain còn có thể đổi; chưa preload).

Chuỗi proxy **edge → web → API**:

1. Caddy ghi đè `X-RAS-Client-IP` bằng TCP peer (`{remote_host}`), thêm `X-RAS-Edge-Token` đọc từ secret
   file, xóa `X-Forwarded-For`/`-Host`/`-Proto`, `X-Real-IP`, `Forwarded`, `X-Client-IP`, `True-Client-IP`,
   `CF-Connecting-IP` do client gửi.
2. Web (`web/lib/proxy.ts`) chỉ chuyển một `X-RAS-Client-IP` hợp lệ khi token khớp `WEB_EDGE_TOKEN_FILE`
   (so sánh hằng thời gian); không bao giờ chuyển token edge hay `X-Forwarded-For`. File token cấu hình mà
   đọc lỗi/quá ngắn → proxy trả 503 `api_not_configured`, không tin header. Không cấu hình (dev) → không chuyển.
3. API chỉ tin header từ peer trong `RAS_HTTP__TRUSTED_PROXY_CIDRS` (deploy: đúng IP web `/32`; settings cấm `/0`)
   và đúng một giá trị IP hợp lệ; ngược lại dùng TCP peer. Uvicorn chạy `--no-proxy-headers`.

Role/secret theo C4: api/worker/beat dùng LOGIN runtime; migrate/provision/recovery-init dùng migrator; định nghĩa
recovery/definer như C4; operator C3 vẫn chạy riêng, không gắn vào API. Mật khẩu DB/Redis/MinIO ở env file
deployment; `edge_token`, `rate_limit_key`, `smtp_password` là file trong `RAS_SECRETS_DIR` (Compose secrets,
read-only). Journal là bind mount bắt buộc `RAS_JOURNAL_DIR`, không có volume mặc định. Cùng máy với DB/object
store thì **không** phải miền sự cố độc lập (M8b).

## SMTP qua TLS

`RAS_DELIVERY__MODE=smtp` dùng `SmtpDelivery` trên interface `DeliveryAdapter`/`OutgoingMessage` sẵn có.
`security` chỉ là `implicit_tls` (SMTPS) hoặc `starttls` (bắt buộc; server không mời STARTTLS → lỗi). Luôn
`CERT_REQUIRED` + kiểm hostname, TLS ≥ 1.2; không có mode plaintext hay khóa tắt verify (khóa lạ như `verify`
bị settings từ chối). `ca_file` thay trust store hệ thống (CA relay riêng hoặc CA thử); để trống dùng CA của
image. Username đi cùng `password_file` (đọc mỗi lần gửi, giới hạn kích thước). Một deadline tổng
`total_timeout_seconds` (mặc định 10 s; connect 5 s) — giá trị khởi đầu chưa đo; web proxy mặc định 15 s.

Không đổi hợp đồng token/journal/compensation: lời mời chỉ gửi sau known commit và proof còn hiệu lực, không giữ
DB/journal lock qua network; gửi lỗi (`DeliveryFailed` với mã `smtp_timeout`, `smtp_rejected`,
`smtp_tls_verify_failed`, `smtp_connection_failed`, …) → bù thu hồi lời mời (`delivery_failed`) và API trả
503 `delivery_failed`. Timeout/đứt kết nối sau DATA có thể đã được server xếp hàng: link trong mail đó đã bị
thu hồi nên không dùng được. Reset vẫn trả 202 chung, gửi sau response, kiểm token còn sống trước khi gửi; lỗi
gửi chỉ log mã. Không retry, không tạo lại token. Log chỉ có mã/kiểu lỗi, không email/link/token.

UI: thông báo tạo lời mời nói kênh gửi đã nhận, **chưa** xác nhận người nhận nhận được; 502/504 khi mời →
"không xác định kết quả, tải lại danh sách trước khi gửi lại".

## Rate limit auth

Route: `POST /auth/login` (nguồn + tài khoản), `POST /auth/password-reset/request` (nguồn + tài khoản),
`POST /auth/invitations/inspect|accept` và `POST /auth/password-reset/confirm` (nguồn, bucket `token`).
Kiểm ở đầu handler, trước khi Session mở connection; gate recovery và Origin/CSRF chạy trước và giữ câu trả lời
của chúng (gate đóng → 503 `service_recovering`, không đếm).

- Redis db `RAS_RATE_LIMIT__REDIS_DB` (1), một script Lua INCR + PTTL + PEXPIRE nguyên tử, sửa key mất TTL;
  cửa sổ cố định; request bị chặn vẫn đếm; thành công không xóa bộ đếm; không khóa account.
- Key chỉ chứa HMAC-SHA256 với `RAS_RATE_LIMIT__KEY_SECRET_FILE` (≥ 32 byte) của nguồn (IPv6 gộp `/64`) hoặc
  email chuẩn hóa — có hay không có account đều cùng bucket/policy/response.
- Bị chặn: **429** `{"error":"rate_limited","retry_after":N}` + `Retry-After: N`, không cookie. Redis/secret
  lỗi: **503** `{"error":"rate_limit_unavailable"}` + `Retry-After: 30`; `/healthz` và readiness không đổi.
  Production từ chối `mode=disabled`; dev compose đặt `disabled` tường minh.
- Mặc định (giá trị vận hành khởi đầu, chưa benchmark/SLO): login nguồn 30/300 s, tài khoản 10/900 s; reset
  nguồn 10/900 s, tài khoản 3/3600 s; token nguồn 20/900 s. Đổi qua
  `RAS_RATE_LIMIT__<RULE>__LIMIT` **và** `__WINDOW_SECONDS` (env lồng nhau thay cả rule).
- UI hiện "Quá nhiều yêu cầu… chờ khoảng N giây" và không tự gửi lại; proxy chuyển nguyên `Retry-After`.

Ngoài phạm vi: CAPTCHA, WAF, rate limit route khác/provider, đo timing enumeration của reset (vẫn mở).

## Runbook

Chuẩn bị (ngoài repo): copy `pilot.env.example`, điền mật khẩu sinh ngẫu nhiên, `RAS_PUBLIC_HOST`, SMTP relay;
tạo `RAS_SECRETS_DIR` với `edge_token` (≥ 32 ký tự, không khoảng trắng), `rate_limit_key` (≥ 32 byte),
`smtp_password` (có thể rỗng nếu relay không AUTH và `RAS_SMTP_USERNAME`/`RAS_SMTP_PASSWORD_FILE` để trống);
tạo `RAS_JOURNAL_DIR` cho UID 10001 đọc/ghi. Chọn `RAS_BACKEND_SUBNET` chưa dùng và `RAS_WEB_BACKEND_IP`
trong đó. Domain thật cần DNS trỏ về host và 80/443 mở để Caddy lấy ACME; certificate riêng thì đặt snippet
`tls <crt> <key>` vào `RAS_CADDY_TLS_DIR`.

**Cài mới:** `… build migrate web` (ghi digest) → `python scripts/m8a_deploy_check.py --env-file <env>
--settings-image <api image>` → `… up -d --wait` (migrate → provision → initialize-installation theo C4) →
`… run --rm --no-deps recovery-init python -m app.cli.recovery status` và readiness (`… exec api python -c`
gọi `http://127.0.0.1:8000/readyz` nội bộ; edge không public `/readyz`) →
`… exec api python -m app.cli.bootstrap_admin --email <email>` (TTY) → mở traffic.

**Nâng cấp có dữ liệu:** theo [runbook C4](c4-integrated-runtime-cutover.md#runbook-cutover-có-dữ-liệu--chưa-chạy-trên-dev):
dừng api/worker/beat/web/edge → `run --rm --no-deps recovery-init python -m app.cli.recovery hold` bằng image cũ
→ build image mới → `run --rm --no-deps migrate` → `run --rm --no-deps provision` →
`… recovery recover --hold-for-operator` (C3 nếu cần) → `… recovery recover` → kiểm `safety_ready`/readiness →
start api/worker/beat/web/edge → kiểm login/invite/reset → mở traffic. Không `register-existing
--confirm-db-is-current`, không xóa hold/checkpoint hay sửa journal để vượt lỗi.

## Kiểm chứng — cách chạy lại

Python Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe`; context luôn ngoài repo.

- Selection API: `scripts/m8a_test_host.py --context <ctx> setup`, rồi
  `… test --junit <xml> -- <file …>` (danh sách như `.github/workflows/ci.yml`), `… cleanup`.
- Smoke deploy: `scripts/m8a_smoke.py --context <ctx> setup | compose build migrate web | compose up -d --wait |
  http | browser | cleanup`. Browser cần image `docker build -t rasm8a-browser:local -f
  scripts/m8a_browser.Dockerfile scripts` (Google Chrome stable + NSS tools; chỉ cho smoke).
- Countercheck: `scripts/m8a_counterchecks.py --host-context <ctx test host> --out <dir>`.
- Web: `npm ci|test|run typecheck|run build --prefix web`; deploy: `scripts/m8a_deploy_check.py`.

Smoke dùng CA thử riêng: Caddy phục vụ certificate `ras.test` ký bởi CA đó; client host (httpx) và Chrome trong
container chỉ tin CA qua `cafile`/NSS DB riêng. Đối chứng: trust store hệ thống và Chrome không có CA đều từ
chối. Mạng `egress` của smoke là internal (không route ra ngoài); SMTP capture (`api/tests/smtp_capture.py`,
SMTPS + AUTH) là transport test nằm ngoài image. Cùng máy nên không chứng minh độc lập miền sự cố.

## Kết quả

Chứng cứ thô ở context ngoài repo `C:\Users\ADMIN\AppData\Local\Temp\ras-m8a-20261019\`; inventory giữ hash,
số liệu và nhãn check (không chép detail có thể chứa cookie/header của stack thử). Lượt cuối chạy trên source
đã chốt; các lượt lỗi/sửa giữ riêng bên dưới, không cộng dồn thành pass.

**Lượt cuối (source chốt):**

- Selection API lượt 6 (CI-equivalent, 22 file, `--deselect` 1 ca cần container C3): **1102 pass, 0 fail/error/skip**, 1 deselected, exit 0, 63 phút trên
PostgreSQL tmpfs/Redis/MinIO cô lập; JUnit đã quét không chứa secret context/link token. Đây là selection theo
rủi ro, không phải full suite.
- Smoke deploy thật `smoke3` (project `rasm8a426c22e7`, image API `sha256:841566ba…84bdcb`, web
  `sha256:5dbcbce3…4b4c49`): **HTTP 66/66**, **Chrome 16/16** (Google Chrome 155.0.8059.84 trong container,
  0 request ngoài origin). 211 file `app/`+`migrations/` trong image khớp bytes host; api/worker/beat cùng digest.
  Gồm: chỉ edge publish port (127.0.0.1 trong smoke); mạng public chỉ tới edge, egress không tới db/redis/minio/web;
  readiness thật; HTTP → 308, POST HTTP không được phục vụ; trust store hệ thống và Chrome không có CA đều từ chối;
  SNI lạ không có certificate; HSTS; dev-outbox/docs/health 404 ở edge; bootstrap CLI; cookie `__Host-ras_session`
  Secure/HttpOnly/SameSite=Lax/Path=/ không Domain; Origin `http://ras.test`/lạ/thiếu → 403; invite A/B qua
  SMTPS + AUTH → accept → login; ma trận private ACL (owner 200, User khác và Admin 404, kể cả file gốc); worker
  dừng ở `awaiting_index` (pricing false); RAG 503 `rag_generation_unavailable_unverified_pricing`; hybrid trên
  scope chưa index trả 200/0 hit/không gọi provider; reset A (account có/không cùng 202, không mail cho địa chỉ
  lạ, mật khẩu cũ 401, phiên cũ 401, token dùng lại 400); 429 + `Retry-After` qua edge dù client giả
  `X-RAS-Client-IP`/`X-Forwarded-For`/`X-Real-IP`/`Forwarded`/token edge; nguồn khác không bị chặn; hết
  `Retry-After` login lại được; kết nối thẳng API (mạng egress) và thẳng web với token giả đều bị khóa theo
  peer của chính nó; bucket tài khoản; reset limit giống nhau cho email có/không account và không gửi thêm mail;
  SMTP reject → 503 `delivery_failed` + lời mời `revoked`, reset vẫn 202 không mail; SMTP dừng → 503
  `delivery_failed`; Redis dừng → 503 `rate_limit_unavailable` + `Retry-After: 30`, readiness 200, phiên đọc được,
  Redis lại → login được (không khóa account); 25 lượt đọc sau lỗi vẫn 200; `recovery hold` → 503
  `service_recovering` và readiness 503, `recover` mở lại; journal mất → đóng, trả lại → mở; journal torn → đóng,
  khôi phục bytes → mở; log compose 0 token/mật khẩu/secret/email/`#token=`.
  Chrome: đối chứng không CA → `ERR_CERT_AUTHORITY_INVALID`; Admin mời từ UI (thông báo không hứa đã nhận);
  link invite/reset từ capture; mật khẩu cũ bị từ chối; upload có xác nhận; trạng thái "Đã trích xuất, chờ lập chỉ
  mục"; thư viện; collection + thêm item; hybrid 0 đoạn; keyword 200; 429 hiện thời gian chờ và đúng 6 POST
  cho 6 lần bấm (không tự gửi lại).
- Countercheck **7/7 giữ** trên bản sao ngoài repo, source gốc không đổi (hash trước/sau trùng): TLS verify tắt,
  production nhận Origin `http://`, tin mọi peer, limiter không nguyên tử (threads + hai process), fail-open,
  gửi mail khi proof trước gửi đã mất, web chuyển IP không cần token edge — mỗi đột biến làm fail đúng test nhắm.
- Web: `npm test` **79/79**, typecheck và build exit 0. Deploy contract ok, `Settings` production kiểm trong image
  smoke3; `caddy validate` "Valid configuration".
- CI: `.github/workflows/ci.yml` đã viết; các bước tương ứng đã chạy local như trên. Run GitHub đầu tiên
  (PR #66, run `39266920892`): **web pass, deploy contract pass, API fail ở bước dựng hạ tầng** vì runner không kéo
  được `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` ("unauthorized"); từ máy này manifest tag đó (và
  `latest`, cả bản Docker Hub) cũng không còn tải ẩn danh — máy local chỉ chạy được nhờ image đã cache. Đây là
  phát hiện ảnh hưởng **cài mới** bằng dev/deploy compose, không phải hành vi M8a; nguồn image MinIO cho triển
  khai là quyết định mở của chủ dự án. CI và harness nay nhận `RAS_TEST_MINIO_IMAGE`, CI dùng
  `cgr.dev/chainguard/minio@sha256:709099…e510` (đã thử local: 114 test storage/limiter pass). Kết quả run sau
  sửa xem trên PR; handoff không tự nhận xanh.
  Timeout job API 100 phút vì selection mất ~63 phút trên máy này (thời gian runner GitHub chưa đo).
- Sau lượt cuối chỉ đổi line ending CRLF → LF của bốn file working copy (`api/tests/test_c2a_origin.py`,
  `docker-compose.yml`, `docs/dev-setup.md`, `web/tests/proxy.test.mjs`; nội dung giữ nguyên, blob Git vốn là LF)
  và sinh inventory; hash trong inventory là bytes LF đã commit.

**Failure/sửa trong quá trình (giữ lịch sử):**

- SMTP test lượt đầu 20/25: kỳ vọng `rcpt` sai; smtplib bọc timeout đọc reply thành `SMTPServerDisconnected`
  (adapter sửa: phân loại theo `__context__` → `smtp_timeout`); Windows báo port đóng sau ~2 s nên ca refused
  dùng timeout dài hơn. Sau sửa 25/25.
- Ca hết cửa sổ limiter fail 1/4 lần lặp với một `sleep` cố định; đồng hồ Redis trong VM Docker Desktop đo chạy
  nhanh hơn host ~3 %. Đổi sang poll có hạn 5 s; không đổi semantics limiter.
- Selection lượt 1 exit 4 (argparse nuốt `--junit` sau action — sửa harness). Lượt 2: 344 pass/49 fail/709 error
  vì fixture autouse đặt `RAS_RATE_LIMIT__MODE=disabled` qua env làm Settings production hợp lệ bị từ chối
  → đổi sang thay factory limiter trong test. Lượt 3: 646 pass/63 fail/393 error do harness thiếu
  `RAS_C3_OPERATOR_PASSWORD` và `RAS_DB__*` mặc định như harness C2d. Lượt 4 dừng giữa chừng (thiếu
  `MINIO_ROOT_*`/`MINIO_API_HOST_PORT` cho fixture upload). Lượt 5: 1067 pass/35 fail, chỉ ở
  `test_c2a_origin.py`: production nay từ chối Origin `http://` (yêu cầu M8a), fixture cũ dùng
  `http://web.test` ở production mặc định → đổi fixture sang `https://web.test`, giữ cùng các ca sai
  scheme/host/port/path.
- Web lượt đầu 78/79: test cấu hình proxy cũ so sánh object thiếu trường `edgeToken` mới → cập nhật kỳ vọng.
- Smoke1: dừng ở kỳ vọng hybrid sai (hành vi thật: không có vector scope → 200 rỗng, không gọi provider).
  Smoke2: HTTP 65/65; Chrome lượt 1 fail vì capture đánh số file theo bộ nhớ process và ghi đè sau khi smoke
  restart SMTP (đổi tên file theo thời gian); lượt 2 fail vì đọc trạng thái trước khi trang client render
  (chờ có hạn); lượt 3 16/16. Smoke3 trên context mới là lượt nghiệm thu ở trên.

**Not-run:** full suite API (> 5.000 node) và năm restore drill (không đổi journal/restore/schema); ca
`test_two_dump_restore_rounds_keep_valid_provenance_and_restrict_forgery` (cần container C3); workflow trên
GitHub; ACME/domain/SMTP relay thật; STARTTLS trong smoke image (chỉ pytest; smoke dùng SMTPS); scale nhiều
replica API trong smoke (chia sẻ giữa process kiểm bằng hai process pytest với Redis thật).

**Cleanup và baseline:** inventory Docker chụp trước khi tạo tài nguyên (24 container, 188 volume, 9 network,
140 image) và sau khi dọn: cùng 24 container ID, 188 volume, 9 network; 138 image (đã gỡ
`caddy:2.10.2-alpine` và `mcr.microsoft.com/playwright/python:v1.63.0-noble` do task kéo — snapshot baseline
chụp sau khi hai lượt pull bắt đầu — cùng image `rasm8a-browser:local` và tag `rasm8a*-api|web:local`).
Baseline không so trạng thái chạy của container có sẵn; không query/restart chúng. Chỉ dọn project `rasm8a*`/`rasm8at*` sau khi kiểm label
`com.docker.compose.project` và `ras.m8a.context`; không đụng `ras`/Dify. Sự cố: khi dừng lượt selection 4 tôi đã
chạy một lệnh `taskkill` theo tên `python.exe` quá rộng; sau đó host không còn process `python.exe` nào và không
xác định được nó có dừng process Python ngoài task hay không. Container (process Linux) không bị ảnh hưởng.

## Giới hạn và phần chưa làm

- CI chỉ chạy selection, không phải full suite (> 5.000 test) và không chạy browser, image smoke hay năm restore
  drill. Chặn outbound trong CI là guard httpx trong pytest, không phải firewall.
- Image MinIO pin trong `docker-compose.yml`/`deploy/compose.yaml` không còn kéo ẩn danh được; host mới cần
  mirror/registry có quyền hoặc chọn image khác (chưa đổi trong PR này vì là quyết định nguồn phần mềm).
- Không có domain/ACME/certificate thật, SMTP relay/account thật, deliverability (SPF/DKIM/DMARC), host thật,
  operator thật hay journal độc lập. Server nhận mail không chứng minh người nhận nhận được.
- Timing enumeration của reset chưa đo; giới hạn rate limit chưa benchmark; HSTS ngắn, chưa preload.
- Pricing false: không có nội dung được index, nên hybrid/RAG chỉ kiểm thông báo/đường từ chối, không đánh giá
  chất lượng. UI không hiện form RAG cho item chưa index.
- M8b: journal độc lập, backup có lịch/retention/mã hóa, alert/recovery và nghiệm thu triển khai.
