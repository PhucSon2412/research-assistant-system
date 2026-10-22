# M9 — demo LAN trên máy của chủ dự án

Task: [M9](tasks/m9-lan-demo-deployment.md). Base `origin/main = 7009a8483e4f9891f11a966f114df136c90c4953` (merge PR #68),
nhánh `ops/m9-lan-demo-deployment`. Source migration **0052**. Không đổi code ứng dụng, không build image.

**Trạng thái (2026-10-21):** bản demo **đang chạy** trên máy này, project `ras-pilot`, image rc2. Agent đã kiểm: cài
mới, readiness, bind mạng, HTTPS từ chính máy qua IP LAN, mail alert thật qua Gmail, replica journal, backup, edge 503
trong cửa sổ backup, drill restore, hai scheduled task. **Phần chủ dự án phải làm** (agent không có quyền/thiết bị):
tạo rule firewall (cần Administrator), bootstrap Admin (nhập mật khẩu), nghiệm thu từ một thiết bị LAN khác, xác nhận mail
đã vào hộp thư, cất private key `age` ra khỏi máy. Xem [mục 4](#4-việc-của-chủ-dự-án-và-nghiệm-thu).

**Cập nhật M7c (2026-10-21, nhánh `feat/m7c-live-quality-verification`):** đã bật provider cho ba route bằng lệnh mới
`m9_lan_demo.py provider` (image rc2 và `deploy/` không đổi). Thư mục paper demo giấy phép mở và kịch bản search/RAG ở
[mục 3](#3-giới-hạn-và-rủi-ro-đã-biết) và [kịch bản demo](#kịch-bản-demo-510-phút). Sau một lần mất điện máy, demo tự chạy
lại: gate open, journal = DB, replica `ok`.

Đây là **bản demo LAN**, lưu trữ cùng máy theo quyết định chủ dự án; không phải cutover nhóm thử theo
[điều kiện M8c](m8c-release-candidate.md#8-điều-kiện-cutover).

## 1. Topology thực tế

| Mục | Giá trị |
|---|---|
| Truy cập | `https://ras-demo.lan:8443` (thiết bị cần dòng hosts + CA demo, mục 5) |
| Bind | Chỉ `192.168.69.97:8443` (IP Ethernet lúc chạy, `/22`, profile Public). Không publish `0.0.0.0`, không 80/443/8080 |
| Project Docker | `ras-pilot` (ứng dụng), `ras-pilot-offsite` (MinIO đích replica + backup), `ras-pilot-drill` (scratch, chỉ tồn tại khi drill chạy) |
| Thư mục | `%USERPROFILE%\ras-pilot\` — `release\` (bản sao `deploy/` + `scripts/ras_ops.py` của `7009a84` bằng `git archive`), `pilot\` (env, overlay, journal, ops-config, ops-state, staging, maintenance), `drill\`, `offsite\`, `secrets\`, `ca\`, `tls.d\`, `public\` (CA cert để cài lên thiết bị), `operator-handoff\` (private key `age`), `bin\`, `logs\`, `evidence\` |
| Image | `ras-pilot-api:rc2` = `sha256:c0936a81…0a06`, `ras-pilot-web:rc2` = `sha256:59cb5141…2cb3`, `ras-pilot-ops:rc2` = `sha256:22656406…66fc` — tag alias trỏ đúng ID của `rasm8c-*` (M8c), kiểm ID trước khi tạo |
| Hạ tầng | Caddy/PostgreSQL 16.14/Redis/MinIO theo digest pin trong `deploy/compose.yaml` (đều đã có trong cache máy) |
| Mạng nội bộ | backend `ras-pilot` `172.30.64.0/24`, drill `172.30.48.0/24`; `ras-pilot-offsite_offsite` internal, chỉ replicator/`ops`/`ops-check` nối vào |
| SMTP | `smtp.gmail.com:465` `implicit_tls`, CA hệ thống của image; tài khoản/sender/người nhận alert = địa chỉ trong `secrets\smtp_username`; mật khẩu chỉ là Compose secret `smtp_password` |
| Provider | **Từ M7c (2026-10-21):** key nhóm (từ `secrets\openrouter_api_key`) trong `pilot.env` (ACL chỉ user), `pricing_verified=true` cho embedding/rerank/generation sau khi có chứng cứ live ([handoff M7c](m7c-live-quality-verification.md)); routing ZDR pin như M7a; budget app $10/ngày, quota User mặc định; output generation 1024 token, trần request $0,06 (từ 2026-10-22, chỉ demo); discovery tắt. Trước đó: key trống, pricing false |
| Lịch | Task Scheduler: `RAS pilot backup` 03:00 hằng ngày, `RAS pilot check` mỗi 15 phút; chạy `pythonw.exe` của Conda `ras` → `bin\run_ops_task.py` → `release\scripts\ras_ops.py`; log `logs\ops-<ngày>.log` (giữ 30 ngày) |

**Overlay, không sửa deploy đã review.** `scripts/m9_lan_demo.py prepare` sinh `pilot\override.yaml` ngoài repo:

- edge publish **duy nhất** `${RAS_EDGE_BIND}:8443:8443` (`ports: !override`);
- site Caddy `ras-demo.lan:8443` (site address có cổng nên Caddy nghe 8443);
- API `RAS_HTTP__ALLOWED_ORIGINS=["https://ras-demo.lan:8443"]`, `PUBLIC_BASE_URL` có cổng để link mời/reset đúng;
- nối `journal-replicator`/`ops`/`ops-check` vào mạng offsite; nhãn `ras.m9.demo=ras-pilot` trên mọi service.

Lý do: compose gốc map cứng container `:443` và tạo origin `https://<host>` không cổng, nên với cổng 8443 trình duyệt
gửi `Origin: https://ras-demo.lan:8443` sẽ bị từ chối CSRF và redirect HTTP sẽ trỏ về 443 (Dify). Không publish cổng
redirect HTTP.

**Không hỗ trợ truy cập bằng IP trần** (`https://192.168.69.97:8443`): request không có SNI, Caddyfile đã review không có
`default_sni`, handshake thất bại (đã kiểm: `tlsv1 alert internal error`). Thiết bị phải dùng hostname.

**CA demo:** EC P-256, 3 năm, **name constraints** chỉ cho `ras-demo.lan` và dải IP private (10/8, 172.16/12,
192.168/16): lộ khóa CA cũng không giả mạo được site khác. Leaf 397 ngày, SAN `ras-demo.lan` + IP LAN, EKU serverAuth.
Khóa CA ở `ca\demo-ca.key`, thư mục chỉ cấp quyền cho user hiện tại (bỏ kế thừa). Không có trong repo.

## 2. Lệnh đã chạy và kết quả (agent)

Python host `C:\Users\ADMIN\.conda\envs\ras\python.exe`. Không đọc `.env`, không đọc key OpenRouter, không đụng `ras`/Dify/
Tailscale/Radmin, không đổi network category, không `taskkill`. Evidence ngoài repo: `%USERPROFILE%\ras-pilot\evidence\`.

| Bước | Lệnh | Kết quả |
|---|---|---|
| Inventory trước | `m9_lan_demo.py inventory` | 24 container, 188 volume, 9 network, 141 image; 0 rule/task `RAS pilot*`. 8080/8443 trống; 80/443 thuộc process khác (Dify) |
| Chuẩn bị | `m9_lan_demo.py prepare --lan-ip 192.168.69.97` | Kiểm IP thuộc `Ethernet`; 3 image đúng ID; release `7009a84`; CA + leaf; 6 secret sinh mới (không ghi đè, không in); cặp khóa `age`; env/overlay/`ops.json` |
| Đích cùng máy | `m9_lan_demo.py offsite-up` | MinIO digest pin, 2 bucket: journal (object lock GOVERNANCE 1 ngày), backup (versioning); 4 user ghi/chỉ đọc. Replicator không có quyền xóa. Lượt đầu lỗi do stdin CRLF trên Windows (không tạo user), sửa script gửi bytes, chạy lại ok |
| Kiểm tĩnh | `docker compose … config` + `m8a_deploy_check.py --env-file pilot.env --settings-image ras-pilot-api:rc2`; `ops.json` qua `OpsSettings` trong image ops | Chỉ edge publish, `192.168.69.97:8443`; key provider trống, pricing false; `deploy contract: ok`; `ops.json` ok |
| Cài mới | `docker @C up -d --wait` | migrate/provision/recovery-init exit 0; mọi service healthy |
| Readiness | `recovery status`, `/readyz`, `ops_probe` | `gate=open journal_seq=3 db_seq=3 registered=new_install`; 200; `status ok` |
| Mạng | `m9_lan_demo.py verify` | Chỉ `192.168.69.97` nghe 8443; Tailscale (`100.x`), Radmin (`26.x`), loopback, WSL: **đóng**; HTTPS `ras-demo.lan` (SNI) qua IP LAN với CA demo → `200 OK`; IP trần lỗi như trên |
| Alert thật | `ras_ops check` (lần 1, lần 2 sau drill) | Lần 1: `backup_stale` + `restore_drill_overdue`, `mailed 1`, không lỗi; lần 2: `resolved` cả hai, `mailed 1`. Gmail chấp nhận gửi; **chủ dự án xác nhận đã nhận** |
| Replica | `journal-replicator … journal_replica verify` | `relation equal`, seq 3, `verify ok` |
| Backup | `ras_ops backup` + poll edge 2 lần/giây | `status ok`, cửa sổ bảo trì **18,6 s** (cả lượt 25,9 s), `reopen ok`, ciphertext `age` đọc lại khớp hash. Poll: 37 mẫu **503** liên tục 18,0 s (`/api/*` → `{"error":"maintenance"}`, trang → HTML "Bảo trì", `Retry-After: 300`), 30 mẫu 200, **0** mẫu 502/lỗi |
| Drill | `ras_ops drill --env-file drill\drill.env --project ras-pilot-drill … --record-state-dir pilot\ops-state` | `drill ok`, restore + recover **39,3 s**, schema `0052`, journal từ replica, gate open; teardown: 0 container, 0 volume, thư mục drill rỗng |
| Lịch | `m9_lan_demo.py schedule install`; `Start-ScheduledTask` cả hai | `check` exit 0; `backup` exit 0 (24,8 s, bản thứ hai). Không cửa sổ console |

Số đo trên là máy này, dữ liệu gần rỗng, đích cùng máy; **không phải SLO/RPO/RTO**.

## 3. Giới hạn và rủi ro đã biết

- **Cùng miền sự cố:** DB, object store, journal, replica và backup đều trên một ổ đĩa/máy. Hỏng máy/ổ là mất tất cả,
  kể cả các thao tác thu hồi/xóa sau lần backup cuối; chủ dự án đã chấp nhận. Restore chỉ có ý nghĩa khi còn máy hoặc
  mang volume `ras-pilot-offsite_offsite-data` + private key `age` sang máy khác.
- **Private key `age`** ở `operator-handoff\age-identity.txt`. Mất key = **không restore được backup nào**. Chủ dự án
  chép sang USB/password manager, rồi xóa bản trên máy (drill tháng sau cần mang key về tạm thời).
- **Firewall:** Docker Desktop đã có sẵn rule `Docker Desktop Backend` cho phép `com.docker.backend.exe` mọi cổng TCP/UDP,
  mọi địa chỉ, trên Private + Public (có từ trước task, không sửa). Vì vậy rule `RAS pilot` là rule tường minh theo task
  nhưng **không phải lớp chặn hiện hành**; lớp giới hạn thực là bind đúng IP Ethernet (router NAT, không port-forward).
  Muốn chặn chặt hơn thì chỉnh rule Docker — ngoài phạm vi M9, chủ dự án tự quyết.
- **Docker Desktop chỉ chạy khi có người đăng nhập** và hiện `AutoStart=False`. Không đăng nhập/không mở Docker Desktop thì
  demo, backup và check đều không chạy, và **không có alert nào** (không có probe ngoài máy).
- **Sleep:** cắm điện không sleep; chạy pin sleep sau 10 phút. Windows Update có thể khởi động lại máy ngoài giờ.
  Task backup có `StartWhenAvailable`: lỡ 03:00 thì chạy khi máy/phiên sẵn sàng.
- **DHCP:** IP đổi thì edge không bind được; xem mục 6 ("Khi IP LAN thay đổi"). Nên đặt DHCP reservation trên router.
- **Mail:** gửi từ Gmail cá nhân, chưa kiểm SPF/DKIM cho hộp nhận khác ngoài Gmail; lời mời có thể vào Spam. Giới hạn gửi
  của Gmail đủ cho demo.
- **Durability:** journal ghi qua bind mount của Docker Desktop (Windows → VM); không tuyên bố an toàn mất điện.
- `scripts/m9_lan_demo.py uninstall` **chưa chạy** (sẽ phá bản demo đang dùng); đã có lệnh thủ công tương đương ở mục 8.

**Provider đã bật (M7c, 2026-10-21; giới hạn output cập nhật 2026-10-22).** `python scripts/m9_lan_demo.py provider --routes
embedding rerank generation --max-output-tokens 1024 --apply` (tắt: `provider --off --apply`). Lúc bật demo không có item nào; ngày
22/10 demo đã có 12 paper do chủ dự án upload (10 `indexed`, 1 `needs_review`, 1 lỗi parser, đúng như stack thử). Agent đã kiểm readiness, cấu hình, egress OpenRouter và bind
mạng. **Chưa kiểm** search/RAG qua UI trên demo: việc này cần chủ dự án upload paper và có tài khoản thử. Giới hạn:

- **Chỉ upload paper giấy phép mở hoặc tài liệu tự viết.** Text sẽ đi qua OpenRouter, mà key thuộc tài khoản nhóm và chưa
  xác minh được cài đặt logging. Thông báo upload trong image rc2 chỉ nói dữ liệu đi qua OpenRouter, không cảnh báo tài
  liệu nhạy cảm, nên người trình bày phải nói rõ điều này.
- Khoảng 40 % paper arXiv hai cột được parser xếp `needs_review` và không index (không OCR); demo nên dùng bộ đã kiểm.
- **Câu so sánh cần title thật và chế độ hybrid**: sau upload, sửa tiêu đề ở trang item (tên file `arxiv-…` không khớp tên
  paper trong câu hỏi), và hỏi so sánh ở chế độ **hybrid**. Keyword mode thường không gom đủ hai paper (M7c, keyword: 1/6).
  Hybrid cho câu so sánh chưa được đo trong M7c (hết trần), nên hỏi thử trước và chỉ trình diễn câu đã chạy được.
- **Output generation của demo là 1024 token** (`max_request_usd` 0,06): trước đây 512 token cắt câu so sánh dài (lỗi
  503). Mỗi câu thường tốn $0,005–0,02; quota User $1/ngày, ngân sách ứng dụng $10/ngày.

## 4. Việc của chủ dự án và nghiệm thu

Trong PowerShell (không cần Administrator, trừ bước 1), đặt sẵn:

```powershell
$R  = "$env:USERPROFILE\ras-pilot"
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
$C  = @('compose','--env-file',"$R\pilot\pilot.env",'-p','ras-pilot','-f',"$R\release\deploy\compose.yaml",'-f',"$R\pilot\override.yaml")
$A  = @('--env-file',"$R\pilot\pilot.env",'--project','ras-pilot','--compose-file',"$R\release\deploy\compose.yaml",'--compose-file',"$R\pilot\override.yaml")
```

1. **Firewall (PowerShell Administrator):**
   `New-NetFirewallRule -DisplayName 'RAS pilot LAN demo HTTPS' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8443 -LocalAddress <IP_LAN> -RemoteAddress LocalSubnet -InterfaceAlias Ethernet -Profile Public`
   (in đúng IP hiện tại bằng `python scripts/m9_lan_demo.py firewall-commands`).
2. **Bootstrap Admin** (một lần, tự gõ mật khẩu, không echo): `docker @C exec api python -m app.cli.bootstrap_admin --email <gmail>`.
3. **Cất key `age`:** chép `$R\operator-handoff\age-identity.txt` ra USB/password manager, kiểm bản chép đọc được, rồi xóa
   file trên máy.
4. Kiểm hộp thư `<gmail>`: hai mail `[RAS demo]` (alert `backup_stale`/`restore_drill_overdue` và `RESOLVED`).
5. **Nghiệm thu từ thiết bị LAN khác** (đã cài CA + hosts, mục 5), đánh dấu từng dòng:

| Hạng mục | Cách làm |
|---|---|
| Login/logout | Admin vừa bootstrap |
| Mời qua Gmail → accept → login | Admin mời `<gmail-local>+demo@gmail.com` (cùng hộp thư, khác tài khoản); mở link trong mail trên thiết bị đã cài CA |
| Reset mật khẩu | "Quên mật khẩu" với tài khoản demo; link qua Gmail |
| Upload có xác nhận, thư viện, collection, tải original | Tài khoản demo upload một PDF có text; xác nhận thông báo xử lý; thêm vào collection; tải original |
| Review → approve → publish → revoke | Demo gửi yêu cầu review; Admin mở file review, approve, publish (bản chung hiện cho tài khoản khác), revoke (bị chặn với request mới) |
| Search/RAG có citation (M7c) | Tài khoản demo upload 3–4 PDF trong `%USERPROFILE%\ras-pilot\demo-papers\` (đọc `ATTRIBUTION.md`), chờ `indexed`, sửa tiêu đề; tìm hybrid và hỏi các câu ở kịch bản dưới; bấm citation mở đúng trang |
| Edge 503 khi backup | `Start-ScheduledTask -TaskName 'RAS pilot backup'` rồi tải lại trang trong ~20 s: trang "Bảo trì" |

## 5. Cài CA và hosts trên thiết bị demo

File CA: `%USERPROFILE%\ras-pilot\public\ras-demo-ca.crt` (chỉ certificate, không có khóa).

- **Windows** (Chrome/Edge): `certutil -user -addstore Root ras-demo-ca.crt` (user hiện tại, không cần admin; chấp nhận hộp
  thoại). Hosts: mở Notepad **Administrator**, thêm `<IP_LAN> ras-demo.lan` vào `C:\Windows\System32\drivers\etc\hosts`.
  Firefox: bật `security.enterprise_roots.enabled` hoặc import CA trong Settings → Certificates. Máy chủ cũng cần dòng
  hosts nếu muốn mở bằng trình duyệt trên chính máy này.
- **Android:** Settings → Security → Encryption & credentials → Install a certificate → CA certificate (Chrome tin CA
  người dùng). **iOS:** gửi file qua AirDrop/mail → Settings → General → VPN & Device Management → cài profile → Settings →
  General → About → Certificate Trust Settings → bật tin cậy đầy đủ.
- Điện thoại **không sửa được hosts**: cần router có DNS nội bộ (thêm bản ghi `ras-demo.lan → <IP_LAN>`). Không có thì
  demo bằng laptop/PC Windows.
- Gỡ sau demo: `certutil -user -delstore Root "RAS LAN demo CA"`, xóa dòng hosts; Android/iOS xóa CA/profile trong cùng
  màn hình.

### Kịch bản demo 5–10 phút

Chuẩn bị trước (khoảng 15 phút, trước buổi demo):

- Admin và tài khoản demo đã tạo.
- Tài khoản demo upload các PDF P10 (SCAR), P06 (SciRet), P07 (RAGdb), P09 (title-chain) và P01 (Experience-RAG) từ
  `%USERPROFILE%\ras-pilot\demo-papers\`. Chờ trạng thái `indexed` (khoảng 5 phút/paper), rồi sửa tiêu đề từng item
  thành tiêu đề paper.
- Upload thêm P11 để minh họa trạng thái "cần review" (không index, không OCR).
- Hỏi thử trước từng câu bên dưới; câu nào không ra kết quả thì bỏ khỏi buổi demo.

1. (1 phút) Kiến trúc: HTTPS trong LAN, phiên `__Host-`, quyền lọc trong SQL, journal thu hồi, backup mã hóa; provider qua
   OpenRouter chỉ endpoint ZDR, ngân sách $10/ngày. **Nói rõ: chỉ dùng paper giấy phép mở, không upload tài liệu riêng.**
2. (2 phút) Upload một paper có hộp xác nhận xử lý → trạng thái `indexed`; P11 dừng ở "cần review" (parser không chắc ranh
   giới body, không đoán) → thư viện, collection, tải original.
3. (3 phút) Tìm kiếm và hỏi đáp có citation (các câu đã trả lời đúng trong M7c):
   - tìm hybrid: `reranking for scientific retrieval` → hit có trang, mở citation;
   - trên item SCAR: *What recall does SCAR achieve on boundary-fragmented queries, and with how many chunks?*;
   - trên item SciRet: *What effect did the MS MARCO-trained cross-encoder reranker have in SciRet?*;
   - trên item RAGdb: *How much does RAGdb reduce disk footprint compared with Docker-based RAG stacks?*;
   - toàn thư viện: *Do all retrieval benchmarks report perfect recall?* (trả lời "không", trích SCAR và Experience-RAG);
   - toàn thư viện, so sánh, **chế độ hybrid**: *Compare how SCAR and the title-chain chunking paper address evidence split
     across chunk boundaries.* (cần đã sửa tiêu đề);
   - không có đáp án, trên item RAGdb: *What monthly subscription price does RAGdb charge?* → hệ thống báo thiếu bằng chứng;
   - bấm một citation → mở đúng paper/trang, quote nằm trong đoạn.
4. (2 phút) Gửi review → Admin xem đúng file được yêu cầu → approve → publish → đăng nhập tài khoản khác thấy bản chung →
   revoke → request mới bị chặn.
5. (1 phút) Mời người dùng: mail thật tới Gmail, accept; reset mật khẩu.
6. (1–2 phút) Vận hành: `& $py "$R\release\scripts\ras_ops.py" status @A`; chạy task backup → trang bảo trì 503 → tự mở
   lại; mail alert.

## 6. Bật/tắt, sau khởi động lại, khi IP đổi

- **Bật:** mở Docker Desktop (nên bật Settings → General → *Start Docker Desktop when you sign in*), rồi
  `docker compose --env-file "$R\offsite\offsite.env" -p ras-pilot-offsite -f "$R\offsite\compose.yaml" up -d --wait`,
  `docker @C up -d --wait`, `Enable-ScheduledTask -TaskName 'RAS pilot backup'; Enable-ScheduledTask -TaskName 'RAS pilot check'`.
- **Tắt (giữ dữ liệu):** `Disable-ScheduledTask` hai task (tránh mail `api_unavailable`), `docker @C stop`,
  `docker compose … -p ras-pilot-offsite … stop`.
- **Sau khởi động lại máy:** đăng nhập, mở Docker Desktop; container có `restart: unless-stopped` tự chạy lại. Kiểm
  `docker @C ps` và `python scripts/m9_lan_demo.py verify` (từ repo). Edge không lên (IP chưa sẵn sàng/đã đổi) thì
  `docker @C up -d --wait`. `& $py "$R\release\scripts\ras_ops.py" status @A` phải có `maintenance: null`, replica `ok`.
- **Khi IP LAN thay đổi:** `python scripts/m9_lan_demo.py rebind --lan-ip <IP mới>` (kiểm IP thuộc Ethernet, cấp leaf mới
  từ CA demo — CA không đổi nên thiết bị không phải cài lại, cập nhật env) → `docker @C up -d --wait` → sửa dòng hosts
  trên thiết bị → xóa rule firewall cũ, tạo lại bằng `firewall-commands`.
- **Diễn tập tháng** (alert `restore_drill_overdue` sau 31 ngày): mang key `age` về tạm thời, rồi
  `& $py "$R\release\scripts\ras_ops.py" drill --env-file "$R\drill\drill.env" --project ras-pilot-drill --compose-file "$R\release\deploy\compose.yaml" --compose-file "$R\drill\override.yaml" --identity-file <key> --record-state-dir "$R\pilot\ops-state"`.
- Alert khác: theo [runbook M8b](m8b-pilot-operations.md#alert-runbook).

## 7. Inventory sau

Sau khi dựng (giữ có chủ đích): thêm container của `ras-pilot` (9 chạy + 3 one-shot đã exit) và `ras-pilot-offsite` (1),
volume `ras-pilot_{db-data,redis-data,minio-data,caddy-data,caddy-config}` + `ras-pilot-offsite_offsite-data`, network
`ras-pilot_{public,edge,backend,egress}` + `ras-pilot-offsite_offsite`, ba tag alias `ras-pilot-*:rc2` (không thêm image
ID mới). Project drill đã hạ sạch. Hai task `RAS pilot backup`/`RAS pilot check`. Rule firewall: chưa có (chờ chủ dự án).
Tổng: trước 24 container/188 volume/9 network/141 image → sau **37/194/14/141** (+13/+6/+5/+0), không mục nào của
baseline bị xóa; 24 container có sẵn giữ nguyên ID và trạng thái. Không tạo/dọn tài nguyên nào khác. Chi tiết ở
`%USERPROFILE%\ras-pilot\evidence\inventory-{before,after}.json`.

## 8. Gỡ sạch

Gỡ là mất toàn bộ dữ liệu demo **và mọi backup** (backup nằm trong volume offsite cùng máy).

1. `python scripts/m9_lan_demo.py uninstall --yes`: gỡ hai task, `down -v` ba project sau khi kiểm từng container có nhãn
   `ras.m9.demo` + đúng project, gỡ ba tag alias (image rc2 gốc `rasm8c-*` giữ nguyên). Thủ công tương đương:
   `Unregister-ScheduledTask -TaskName 'RAS pilot backup' -Confirm:$false` (và `'RAS pilot check'`);
   `docker @C --profile ops down -v --remove-orphans`;
   `docker compose --env-file "$R\drill\drill.env" -p ras-pilot-drill -f "$R\release\deploy\compose.yaml" -f "$R\drill\override.yaml" --profile ops down -v`;
   `docker compose --env-file "$R\offsite\offsite.env" -p ras-pilot-offsite -f "$R\offsite\compose.yaml" down -v`;
   `docker image rm ras-pilot-api:rc2 ras-pilot-web:rc2 ras-pilot-ops:rc2`.
2. PowerShell Administrator: `Remove-NetFirewallRule -DisplayName 'RAS pilot LAN demo HTTPS'`.
3. Thiết bị demo: gỡ CA và dòng hosts (mục 5).
4. Xóa `%USERPROFILE%\ras-pilot` (gồm secret do chủ dự án tạo; giữ lại trước nếu cần).

Kiểm sau gỡ: `docker ps -a --filter label=ras.m9.demo` rỗng, `docker volume ls --filter label=com.docker.compose.project=ras-pilot`
rỗng, `Get-ScheduledTask -TaskName 'RAS pilot*'` và `Get-NetFirewallRule -DisplayName 'RAS pilot*'` không còn.

## 9. File trong PR

- [`scripts/m9_lan_demo.py`](../scripts/m9_lan_demo.py): chuẩn bị/đích cùng máy/kiểm mạng/đổi IP/lịch/lệnh firewall/gỡ.
  Không in secret; mật khẩu MinIO root qua biến môi trường của `docker exec`, mật khẩu user MinIO qua stdin.
- [`deploy/lan-demo/run_ops_task.py`](../deploy/lan-demo/run_ops_task.py): entry Task Scheduler, chạy `ras_ops.py` ẩn,
  log theo ngày.
- Tài liệu: handoff này, kế hoạch MVP, mục lục, AGENTS/CLAUDE, decisions; kèm docs orchestrator chưa commit trước đó
  (review PR #68, task M7c/M9).

Không có trong repo: env file, secret, địa chỉ Gmail, khóa CA/leaf/`age`, IP Tailscale/Radmin.
