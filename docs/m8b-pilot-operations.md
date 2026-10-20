# M8b — vận hành nhóm thử: journal replica ngoài máy, backup có lịch/mã hóa, alert, diễn tập restore

Task: [M8b](tasks/m8b-pilot-operations.md). Base `origin/main = 0de10f4c7d119bd0190a673a52cc99b0e98ca99f` (merge
PR #66, tree `119d604`), nhánh `feat/m8b-pilot-operations`. Source migration giữ **0052**: không migration, không đổi
đường ghi journal, gate, readiness, schema hay quyền DB. Kết quả kiểm ở [inventory M8b](m8b-test-inventory.json).

**Trạng thái:** đã triển khai và kiểm trên project Docker cô lập với hai MinIO thử đóng vai hai đích S3 ngoài máy,
SMTP capture TLS và khóa `age` thử. **Chưa có** host, đích S3 ngoài miền sự cố, cặp khóa `age`/người giữ private
key, người nhận alert, SMTP relay, domain hay probe ngoài host thật; **chưa cutover, chưa chạy systemd thật, không
phải RPO/RTO đã đạt**. Hai MinIO chạy cùng máy: chỉ mô phỏng, không chứng minh độc lập miền sự cố. Không đọc
`.env`/key, không đụng dev `ras`/Dify, không gọi provider/arXiv, không gửi mail thật, `pricing_verified` giữ false,
không điền phiếu human M7b. Các lựa chọn dưới là **orchestrator chọn theo ủy quyền** (task M8b), không phải chủ dự án
đã chọn nhà cung cấp/host/người trực.

## Topology M8b

Thêm vào [`deploy/compose.yaml`](../deploy/compose.yaml) (giữ nguyên mọi service/mạng M8a):

| Thành phần | Image / chạy | Mạng | Mount | Credential/secret |
|---|---|---|---|---|
| `journal-replicator` | API image, service liên tục, UID 10001, rootfs read-only, `cap_drop: ALL` | `egress` | journal **read-only**, ops config ro, ops state rw | chỉ `journal_replica_credentials` (ghi đích replica). Không DB/MinIO/backup/SMTP |
| `ops` (profile `ops`) | ops image = API image + `pyrage==1.4.0` pin hash; one-shot do `ras_ops.py` gọi | `backend`, `egress` | journal rw (chỉ restore ghi journal mới), staging rw, state rw, config ro | mật khẩu migrator + MinIO root (dump/objects), `backup_target_credentials` (ghi), `journal_replica_read_credentials` |
| `ops-check` (profile `ops`) | ops image, one-shot theo timer | `egress` | journal ro, staging ro, state rw, config ro, trust ro | `journal_replica_read_credentials`, `backup_target_read_credentials`, `smtp_password`. Không DB/MinIO |
| `edge` | như M8a + mount `RAS_MAINTENANCE_DIR` ro | như M8a | cờ `enabled` | như M8a |

- Dữ kiện DB cho alert lấy bằng `docker compose exec api python -m app.cli.ops_probe` (LOGIN runtime, chỉ đọc, chỉ
  gate reason + số đếm quá hạn). Không service chạy lâu nào có mật khẩu migrator.
- Cấu hình: một file JSON tường minh `RAS_OPS_CONFIG_DIR/ops.json` ([mẫu](../deploy/ops.example.json), model
  `app.ops.config.OpsSettings`) + recipient `age` public key. Credential đích S3 là file JSON
  `{"access_key_id","secret_access_key"}` trong `RAS_SECRETS_DIR` (Compose secrets), không argv/env/log. Hai đích phải
  khác bucket và khác credential (config từ chối nếu trùng); mỗi đích có một credential ghi và một credential chỉ đọc
  (có thể trùng nếu nhà cung cấp không có read-only, khi đó mất lớp phân quyền này).
- Biến env mới: `RAS_OPS_IMAGE`, `RAS_OPS_CONFIG_DIR`, `RAS_OPS_STATE_DIR`, `RAS_OPS_STAGING_DIR` (0700, UID 10001),
  `RAS_MAINTENANCE_DIR` (xem [pilot.env.example](../deploy/pilot.env.example)).
- Entrypoint host [`scripts/ras_ops.py`](../scripts/ras_ops.py) chỉ dùng thư viện chuẩn Python (host Linux cần
  `python3` + Docker Compose, không cần cài app). Bắt buộc `--env-file`, `--project`, `--compose-file`; từ chối project
  `ras`, compose dev, file tên `.env`, project lệch `RAS_DEPLOY_PROJECT`; chỉ đọc 6 tên biến không bí mật từ env file;
  gọi Compose với environment sạch (bỏ `RAS_*`, `POSTGRES_*`, `MINIO_*`, `REDIS_*`, `COMPOSE_*` của shell gọi).
- Lịch: [`deploy/systemd/`](../deploy/systemd/) có `ras-ops-backup.service/.timer` (hằng ngày, giờ mẫu 02:30
  Asia/Ho_Chi_Minh) và `ras-ops-check.service/.timer` (5 phút). Đã `systemd-analyze verify` trong container Debian
  (systemd 257); **chưa chạy dưới systemd thật**.

## Journal: replica ngoài máy (đường ghi giữ nguyên)

`app/ops/replica.py` + `python -m app.cli.journal_replica ship` (vòng lặp, mặc định 15 s):

- Đọc file journal (mount ro) bằng `app.journal.read`, chỉ lấy record hoàn chỉnh; **torn tail không bao giờ ship**.
  Không lock DB/journal, không chạm request/gate/readiness: đích chậm/lỗi chỉ làm tăng lag.
- Đích: `<prefix>/journal/install.json` (journal_id + genesis, tạo một lần), segment bất biến
  `<prefix>/journal/<journal_id>/segments/<first-seq:012d>.jsonl` tạo bằng `If-None-Match: *` (không ghi đè), đọc lại
  SHA-256 sau ghi, rồi `head.json` (seq/hash/thời điểm; con trỏ có versioning). Đích phải bật versioning
  (`require_versioning`); object lock dùng nếu nhà cung cấp hỗ trợ (đích thử journal: governance 1 ngày; credential
  replicator không có quyền xóa).
- Dừng ship, không ghi gì, ghi trạng thái lỗi khi: journal_id/genesis khác (`replica_journal_mismatch`), record khác
  tại cùng seq (`replica_fork`), local ngắn hơn phần đã ship (`journal_behind_replica` — local/host bị quay về), chuỗi
  đích không kiểm được (`replica_corrupt`/`replica_incomplete`). Không tự sửa journal, không prune replica.
- Trạng thái `ops-state/replica-status.json`: `code`, `local_seq`, `remote_seq`, `lag_records`, `unshipped_since`.
- **Giới hạn còn lại:** mất host thì mất các record sau lần ship thành công cuối (đo được ở diễn tập b). Đây là cửa sổ
  rủi ro cho revoke/xóa/auth closure, được đo và alert (`replica_lag`, `replica_stale`), **không phải zero-RPO**.
  Journal vẫn không bao giờ nằm trong artifact backup.

**Chọn journal khi restore** (`replica.choose`): bản local (nếu còn) và replica; dùng chuỗi dài hơn **chỉ khi** chuỗi
kia là prefix của nó; khác journal → `journal_sources_mismatch`; phân kỳ → `journal_sources_diverged` (operator quyết
định, không ghép). Sau đó vẫn chạy `check_journal_for_backup` với manifest: replica/local cũ hơn backup →
`journal_behind_backup`. Ca **DB + journal local cùng quay về nhưng replica mới hơn** (trước M8b không phát hiện được)
nay lộ ra ở ba chỗ: restore chọn replica (`journal_relation=replica_ahead`) và replay; replicator báo
`journal_behind_replica`/`replica_fork` và không ghi; `check` alert `journal_rollback` và **dựng cờ bảo trì ở edge**
(503) để không mở traffic; `ras_ops.py open` từ chối khi journal lệch replica.

## Backup theo lịch trên topology deploy

`ras_ops.py backup` (timer gọi đúng argv này):

1. Khóa `ops-state/maintenance.lock` (O_EXCL, không tự phá; quá hạn → alert `maintenance_stuck`). Preflight trong
   `ops`: recipients chỉ public key (file có `AGE-SECRET-KEY` → `private_key_on_server`), đích backup sẵn sàng +
   versioning, staging ghi được. Preflight lỗi → không mở cửa sổ, run `failed`, alert.
2. Cờ bảo trì → edge trả **503** `{"error":"maintenance"}` (/api) hoặc trang HTML, `Retry-After: 300`; upstream chết
   ngoài cửa sổ → 503 `service_unavailable` (không còn 502 mơ hồ).
3. `compose stop web api worker beat`, kiểm không còn writer. `ops backup-capture` mở snapshot REPEATABLE READ (migrator)
   và in snapshot id; `pg_dump --snapshot` chạy **trong container db** (không cổng DB ra host); host chuyển dump vào ops
   qua stdin theo frame có kích thước + trailer (exit, bytes, SHA-256 phía host) — dump cụt/sai bị từ chối. Ops copy
   object qua mạng `backend`, kiểm từng file row/SHA-256, ghi manifest, verify (`app.backup`).
4. **Luôn** `compose start` writer + chờ readiness thật; chỉ xóa cờ khi ready. Mở lại thất bại → cờ giữ nguyên, run
   ghi `reopen=failed`, alert ngay (`maintenance_reopen_failed`); không tự đặt/xóa recovery hold.
5. Ngoài cửa sổ: `ops backup-ship` đóng tar → mã hóa `age` (pyrage/rage) → **xóa plaintext ngay** → upload không ghi
   đè → đọc lại SHA-256 từ đích → mới ghi metadata `verified` → xóa ciphertext local. Lỗi bất kỳ: không metadata
   (không phải backup tốt), staging rỗng.
   Upload là **một** `put_object`: S3 giới hạn 5 GB mỗi PUT; dữ liệu pilot nhỏ nên chưa ảnh hưởng, artifact lớn hơn cần
   multipart (chưa làm, giới hạn đã biết).
6. Retention `ops backup-prune`: chỉ khi lượt này thành công; luôn giữ `min_keep` bản tốt mới nhất; bản tốt chỉ bị xóa
   khi có bản tốt mới hơn; metadata xóa trước ciphertext. Versioning đích giữ bản noncurrent tới lifecycle của bucket
   (cấu hình ở nhà cung cấp, chưa có).
7. Ghi `ops-state/backup-state.json` (`last_run`, `last_success`, `window_seconds`, `reopen`); lỗi → chạy `check` ngay.

Timeout từng bước nằm trong `ops.json` `window`. Giá trị mặc định (retention 30 ngày theo
`retention.backup_retention`, `min_keep` 7, lag 900 s, backup cũ 26 h, drill 31 ngày, đĩa 1/5 GiB) là **cấu hình vận
hành khởi đầu**, không phải SLO.

## Alert tối thiểu

`ras_ops.py check` (timer 5 phút): probe runtime trong api + `ops-check`. Mail qua **adapter SMTP TLS hiện có**
(`SmtpDelivery.send_email`, cùng process con/deadline M8a) tới **từng** người nhận trong `alerts.recipients`, độc lập
nhau (sửa review PR #67): lỗi ở một người không chặn người khác. Dedupe theo từng người nhận trong
`ops-state/alerts-state.json` (khóa là hash địa chỉ, không lưu email): mail khi mới, nhắc lại sau `renotify_hours`, mail
`RESOLVED` chỉ tới người đã được báo. Nội dung chỉ mã, số đếm, thời điểm: mọi giá trị qua `alerts.safe` (số/bool/ISO
time/mã `[a-z0-9_:.-]`), còn lại thành `?`. Có người nhận gửi lỗi → exit 5, summary nêu `mailed`/`failed`/
`delivery_errors` (mã); người lỗi giữ state cũ nên được gửi lại lượt sau, người đã nhận không bị gửi lặp. **Cần thêm probe/dead-man ngoài host ở cutover**: host chết thì không
tự báo được; M8b không đăng ký dịch vụ ngoài.

### Alert runbook

- <a id="alert-gate-closed"></a>**`gate_closed:<reason>`** — readiness đóng. `journal_torn`/`journal_missing`/
  `journal_unconfirmed`/`journal_mismatch`/`db_behind_journal`: dừng writer, giữ bảo trì, chạy
  `recovery-init python -m app.cli.recovery status`, theo [runbook C4](c4-integrated-runtime-cutover.md); không sửa tay
  journal/DB. `recovery_in_progress`, hoặc `db_behind_journal` ngay sau `recovery hold` (bản ghi hold chỉ nằm trong file tới khi `recover` mirror): một hold đang mở — hoàn tất `recover` theo kế hoạch bảo trì. `db_behind_journal` ngoài bảo trì là DB bị quay về: xử lý như restore.
- <a id="alert-api-unavailable"></a>**`api_unavailable`** — api không chạy/không trả lời ngoài cửa sổ: xem
  `compose ps/logs api`, khởi động lại; nếu do DB/journal xem `gate_closed`.
- <a id="alert-replica-error"></a>**`replica_error:<code>`** — `target_unavailable`/`target_denied`: kiểm đích S3,
  credential, versioning; replicator tự bắt kịp khi đích trở lại. `replica_corrupt`/`replica_incomplete`: đích bị sửa
  hoặc thiếu segment — **không** ghi đè; so version trên đích, khôi phục version gốc (object lock/versioning), báo người
  chịu trách nhiệm recovery.
- <a id="alert-journal-rollback"></a>**`journal_rollback:<reason>`** — local journal sau hoặc khác replica. Edge đã
  dựng cờ bảo trì. Dừng writer; **không** xóa cờ; xác định host/DB có bị quay về (snapshot VM, restore tay). Nếu replica
  là chuỗi đúng: lấy bản sao journal hiện tại ra ngoài, restore theo runbook "DB rollback" dưới đây (công cụ chọn replica
  khi local là prefix). Phân kỳ thật: operator quyết định, không ghép chuỗi. **Đánh đổi:** restore từ backup làm mất mọi
  thay đổi DB sau backup đó. Ca DB không bị quay về mà chỉ journal local thiếu đuôi (vd. replicator đã ship một dòng chưa
  kịp `fsync` rồi máy mất điện) không có công cụ nạp replica làm journal local tại chỗ trong M8b; operator cân nhắc giữa
  restore (mất thay đổi DB sau backup) và điều tra thủ công, không sửa tay journal/DB.
- <a id="alert-replica-lag"></a>**`replica_lag`** — record chưa ship quá ngưỡng: xem `replica_error`; đây là cửa sổ mất
  dữ liệu nếu host mất lúc này.
- <a id="alert-replica-stale"></a>**`replica_stale`** — `replica-status.json` không cập nhật: `compose ps
  journal-replicator`, logs, khởi động lại.
- <a id="alert-backup-failed"></a>**`backup_failed:<code>`** — mã bước (`preflight_*`, `capture_*`, `ship_*`,
  `pg_dump_failed`, …). Sửa nguyên nhân rồi chạy lại `ras_ops.py backup` ngoài giờ cao điểm.
- <a id="alert-backup-stale"></a>**`backup_stale`** — không có backup tốt mới hơn ngưỡng: kiểm timer, đích, log lượt cuối.
- <a id="alert-maintenance-reopen-failed"></a>**`maintenance_reopen_failed`** — writer không ready lại: cờ bảo trì vẫn
  bật. Xem readiness/gate, sửa (DB/journal), rồi `ras_ops.py open`.
- <a id="alert-maintenance-stuck"></a>**`maintenance_stuck`** — `maintenance.lock` giữ quá timeout: kiểm process
  `ras_ops.py` còn chạy không (đúng PID trong lock); nếu đã chết, `ras_ops.py status`, kiểm writer/cờ rồi mới xóa lock.
- <a id="alert-cleanup-overdue"></a>**`cleanup_overdue:<which>`** — tombstone/xóa private quá `purge_due_at` chưa dọn:
  kiểm worker/beat, gate, MinIO; cleanup tự chạy lại theo `cleanup_poll_seconds`.
- <a id="alert-restore-drill-overdue"></a>**`restore_drill_overdue`** — chưa diễn tập trong 31 ngày: chạy diễn tập tháng.
- <a id="alert-disk-low"></a>**`disk_low:<which>`** — dọn/mở rộng volume journal hoặc staging; không xóa journal.
- <a id="alert-check-incomplete"></a>**`check_incomplete:<which>`** — một dữ kiện không đọc được (credential/đích/
  state file): sửa để check đủ dữ kiện.

## Runbook

Mọi lệnh: `python3 scripts/ras_ops.py <lệnh> --env-file <pilot.env> --project <P> --compose-file deploy/compose.yaml`
(viết tắt `ras_ops <lệnh>` dưới đây); Compose trực tiếp luôn kèm `--env-file`, `-p`, `-f deploy/compose.yaml`.

**Chuẩn bị (ngoài repo):** như M8a + tạo `RAS_OPS_CONFIG_DIR` (ops.json từ mẫu, `age-recipients.txt` chỉ public key),
`RAS_OPS_STATE_DIR`, `RAS_OPS_STAGING_DIR` (0700), `RAS_MAINTENANCE_DIR`, thư mục journal; chủ UID 10001. Bốn file
credential trong `RAS_SECRETS_DIR`. Đích journal: bucket riêng, versioning (+object lock nếu có), credential ghi không có
quyền xóa. Đích backup: bucket/credential khác, versioning, lifecycle noncurrent theo chính sách. Cặp khóa `age` tạo trên
máy operator (`age-keygen`), chỉ public key lên server. Build: `… build migrate web`, `… --profile ops build ops`; ghi
digest. `python scripts/m8a_deploy_check.py --env-file <env> --settings-image <api image>`.

**Cài mới:** `… up -d --wait` (thêm `journal-replicator`) → readiness → bootstrap Admin (M8a) → `ras_ops check` → cài
timer systemd (file 0644, user có quyền Docker) → lượt backup đầu → diễn tập restore đầu.

**Nâng cấp từ M8a có dữ liệu:** thêm biến/secret/thư mục M8b vào env → dừng `edge web api worker beat` →
`… run --rm --no-deps recovery-init python -m app.cli.recovery hold` bằng **image đang phục vụ** → đổi image/compose
M8b → `run --rm --no-deps migrate` → `run --rm --no-deps provision` → `… recovery recover` (C3 `--hold-for-operator` nếu
cần) → `ras_ops open` (so replica: rỗng là bình thường, rồi `up -d --wait`, readiness, xóa cờ) → replicator ship toàn bộ
journal hiện có → `ras_ops backup`.

**Kiểm replica:** `ras_ops status` (replica-status), `… run --rm --no-deps journal-replicator python -m
app.cli.journal_replica verify` (tải và kiểm toàn chuỗi đích, so local).

**Restore khi DB rollback (host còn):** dừng project hiện tại hoàn toàn (`… --profile ops stop`), giữ thư mục journal
hiện tại. Tạo project mới (env mới: project/subnet/thư mục khác, cùng ops config) rồi `ras_ops restore --backup-id <id>
--identity-file <khóa age của operator, mount một lần> --local-journal <thư mục journal hiện tại>` → kiểm
`journal_relation` (`equal`/`local_ahead`/`replica_ahead`), gate vẫn đóng → `ras_ops open --recover` → kiểm nghiệp vụ →
hạ project cũ. Không chạy hai project cùng journal_id song song.

**Restore khi mất host:** host mới, Docker, repo, env/secret/ops config. `ras_ops restore --backup-id <id>
--identity-file <khóa>` (không `--local-journal`: chỉ replica) → `ras_ops open --recover` → kiểm login/library/original/
xóa-thu hồi → bật timer. Record sau lần ship cuối đã mất (xem số đo). `--without-replica` chỉ khi đích replica không còn
và operator chấp nhận rủi ro; `open` cũng cần `--without-replica`, và replicator/`check` sẽ chặn nếu chuỗi phân kỳ.

**Diễn tập tháng:** `ras_ops drill --env-file <env drill> --project <tên có "drill"> --compose-file … --identity-file
<khóa> --record-state-dir <RAS_OPS_STATE_DIR của deploy thật>`: restore bản tốt mới nhất vào project scratch, recover,
chỉ khởi động `api`, readiness + probe, hạ project (kiểm label) và ghi `drill-state.json`. Không chạy
replicator/edge/worker trong drill: journal scratch không bao giờ được ship. **Env drill phải có project, port, subnet và
thư mục journal/state/staging/maintenance riêng**, không copy nguyên env đang phục vụ. Trước mọi tác động, công cụ từ
chối và thoát (không teardown) khi: thư mục journal đã có `revocations.jsonl` hoặc file `.torn-*`
(`target_journal_exists`), cờ bảo trì đã bật (`drill_maintenance_flag_present`), staging không rỗng
(`drill_staging_not_empty`), `--record-state-dir` trùng một thư mục của chính drill (`drill_dirs_shared`), hoặc project
đã có container/volume (`drill_project_not_fresh`). Teardown chỉ xóa journal/torn file mà lượt này tạo (preflight chứng
minh chúng chưa có), hạ project mà lượt này dựng, và gỡ cờ bảo trì do restore của lượt này đặt (lý do `restore`, thời
điểm sau khi bắt đầu lượt); cờ khác được giữ.

## Kết quả

Chứng cứ thô ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-m8b-20261020\` (lượt chốt ở `final\`, gỡ lỗi ở
`drill\`, countercheck ở `counterchecks-*`, JUnit ở `junit\`). Inventory giữ hash, số liệu, nhãn check (không chép
detail có thể chứa cookie/ID thử). Python `C:\Users\ADMIN\.conda\envs\ras\python.exe`; Docker Desktop trên Windows 11.
Các lượt lỗi/sửa giữ riêng bên dưới, không cộng dồn thành pass.

### Lượt chốt (source chốt, context mới `final`)

Image build từ source chốt (cache layer của máy này): API `sha256:1a366bdd…7148e4`, web `sha256:6667d341…ce9293`, ops
`sha256:a2e2e6af…a849a3`; API M8a cho diễn tập nâng cấp build từ worktree `0de10f4` (`sha256:3e6255fe…a9748c`). 38 file
code/test/deploy giữ nguyên hash từ đầu lượt chốt tới sau sự cố mất điện (đối chiếu snapshot).

Lệnh: `scripts/m8b_drill.py --context <final> setup`, `… build`, `… run upgrade install replica backup disaster alerts
drill` (bước Chrome gọi `scripts/m8b_browser.py` trong image `rasm8a-browser:local`). **141/141 kiểm pass, 0 fail**:

| Phase | Pass | Nội dung chính |
|---|---|---|
| upgrade | 23/23 | deploy M8a (compose + API image của `0de10f4`) có dữ liệu → hold bằng image cũ → migrate/provision M8b → recover → `ras_ops open`; dữ liệu/xóa/phiên đã đóng giữ nguyên; replicator ship toàn bộ journal cũ; backup đầu tiên; log/argv không secret |
| install | 19/19 | contract (Settings + `ops.example.json` trong image); cài mới; replicator journal ro/rootfs ro/UID 10001/không DB-MinIO; check đầu mail `backup_stale` + `restore_drill_overdue` tới 2 người nhận, lượt sau không mail; trang bảo trì 503 JSON/HTML + `Retry-After`; web chết → 503 `service_unavailable` (không 502); dữ liệu thử (upload, review mở, bản chung publish) |
| replica | 17/17 | torn tail không ship; đích dừng → request vẫn phục vụ, alert `replica_error:target_unavailable` + `replica_lag`, đích về → bắt kịp + mail hồi phục; replicator dừng → `replica_stale`; segment bị sửa → `replica_corrupt`, không ghi thêm; journal local phân kỳ → `replica_fork`, alert `journal_rollback` + edge 503; journal khác → exit 3 `replica_journal_mismatch`; credential replicator: ghi đè 412, xóa version AccessDenied; mail không lộ dữ liệu |
| backup | 17/17 | lượt theo lịch (argv như systemd): edge 503 maintenance trong cửa sổ, không 502; staging rỗng; đích chỉ có ciphertext `age`, hash đọc lại = metadata; MinIO chết giữa cửa sổ → failed, writer mở lại, mail ngay; DB dừng → `compose start` kéo db dậy, ready; hold mở → readiness đóng → `reopen=failed`, cờ giữ, mail `maintenance_reopen_failed`, operator recover + `open`; đích backup chết → preflight từ chối trước cửa sổ, không prune; retention giữ mọi bản thật, xóa bản hết hạn ngoài 3 bản tốt mới nhất và upload dở |
| disaster | 40/40 | (a) sau backup: xóa private, revoke bản chung, reject review, logout → DB về backup với journal hiện hành (`equal`) → không hồi sinh; (b) mất host (xóa volume/journal/state/staging) → restore từ backup mã hóa + replica (`replica_only`) → recover → ready, quyết định đã ship giữ; **Chrome 8/8**; (c) replica cũ hơn backup → `journal_behind_backup`, không plaintext/journal; (d) DB + journal local cũ, replica mới hơn → `replica_ahead`, dùng replica, không hồi sinh; (d2) ép `--without-replica` → mở trạng thái cũ (giới hạn), replicator `replica_fork`, check alert + edge 503 sau 6,1 s, `open` từ chối; (e) replica bị sửa → `replica_corrupt` |
| chrome-p3 | 8/8 | Chrome 155.0.8059.84 trong container, CA thử qua NSS: đối chứng không CA từ chối; login; thư viện có item giữ, không có item đã xóa; tải original SHA-256 khớp; item đã xóa 404; logout; phiên thu hồi trước khi mất host → 401 + về `/login`; 0 request ra ngoài origin |
| alerts | 13/13 | `gate_closed:db_behind_journal` (hold) + hồi phục; `api_unavailable` + hồi phục; `cleanup_overdue:overdue_private_deletions` (worker/beat dừng, hạn 60 s thử) + hồi phục khi cleanup chạy; `maintenance_stuck`; `disk_low:staging`; `check_incomplete:backup_target_target_denied`; mỗi mail tới 2 người nhận; quét mọi mail alert: không email User, mật khẩu, secret, token, path, object key |
| drill | 4/4 | `ras_ops drill` restore bản tốt mới nhất vào project scratch, recover, chỉ chạy api, ready; hạ sạch (0 container/volume/journal); head replica không đổi; `restore_drill_overdue` hết trên deploy thật |

**Số đo (máy thử, dữ liệu tổng hợp nhỏ, đích mô phỏng cùng máy — không phải SLO/RPO/RTO):** cửa sổ bảo trì backup
20,4 s (cả lượt 29,0 s; nâng cấp 19,0 s); downtime nâng cấp 35,1 s; restore (a) 27,8 s + recover/open 28,4 s; mất host (b)
restore 25,8 s + recover/open 28,1 s = **53,9 s**; drill tháng 42,4 s. Ship sau một logout 4,8 s (interval 5 s); đích dừng
~36 s → 14 record chưa ship, bắt kịp 4,3 s sau khi đích về; latency thư viện khi đích dừng 1,0–1,3 s/req (không đổi so
với bình thường trên máy này). **Cửa sổ mất record khi mất host:** 1 record (logout ghi khi replicator dừng) mất cùng host;
phiên đó dùng lại được sau restore — đúng giới hạn đã nêu, không phải zero-RPO. Override `--without-replica` (d2): trạng thái
cũ phục vụ 6,1 s trước khi edge bị chặn.

### Unit/integration API, countercheck, kiểm tĩnh

- **Selection API** (danh sách CI + 5 file M8b, 1 deselect như M8a): lượt chạy lại sau mất điện trên test host mới (PostgreSQL tmpfs/Redis/MinIO digest pin, chạy song song countercheck lượt 4): **1192 pass, 1 fail**, 1 deselected, 3709 s; 97/97 test M8b pass. Failure `test_m8a_smtp_delivery.py::test_recipient_reject_is_a_failure` nhận `smtp_timeout` thay vì `smtp_rejected` (deadline test 3 s). Chạy lại riêng file đó 5 lần khi máy rảnh: 5×25/25 pass; ca đó 10 lần dưới tải CPU 40 tiến trình trên `api/` của base `0de10f4` và trên source M8b: 10/10 và 10/10. **Không tái hiện, nguyên nhân chưa rõ**; giữ riêng, không quy cho tải hay cho việc tách `_transmit` (thứ tự deadline → soạn → đọc mật khẩu → process con giữ nguyên). Đây là selection theo rủi ro, không phải full suite.
- Test M8b riêng: replica 16, mã hóa/retention 10, alert 26, host entrypoint 15, MinIO thật 4 (versioning, `If-None-Match`,
  object lock governance, vòng mã hóa thật).
- **Countercheck** (`scripts/m8b_counterchecks.py`, bản sao ngoài repo, source hash trước/sau trùng): lượt 4 trên source chốt **12/12 HOLDS** (bản sạch exit 0; mỗi đột biến làm test nhắm fail với exit 1): ship torn tail, bỏ `If-None-Match` (ghi đè replica), bỏ kiểm fork, chọn chuỗi ngắn hơn, ghép chuỗi phân kỳ, backup đọc `.env`, giữ plaintext sau upload, nhận private key trên server, prune bản tốt cuối, prune sau lượt lỗi, quên mở lại writer, alert lộ dữ liệu.
- `scripts/m8a_deploy_check.py` (có `--settings-image`, gồm `OpsSettings` trên `deploy/ops.example.json`): ok trong phase
  install. `caddy validate` (2.10.2) Valid. `systemd-analyze verify` 4 unit (systemd 257, Debian trixie) exit 0;
  `systemd-analyze calendar` cho lịch mẫu. Không chạy systemd thật.

### Failure, sửa và lượt không tính (giữ lịch sử)

- `ops-check` lần đầu crash: CLI dựng `Settings` cần DB trong khi service cố ý không có DB → sửa CLI chỉ đọc đường dẫn
  journal (+ test). Rebuild image.
- Install lượt 2: prefix replica còn journal của lượt hỏng trước → replicator từ chối `replica_journal_mismatch`, check dựng
  fence (đúng hợp đồng; giữ làm chứng cứ). Xóa dữ liệu thử của prefix bằng root, chạy lại.
- Backup: Caddyfile gắn `Content-Type: text/html` không điều kiện, ghi đè JSON của `/api/*` trong trang bảo trì/lỗi → sửa
  bằng matcher `not path /api/*`, thêm kiểm content-type. `ras_ops backup` in báo cáo check sau báo cáo backup → báo cáo
  backup nay là dòng cuối, kết quả alert nằm trong đó. Ca "DB dừng" không tạo lỗi mở lại vì `compose start` kéo dependency
  lên → giữ thành kiểm riêng, ca mở lại thất bại dùng `recovery hold`.
- Alerts: kỳ vọng `recovery_in_progress` sai; gate thật báo `db_behind_journal` ngay sau hold (record hold chưa mirror) →
  sửa kỳ vọng + runbook.
- Countercheck lượt 1 vô hiệu (bản sạch cũng exit 4: `--api-dir` bị REMAINDER nuốt); lượt 2 **11/12** — `skip_fork_check`
  MISSED vì đột biến chỉ bỏ lớp kiểm trong vòng lặp, lớp sau vòng lặp vẫn chặn; lượt 3 đổi đột biến sang `relation()`:
  12/12. Lượt 4 chạy lại trên source cuối (ở trên).
- `systemd-analyze` lần đầu: `Asia/Saigon` không parse (thiếu tzdata-legacy) → `Asia/Ho_Chi_Minh`.
- Build lượt 1 crash ở harness (decode stdout Windows) → sửa harness.
- **Mất điện máy** khi selection API lượt chốt đang chạy (~67 %, exit 4, không JUnit): không tính; test host cũ dọn, dựng
  test host mới và chạy lại. Lượt diễn tập chốt đã xong trước đó; source không đổi.

**Not-run:** full suite API (> 5.000 node) và năm restore drill C4 (không đổi journal/gate/schema/restore core); web
`npm test/typecheck/build` (web không đổi); systemd thật, host Linux thật; đích S3 thật/HTTPS tới đích; SMTP relay thật;
kiểm mất điện thật của DB/journal; CI GitHub trên commit M8b (chờ push).

**Cleanup và baseline:** baseline trước khi tạo tài nguyên: 24 container, 188 volume, 9 network, 140 image. Sau khi dọn (mọi project `rasm8b*`/`rasm8at*` chỉ hạ sau kiểm label `ras.m8b.context`/project; worktree `0de10f4` gỡ; image `rasm8a-browser:local` và `debian:trixie-slim` do task tạo/kéo đã gỡ): cùng 24 container ID, 188 volume, 9 network, 140 image. Không query/restart container có sẵn, không đụng `ras`/Dify, không `taskkill`; chỉ dừng PID client do harness tự tạo và container one-shot đặt tên riêng. Context thử giữ evidence, journal/backup tổng hợp, khóa `age` thử và credential tổng hợp (ngoài repo).

## Sửa review PR #67

[Report reviewer](reviews/pr67-m8b-pilot-operations.md) tại `41d07d4`: hai P2, sửa trên cùng PR theo
[prompt](tasks/fix-pr67-drill-teardown-alert-recipients.md). Không đổi đường ghi journal, gate, readiness, schema,
`restore()`, replica, backup hay mã hóa. Chứng cứ thô: `C:\Users\ADMIN\AppData\Local\Temp\rv67b\` (regression trước/sau)
và `C:\Users\ADMIN\AppData\Local\Temp\ras-m8b-fix67\` (diễn tập, test, countercheck).

- **P2-1 drill dọn đồ không phải của nó.** `scripts/ras_ops.py`: `drill_preflight` chạy **trước mọi tác động** (trước cả
  lock) và từ chối `target_journal_exists` (journal hoặc `.torn-*` đã có), `drill_maintenance_flag_present`,
  `drill_staging_not_empty`, `drill_dirs_shared` (`--record-state-dir` trùng thư mục của drill),
  `drill_project_not_fresh`; bị từ chối thì thoát, không teardown. `teardown_drill` chỉ xóa journal/torn file (preflight
  chứng minh chưa có trước lượt), hạ project mà lượt này dựng (guard label/project giữ nguyên), và gỡ cờ bảo trì do
  restore của lượt này đặt (lý do `restore`, thời điểm sau khi bắt đầu); drill thành công không còn để lại cờ.
- **P2-2 một người nhận lỗi làm im các người khác.** `app/cli/ops.py check`: thử gửi cho **mọi** người nhận; dedupe/nhắc
  lại/`RESOLVED` theo từng người nhận (khóa hash địa chỉ trong `alerts-state.json`, state cũ dạng `active` vẫn đọc được);
  người lỗi giữ state cũ để lượt sau gửi lại, người đã nhận không bị gửi lặp; có lỗi → exit 5, summary `mailed`/`failed`/
  `delivery_errors`.
- **P3:** runbook `journal_rollback` ghi đánh đổi restore làm mất thay đổi DB sau backup và ca journal local thiếu đuôi
  không có công cụ nạp replica tại chỗ (chưa làm); handoff ghi giới hạn một PUT ≤ 5 GB.

**Kiểm chứng:**

- Regression mới trên `41d07d4` (trước sửa): `test_m8b_ops_host` + `test_m8b_alerts` lọc theo drill/recipient →
  **11 fail / 3 pass** (3 pass: test cũ "drill từ chối project đang phục vụ" và "giữ cờ không phải của drill" — code cũ vốn
  không gỡ cờ — là đối chứng); repro reviewer `rv67\test_rv67_drill.py` + `test_rv67_repro.py` → **2 fail / 1 pass** (ca
  P3). Sau sửa: hai file **53/53**, repro reviewer **3/3**.
- Test M8b đủ 5 file + SMTP M8a trên test host cô lập (MinIO digest): **117/117 pass**.
- Countercheck lượt 5: **16/16 HOLDS** (12 cũ + 4 mới: bỏ preflight drill, drill gỡ mọi cờ, dừng ở người nhận lỗi đầu, đánh
  dấu người nhận lỗi là đã gửi), bản sạch exit 0, source trước/sau trùng.
- Đường drill đổi nên chạy lại diễn tập trên context mới `ras-m8b-fix67` (`setup`, `build`, `run install backup drill`):
  **42/42 pass** (install 19, backup 17, drill 6). Drill thêm ca env drill trỏ vào journal của deploy đang phục vụ: từ chối
  `target_journal_exists` trước mọi tác động, không tạo container nào, journal đang phục vụ nguyên vẹn, deploy vẫn ready,
  không cờ bảo trì; drill thật gỡ cờ của nó, hạ sạch, journal scratch không ship. `restore()` không đổi nên không chạy lại
  phase disaster. Docker sau dọn trùng baseline (24/188/9/140).
- CI run `39422883314` trên `41d07d4`: ba job **success**, API selection **1193 passed** / 1 deselected (gồm
  `test_recipient_reject_is_a_failure` từng fail một lần ở máy này; vẫn không tái hiện, giữ là failure lịch sử chưa rõ
  nguyên nhân). CI trên commit sửa: xem PR (chưa có lúc ghi).

## Thông tin chủ dự án phải cấp trước cutover

1. **Host** Linux chạy Docker Compose (+ quyền quản trị, người chịu trách nhiệm host).
2. **Hai đích S3-compatible** ở miền sự cố khác host và khác nhau: (a) replica journal — versioning (+object lock nếu
   có), credential ghi không có quyền xóa + credential chỉ đọc; (b) backup — versioning, lifecycle noncurrent,
   credential ghi + chỉ đọc. Endpoint HTTPS, region, bucket, prefix.
3. **Cặp khóa `age`**: public key cho server; **người giữ private key** và nơi giữ dự phòng (mất khóa = mất backup).
4. **Người nhận alert** và **người chịu trách nhiệm recovery** (có thể là chủ dự án), kênh liên lạc khi mail không đến.
5. **SMTP relay** (host/port/TLS/CA nếu riêng, tài khoản, sender) cho cả invite/reset và alert.
6. **Domain** + DNS cho `RAS_PUBLIC_HOST` (ACME) hoặc certificate.
7. **Giờ cửa sổ bảo trì** hằng ngày (đo được ở diễn tập: xem số đo) và lịch diễn tập tháng.
8. **Probe/dead-man ngoài host** (dịch vụ ngoài kiểm HTTPS và nhận heartbeat), vì host chết không tự alert được.
9. Mirror image (API/web/ops, MinIO digest, Caddy, PostgreSQL, Redis) vào registry người vận hành kiểm soát (như M8a).
