# M8c — bản ứng viên phát hành: hồi quy tổng và nghiệm thu cô lập

Task: [M8c](tasks/m8c-release-candidate-regression.md). Base `origin/main = e0dcdb472506a49691edffdbee6e4071a3f17d5f`
(merge PR #67, tree `cd71eac`, CI `39431941809` pass), nhánh `chore/m8c-release-candidate`. Source migration giữ
**0052**. Kết quả theo node/lớp ở [inventory M8c](m8c-test-inventory.json).

**Trạng thái:** đã đóng băng một bộ image và chạy lại toàn bộ lớp chứng cứ trên môi trường Docker cô lập (credential
tổng hợp, CA/SMTP thử, hai MinIO cùng máy mô phỏng đích ngoài máy). Tìm và sửa **hai lỗi thật** (image hạ tầng chưa pin
đã trôi trên registry; CLI evaluation hỏng từ M8a). **Chưa cutover**, chưa host/S3/SMTP/domain/operator thật. Không đọc
`.env`/key, không đụng dev `ras`/Dify, không gọi provider/arXiv, không gửi mail thật, `pricing_verified` giữ false, không
điền phiếu human, không tạo tag/release. Không thêm tính năng.

## 1. Bản ứng viên

| Image | rc1 (source `e0dcdb4`) | **rc2 — bản ứng viên** (rc1 + sửa evaluation) |
|---|---|---|
| API (`api/Dockerfile`) | `sha256:e4ca453fbe11de98cc4600c6ef8f50ac86657f65e849445b032032f5f6fc44a7` | `sha256:c0936a8189df7335187c457ff56e8c4ff21aebbe338ed7e4240e5c14ec730a06` |
| web (`web/Dockerfile`) | `sha256:59cb514163f9d7f8f5b5163977966ae456f83957e6912f72f59478c5c1dc2cb3` | không đổi (cùng digest) |
| ops (`deploy/ops`, FROM API) | `sha256:53c460d84206bbf2b81cb268fa3f96b4baf2590e91e981ae9d5dca9aa47d8659` | `sha256:22656406b2a9ed9d1697baf4cf754216c0e2b481dd14d4a10268bfe349e066fc` |

- Build một lần mỗi bản (`docker build`, cache layer của máy này), mọi harness dùng **tag alias** trỏ đúng image ID đó
  (`rasm8a…`, `rasm8b…`, `rasc4…`), không có bước build nào của harness chạy lại. 224 file `app/`+`migrations/`+
  `alembic.ini`+`pyproject.toml` trong image khớp bytes host ở cả hai bản; image chạy UID 10001, Python 3.12.15 (host
  Conda 3.12.14). rc1 và rc2 khác đúng một file `app/evaluation/runner.py`; không entrypoint service nào nạp
  `app.evaluation` (kiểm trong image: `app.main`, worker, cleanup, ops, journal_replica, recovery, ops_probe,
  bootstrap_admin).
- Dependency Python dùng khoảng phiên bản và base `python:3.12-slim` theo tag: build lại trên host khác có thể ra bytes
  khác. **Cutover phải dùng chính digest đã nghiệm thu** (mirror/`docker save`), không build lại trên host thật.
  `pip freeze` của image nằm trong inventory.
- Hạ tầng trong `deploy/compose.yaml`, nay đều pin digest đã kiểm và resolve trên registry (xem 2.1): Caddy
  `caddy@sha256:4c6e91c6…e530d` (2.10.2-alpine), PostgreSQL `pgvector/pgvector@sha256:a3625087…6c6b` (pg16 = 16.14), Redis
  `redis@sha256:858f009f…3499` (7.4.11), MinIO `cgr.dev/chainguard/minio@sha256:709099…e510` (PR #66). Chỉ dùng cho
  test, chưa pin: `python:3.12-alpine` (SMTP capture), `node:24-alpine` (relay C4), `rasm8a-browser:local`.

## 2. Lỗi thật đã sửa

### 2.1 Image hạ tầng chưa pin đã trôi trên registry

Tag `pgvector/pgvector:pg16` trên Docker Hub nay trỏ `sha256:7b822b0a…b90a` (PostgreSQL **16.15**, 2026-10-01), trong khi
mọi lượt M8a/M8b đã kiểm `a3625087…` (16.14, chạy từ cache máy này). Máy cài mới sẽ nhận PostgreSQL chưa từng qua hồi quy.
Caddy/Redis lúc kiểm còn trùng digest cache nhưng có thể trôi tương tự.

Sửa: `deploy/compose.yaml` pin edge/db/redis theo digest đã kiểm (dạng chỉ digest: `tên:tag@digest` làm
`docker manifest inspect` tra theo tag đã dời và báo sai); `scripts/m8a_deploy_check.py` buộc **mọi** service được kéo
(`pull_policy` ≠ `never`) pin digest, thay cho kiểm riêng MinIO; CI `caddy validate` dùng cùng digest. Dev
`docker-compose.yml` giữ nguyên (không đổi dữ liệu dev).

Regression: checker cũ trên compose cũ **ok** (lỗ hổng); checker mới trên compose cũ **FAIL 3** (db/edge/redis chưa
pin); checker mới trên compose mới `--check-registry` **ok, 4 digest resolve**. Cài mới trên compose pin đã chạy trong
smoke M8a và diễn tập M8b (container chạy đúng các digest trên). Đổi sang PostgreSQL mới hơn là quyết định riêng: pin
digest khác rồi chạy lại hồi quy.

### 2.2 CLI evaluation hỏng từ M8a

`python -m app.evaluation run --context …` (lớp nghiệm thu của C4) dừng ngay ở seed: `login failed for user_a: 503`.
Nguyên nhân: M8a đặt mặc định `rate_limit.mode="redis"`; runner chạy API trong process, không có Redis/khóa limiter, nên
mọi `POST /auth/login` trả `503 rate_limit_unavailable`. Suite không bắt được vì fixture autouse thay factory limiter bằng
`DisabledRateLimiter`.

Sửa hẹp trong `api/app/evaluation/runner.py`: môi trường evaluation (vốn `RAS_ENVIRONMENT=development`, delivery
`disabled`) đặt tường minh `RAS_RATE_LIMIT__MODE=disabled` như dev compose, xóa cache limiter cùng chỗ xóa cache
settings, và khôi phục environment sau lượt chạy. Không đổi limiter, route hay Settings production.

Regression `test_evaluation.py::test_cli_environment_logs_in_with_the_real_limiter_and_no_redis` dựng đúng
`runner.environment()` (DB/role/bucket/journal mới), dùng factory limiter thật, Redis trỏ cổng đóng: trên `runner.py` của
`e0dcdb4` **1 fail** (`login failed for admin: 503`, log `rate_limit_secret_unconfigured`) + 1 đối chứng pass; sau sửa
**2/2 pass**.

## 3. Kết quả theo lớp

| Lớp | Image | Kết quả |
|---|---|---|
| Full inventory API (`full-r1`) | rc1 | **5.579/5.579 pass trong một lượt**, 0 fail/error/skip/not-run, không trùng; 122 file trên 6 shard cô lập, exit 0; source/image trước-sau trùng |
| Chạy lại file bị ảnh hưởng bởi sửa 2.2 | rc2 | `test_human_review` 50/50; `test_evaluation` 22 pass + **1 fail** giữ riêng (mục 4); `test_c2d7_rag_runtime` **111/111** |
| Năm restore drill C4 | rc1 | backup/restore **41**, session **35**, reset token **54**, review closure **34**, invitation **59** bước — tổng 223, đều exit 0 |
| Smoke M8a HTTPS | rc1 / rc2 | HTTP **68/68** + Chrome **16/16** ở cả hai bản; 222 file app/migrations trong container khớp host |
| Diễn tập M8b | rc1 | **143/143**: upgrade 23, install 19, replica 17, backup 17, disaster (a–e) 40, Chrome sau mất host 8, alerts 13, drill 6 |
| Diễn tập M8b | rc2 | install 19 + backup 17 + drill 6 = **42/42** |
| Web | — | `npm ci` 0, test **79/79**, typecheck 0, build 0; source web trước/sau trùng |
| CLI evaluation | source rc2 | `validate` 80 câu/16 holdout/15 paper OK; `run` exit 0: 400 record, 0 ACL violation, viewer 261 request/87 chunk 0 violation, 0 gold label trong payload, 0 external/blocked call, 239 local embedding, 80 nhãn `pending_human_review` (không chấm semantic) |

**Danh tính node:** collect 5.579 = 5.250 (M6d) − 12 + 341. 12 ID của C4/M6d trong `test_c2a_origin.py` chỉ đổi tên
(`http://web.test…` → `https://web.test…`, fixture M8a/PR #66), cặp 1-1. 341 node mới đều có nguồn gốc PR (commit đầu tiên
định nghĩa hàm, merge đầu tiên trên `main`): #62 9, #63 39, #64 66, #65 50, #66 82 (+12 đổi tên), #67 83. Không node nào
của C4 mất. Suite đầy đủ chạy trong **một invocation**, khác C4/M6d (hợp nhất nhiều lượt).

**Diễn tập M8b — số đo máy thử** (dữ liệu tổng hợp nhỏ, đích mô phỏng cùng máy; không phải SLO/RPO/RTO): cửa sổ bảo trì
backup 18,0 s (cả lượt 25,3 s); downtime nâng cấp 39,0 s; restore DB rollback 24,4 s + recover/open 26,3 s; mất host
24,6 s + 27,3 s = **51,9 s**; drill tháng 41,5 s (rc2 44,7 s). Đích dừng: 14 record chưa ship, bắt kịp 3,8 s sau khi đích
về; ship sau logout 4,1 s; mất host mất **1 record** chưa ship (giới hạn đã biết, không zero-RPO); override
`--without-replica` bị chặn ở edge sau 5,8 s.

## 4. Failure, rerun và lượt không tính (giữ riêng)

- **`test_evaluation.py::test_runner_renews_expired_idle_session_through_real_login` fail 1 lần** trong lượt chạy lại
  file trên rc2 (`rc2-affected-files-2`): login đầu tiên 503 vì gate đóng `auth_safety_postcondition_failed`
  (journal_seq=5, db_seq=5) ngay sau khi mở. Ca này pass trong `full-r1`, `eval-regression-before/after` và chạy lại riêng
  **5/5** (`renews-node-1…5`). Ca chạy trước ca regression mới trong file và không đi qua `environment()`, nên bản sửa
  2.2 không nằm trên đường của nó. Lượt fail chạy cùng lúc với diễn tập M8b r2, smoke r2 và `test_c2d7`.
  `auth_safety_postcondition_failed` là nhánh `except Exception` của `auth_safety.cutover()` nên log không giữ ngoại lệ
  gốc. Thử tái hiện dưới 40 process đốt CPU: 10/10 pass (`load-repro-1…10`). **Nguyên nhân chưa rõ**; không quy cho tải hay Windows.
- Harness, không tính: `rc2-affected-files` (lượt 1) dừng trước test vì shard đã hạ còn `credentials.json` → sửa runner
  khởi động lại shard; context smoke/drill đầu bị hết dải subnet (shard và MinIO mô phỏng chiếm `172.22–172.31/16` của pool
  Docker mặc định, trùng dải /24 harness tự chọn) → hạ shard, dựng lại context; `m8b` đầu bỏ, không chạy phase nào.
- `prep-probe` (3 file, 36 pass) là lượt thử harness trước `full-r1`, không cộng.

## 5. Failure lịch sử chưa rõ nguyên nhân

| Failure | Thử tái hiện | Kết quả |
|---|---|---|
| M8b `test_m8a_smtp_delivery.py::test_recipient_reject_is_a_failure` nhận `smtp_timeout` | full-r1 pass; file 10×25 khi máy chạy drill; đo 50 lượt gửi bị từ chối dưới tải drill và 50 lượt với 40 process đốt CPU (dừng đúng PID) | Không tái hiện. Một lượt (spawn con + TLS + RCPT) 0,94–1,42 s, lượt lạnh đầu 1,39–1,42 s, deadline test 3 s. **Chưa rõ nguyên nhân** |
| M6d `test_c2d5f2_retarget.py::test_revoke_can_commit_while_http_waits` (barrier 10 s) | full-r1 (file 6/6); 10 vòng dưới 40 process đốt CPU (`load-repro-1…10`, image rc2) | 10/10 pass. Không tái hiện, **chưa rõ nguyên nhân** |
| M6d `test_c2d5e2_process_smoke.py::test_real_cli_process_kill_between_fence_and_delete_then_resume` (`orphan_clock_invalid`) | Probe đồng hồ VM Docker (container không mạng, mẫu 0,2 s) suốt full-r1 và các lớp sau; full-r1; 10 vòng dưới 40 process đốt CPU (cùng `load-repro`) | 10/10 pass; probe 354 heartbeat (~5,9 giờ), **0** lần đồng hồ lùi/nhảy. Không tái hiện, **chưa rõ nguyên nhân** |

Guard `orphan_clock_invalid` halt khi `now` của process sau resume nhỏ hơn `fenced_at` do process trước ghi; một bước lùi
của đồng hồ VM giữa hai process sẽ gây đúng triệu chứng. Trong ~5,9 giờ probe không ghi được bước lùi nào; vì không quan sát được nên giả thuyết này **chưa
được chứng minh** và nhãn giữ "chưa rõ nguyên nhân".

## 6. Harness và công cụ

- `scripts/m8c_inventory.py` (mới; sau full-r1 thêm nhánh khởi động lại shard đã hạ, không đổi cách chạy test): full inventory theo shard `c3_test_host` (PostgreSQL tmpfs, Redis, MinIO digest pin),
  Redis riêng cho test limiter, `RAS_TEST_PROCESS_IMAGE` = image đóng băng, `--basetemp` trong context, `@args`, timeout
  theo file chỉ dừng cây PID của chính nó; kiểm image ID trước/sau, băm `api/scripts/web/deploy` trước/sau; đối chiếu ID với
  inventory C4/M6d; `--files`/`--nodes-file` cho rerun riêng tên.
- `scripts/m8a_smoke.py`: từ M8b, compose deploy đòi biến/secret ops nên harness cũ **không render được** (đã tái hiện:
  `required variable RAS_OPS_IMAGE is missing`). Harness nay cấp config/secret M8b tổng hợp; replicator trỏ đích không tồn
  tại trên mạng egress internal (đích lỗi chỉ tăng lag theo M8b). Replica/backup/alert kiểm ở diễn tập M8b.
- Ngoài repo (trong context, có hash trong inventory): adopt image cho `m8b_drill`, quy nguồn gốc node, probe đồng hồ, đo
  SMTP, dựng inventory.

## 7. Not-run và giới hạn

- Full inventory chạy trên rc1; sau sửa 2.2 chỉ chạy lại ba file import `app.evaluation` (đủ theo phạm vi thay đổi, không
  phải full suite trên rc2). Năm drill C4 và phase upgrade/replica/disaster/alerts M8b chạy trên rc1; rc2 chạy smoke đầy
  đủ và M8b install/backup/drill. Không cộng pass rc1 thành pass rc2.
- CI GitHub trên commit M8c: chờ push (xem PR). Không chạy systemd thật, host Linux thật, đích S3/HTTPS thật, SMTP relay
  thật, ACME/domain, mất điện thật; tmpfs/SIGKILL không chứng minh durability.
- Fake provider/local embedding không xác minh charge, routing, giá hay chất lượng semantic; nhãn evaluation vẫn
  `pending_human_review`, M7b 0 verdict thật.
- Hai MinIO cùng máy chỉ mô phỏng miền sự cố; DB và journal cùng rollback vẫn ngoài khả năng phát hiện.

## 8. Điều kiện cutover

| Hạng mục | Chứng cứ cô lập đã có (M8c trên rc1/rc2) | Cần hạ tầng/người thật |
|---|---|---|
| Image | rc2 đóng băng, bytes khớp source, hạ tầng pin digest resolve được | Mirror API/web/ops rc2 + Caddy/PostgreSQL/Redis/MinIO digest vào registry người vận hành kiểm soát |
| Cài mới + HTTPS | Smoke rc1/rc2: Caddy CA thử, cookie Secure, Origin, HSTS, edge chặn path nội bộ | Host Linux, domain + DNS/ACME hoặc certificate |
| Invite/reset | SMTPS + AUTH qua capture, lỗi gửi → bù lời mời | SMTP relay, sender, SPF/DKIM/DMARC (deliverability chưa kiểm) |
| Rate limit | Redis limiter qua edge, 429/503 | Benchmark ngưỡng (chưa đo) |
| Journal độc lập | Replica S3 mô phỏng, chain/fork/torn, cảnh báo rollback + rào edge | Đích S3 journal ở miền sự cố khác: versioning/object lock, credential ghi không xóa + chỉ đọc |
| Backup | Lịch qua entrypoint, `age`, retention, đích mô phỏng; restore DB rollback/mất host | Đích S3 backup khác; cặp khóa `age` + người giữ private key + bản dự phòng; giờ bảo trì |
| Alert | Mail capture tới 2 người nhận, dedupe/resolved, runbook | Người nhận alert, người chịu trách nhiệm recovery, kênh dự phòng; **probe/dead-man ngoài host** |
| Lịch | `systemd-analyze verify` (M8b), argv timer chạy qua harness | Cài timer trên host thật, chạy thật |
| Restore drill | 5 drill C4 + drill tháng M8b | Diễn tập đầu tiên trên hạ tầng thật |
| Chất lượng/chi phí (D) | Evaluation cấu trúc + ACL | Phiếu human M7b; ngân sách live mới; policy/paper thật; `pricing_verified` vẫn false |

Thông tin chủ dự án phải cấp: theo [danh sách M8b](m8b-pilot-operations.md#thông-tin-chủ-dự-án-phải-cấp-trước-cutover)
(host, hai đích S3, khóa `age`/người giữ, người nhận alert/người chịu trách nhiệm recovery, SMTP relay, domain, giờ bảo trì,
probe ngoài host, mirror image) — nay mirror gồm cả digest Caddy/PostgreSQL/Redis đã pin. Thêm: dữ liệu dev có cần chuyển
sang pilot không (mặc định **cài mới**, không đụng dev; chuyển dữ liệu dùng runbook nâng cấp C4/M8b và là quyết định riêng).

### Runbook cutover (cài mới), theo thứ tự

Ký hiệu `C = docker compose --env-file <pilot.env> -p <P> -f deploy/compose.yaml`, `OPS = python3 scripts/ras_ops.py
<lệnh> --env-file <pilot.env> --project <P> --compose-file deploy/compose.yaml`. Mọi file env/secret ngoài repo, không
dùng tên `.env`. Chưa thực thi trên hạ tầng thật.

1. Host: Docker Compose, `python3`, checkout đúng commit phát hành; nạp image rc2 theo digest (`docker pull`/`load` từ
   registry người vận hành), kiểm `docker image inspect` ra đúng ID ở mục 1. Không build.
2. Chuẩn bị ngoài repo theo [M8a](m8a-pilot-entry-hardening.md#runbook) + [M8b](m8b-pilot-operations.md#runbook):
   `pilot.env` từ `deploy/pilot.env.example` (`RAS_API_IMAGE`/`RAS_WEB_IMAGE`/`RAS_OPS_IMAGE` = digest/tag rc2,
   `RAS_DEPLOY_PROJECT=<P>`), `RAS_SECRETS_DIR` (edge_token, rate_limit_key, smtp_password, 4 file credential S3),
   journal/ops-state/staging (0700)/maintenance thuộc UID 10001, `ops.json` + `age-recipients.txt` (chỉ public key).
3. `python3 scripts/m8a_deploy_check.py --env-file <pilot.env> --settings-image <API rc2> --check-registry` → `ok`.
4. `C up -d --wait` (migrate → provision → initialize-installation → api/worker/beat/web/edge/journal-replicator).
5. `C run --rm --no-deps recovery-init python -m app.cli.recovery status` (`safety_ready`, gate open) và readiness nội bộ
   `C exec api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').status)"` → 200.
6. `C exec api python -m app.cli.bootstrap_admin --email <email Admin>` (TTY, không argv mật khẩu).
7. `OPS check` (lượt đầu báo `backup_stale`/`restore_drill_overdue` là đúng) →
   `C run --rm --no-deps journal-replicator python -m app.cli.journal_replica verify`.
8. `OPS backup` (lượt đầu) → `OPS drill --env-file <env drill riêng: project/port/subnet/journal/state/staging/maintenance
   khác> --project <tên có "drill"> --identity-file <khóa age, mang tới một lần> --record-state-dir <RAS_OPS_STATE_DIR thật>`.
9. Cài `deploy/systemd/ras-ops-{backup,check}.{service,timer}` (sửa đường dẫn/env), `systemctl enable --now` hai timer;
   đăng ký probe HTTPS + heartbeat ở dịch vụ ngoài host.
10. Kiểm tay qua domain thật: login/logout, mời một người (mail tới hộp thư thật), reset, upload có xác nhận, thư viện,
    tải original. Rồi mới mở lời mời cho nhóm thử. Hậu điều kiện sai ở bất kỳ bước nào: giữ bảo trì, không xóa hold/
    checkpoint, không sửa journal, không dùng `--without-replica`/`register-existing` để vượt.

## 9. Cleanup

Baseline trước khi tạo tài nguyên: 24 container, 188 volume, 9 network, 140 image. Sau dọn: **cùng 24 container ID và
trạng thái**, 188 volume, 9 network (trùng tập); 143 image = baseline + 3 image ứng viên rc2 giữ có chủ đích (label
`ras.m8c.context`, gỡ bằng `docker image rm` khi không cần). Chỉ hạ project/alias có label của context (`ras.task`,
`ras.m8a.context`, `ras.m8b.context`), 12 volume có label project shard (dev compose của process smoke) và 12 volume ẩn
danh không label, không consumer, tạo lúc 15:37–15:39Z đúng lúc setup sáu shard (cặp PostgreSQL `PG_VERSION 16` +
`.minio.sys` ban đầu của `c3_test_host`, trước khi runner chuyển sang tmpfs/MinIO digest). Không prune; không query/restart
dev `ras`/Dify; burner chỉ dừng đúng PID đã ghi (kiểm tên image `python`), không `taskkill` theo tên. Quét 129 giá trị
credential tổng hợp với 845 file repo: 0 trùng. Credential/evidence/journal/backup tổng hợp giữ ngoài repo tại
`C:\Users\ADMIN\AppData\Local\Temp\ras-m8c-20261020\`.
