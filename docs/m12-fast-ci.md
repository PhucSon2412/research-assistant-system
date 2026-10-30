# M12 — CI nhanh và kiểm thử theo tầng (handoff)

Task: [m12-fast-ci-and-test-tiers](tasks/m12-fast-ci-and-test-tiers.md), áp dụng [chính sách kiểm thử giai đoạn dùng cá nhân](decisions.md#chính-sách-kiểm-thử-giai-đoạn-dùng-cá-nhân-2026-10-29). Nhánh `chore/m12-fast-ci`, base `origin/main = 959e4a4`. Không đổi code ứng dụng, schema hay image; không xóa test, không nới assertion.

## Cơ chế

**DB mẫu đã migrate** (`api/tests/conftest.py`):

- `migrated_template` (scope session, mỗi worker một lần) migrate một DB trống tới head rồi đổi tên thành `ras_tpl_<hash>`. Hash 16 ký tự là SHA-256 của `api/alembic.ini` và mọi file trong `api/migrations/` (bỏ `__pycache__`), nên sửa migration là tạo template mới.
- Dựng dưới `pg_advisory_lock` trên DB admin. Chỉ đổi tên vào chỗ khi đã migrate xong. Sau đó đặt `IS_TEMPLATE true ALLOW_CONNECTIONS false`, nên không ai mở kết nối tới template; một bản dựng dở không bao giờ được clone.
- `migrated_db_url` và `shared_migrated_engine` clone bằng `CREATE DATABASE … TEMPLATE ras_tpl_…`. `register_new_install` vẫn chạy **từng test** với journal riêng trong `tmp_path`; template không có genesis/registration.
- Mỗi session, một bản clone được so với một lần `upgrade head` mới bằng dấu vân tay catalog (`schema_fingerprint`). Dấu vân tay gồm: extension, schema, bảng/sequence/view, cột, constraint, index, trigger, function (định nghĩa, `SECURITY DEFINER`, config, owner, ACL), policy, type/enum, default ACL, ACL/setting cấp database và nội dung mọi hàng mà migration chèn. Lệch thì cả session fail.
- Test kiểm đường `upgrade` thật (`test_migrations.py`, các `*_migration.py`, mọi test dùng `empty_db_url`) vẫn migrate thật.

**Chạy song song theo stack riêng** (`scripts/m8a_test_host.py`):

- `setup --workers N` dựng N bộ PostgreSQL/Redis/MinIO (digest pin theo `deploy/compose.yaml`) trong một Compose project `rasm8at<hex>`, cổng loopback ngẫu nhiên, credential ngẫu nhiên mỗi context.
- `test` chạy `pytest-xdist -n N --dist worksteal`. `conftest` đọc `RAS_TEST_WORKER_ENV_FILE` lúc import và gán cho worker `gwK` đúng stack K. Vì vậy các tài nguyên sau không bao giờ dùng chung giữa worker:
  - role cấp cluster (`c1_runtime`, `c1_recovery`, operator…);
  - key limiter Redis, bucket MinIO;
  - container DB cho vòng `pg_dump`/`pg_restore`.

  Biến môi trường và cache `get_settings` vốn theo process.
- Sau pha xdist, harness chạy riêng các test marker `timing` (cận wall-clock vài giây: SMTP deadline/delivery, poll M7a) **tuần tự trên stack 0**, rồi gộp vào một JUnit. Truyền `-n`/`-p no:xdist` thì harness chạy một pytest thường.
- Admin PostgreSQL của harness đổi tên thành `task_admin` và container DB mang nhãn `ras.task`. Nhờ đó ba vòng dump/restore C3 trước đây bị deselect (cần `RAS_C3_TEST_CONTAINER`) nay chạy được ở tầng đầy đủ.
- Gốc repo được thêm vào cuối `PYTHONPATH`, để các suite `from scripts import …` (M7a/M7c) chạy được.

**Tầng** (`scripts/ci_tiers.py`, marker khai báo trong `api/pyproject.toml` với `--strict-markers`):

| Tầng | Khi nào | Nội dung | Shard × worker |
|---|---|---|---|
| nhanh | `pull_request` (và `workflow_dispatch` với `tier=fast`) | 40 file `FAST`, `-m "not slow"`: 1.407 test (45 `timing`) | 6 × 2 |
| đầy đủ | push `main`, nightly 02:23 giờ VN, `workflow_dispatch` | 112 file (mọi file trừ `NOT_IN_CI`), gồm `slow`: 5.709 test (46 `slow`, 51 `timing`) | 12 × 2 |

Shard chia theo file, tham lam theo số giây đo từ JUnit CI (`scripts/ci_durations.json`, tách `fast`/`full`). Mỗi shard là một runner với context harness riêng. Nightly bỏ qua khi commit `main` đó đã có run push/schedule thành công.

**Lọc đường dẫn** (`scripts/ci_paths.py`, `git diff --name-only HEAD^1 HEAD` trên merge commit của PR; không secret, không action bên thứ ba):

| Thay đổi | Job chạy |
|---|---|
| chỉ `docs/**` hoặc `*.md` ở gốc repo | không job nào (job `changes` ~7 s) |
| `web/**` | web |
| `deploy/**` | deploy-config + API (harness kéo digest trong `deploy/compose.yaml`) |
| `api/**`, `scripts/**`, `evaluation/**` | API tầng nhanh + deploy-config (build image API) |
| `.github/**` hoặc đường dẫn khác/không đọc được diff | mọi job |

`.md` ngoài gốc repo không được coi là docs vì corpus evaluation dùng Markdown làm dữ liệu test.

**Bảo đảm giữ nguyên:**
- Không đọc `.env`, chặn HTTP ra ngoài như cũ.
- Credential container ngẫu nhiên mỗi run.
- JUnit quét secret trước upload (`m8a_ci_scan.py`, artifact `junit-api-<tầng>-<shard>`).
- Không `continue-on-error`, không retry.
- Pip cache theo `api/constraints-rc2.txt` + `api/constraints-dev.txt` + `pyproject`.
- `pytest-xdist==3.8.0`/`execnet==2.1.2` pin ở `api/constraints-dev.txt`; `constraints-rc2.txt` vẫn đúng là freeze của image.
- Deselect cũ (`test_two_dump_restore_rounds_keep_valid_provenance_and_restrict_forgery`) đã bỏ: test nay là `slow` và chạy ở tầng đầy đủ.

## Thêm test `slow` hoặc `timing`

- `@pytest.mark.slow`: vòng dump/restore, test cần container host đặc biệt, hoặc test đơn lẻ nhiều phút. Hiện gồm:
  - các vòng restore trong `test_session_revocation_journal`, `test_reset_token_journal`, `test_invitation_journal`;
  - ba vòng `pg_dump` C3: `test_pre_c4_initial_receipts`, `test_c3_operator_recovery`, `test_c2c2_restore`;
  - `test_evaluation::test_acl_same_doi_hash_matrix_through_runner`.

  Test `slow` chỉ chạy ở tầng đầy đủ, nên **không** gắn cho test bất biến an toàn chạy nhanh.
- `@pytest.mark.timing` (hoặc `pytestmark` cả module): test có assertion cận thời gian dưới vài giây, sẽ fail giả dưới tải song song. Có thể kết hợp với `slow`.
- File test mới tự vào tầng đầy đủ. Muốn chạy ở PR thì thêm vào `FAST` trong `scripts/ci_tiers.py`; file chỉ chạy được với Chrome/image thì thêm vào `NOT_IN_CI` kèm lý do. `test_m12_ci.py` kiểm mọi file đều được phân loại.
- Sau khi đổi selection, cập nhật thời lượng từ artifact JUnit:

  ```
  python scripts/ci_tiers.py durations --tier fast|full <junit.xml>…
  ```

## Lệnh local (Windows, Conda `ras`)

Context phải ở đường dẫn **ngắn** ngoài repo. Context trong thư mục Temp sâu làm file "aside" của journal vượt MAX_PATH; đã gặp 2 `FileNotFoundError` giả, và harness giờ cảnh báo khi đường dẫn > 40 ký tự.

```powershell
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py -m pip install -c .\api\constraints-rc2.txt -c .\api\constraints-dev.txt -e ".\api[dev]"   # một lần
& $py scripts\m8a_test_host.py --context "$env:USERPROFILE\rt\m12" setup --workers 8
# tầng nhanh, song song + pha timing, một JUnit
& $py scripts\m8a_test_host.py --context "$env:USERPROFILE\rt\m12" test --junit "$env:USERPROFILE\rt\fast.xml" -- (& $py scripts\ci_tiers.py files --tier fast).Split(' ') -m "not slow"
# lọc theo file/marker: -- tests\test_search.py -m "not slow"; xáo thứ tự: --ras-shuffle <seed>
& $py scripts\m8a_test_host.py --context "$env:USERPROFILE\rt\m12" cleanup     # chỉ hạ project rasm8at… của context này
```

Harness không chạm `ras-pilot`, dev `ras` hay Dify; `cleanup` chỉ `down -v` project của chính context.

Số đo thật trên máy này (20 CPU, Docker Desktop, demo `ras-pilot` vẫn chạy song song):

| Lượt | Kết quả | Wall |
|---|---|---|
| 3 file (`test_c2c2_invitations`, `test_auth_safety_foundation`, `test_c2c1_bootstrap`), tuần tự, conftest `origin/main` (migrate từng test) | 379 pass | 616 s |
| cùng 3 file, tuần tự, template | 379 pass | 398 s |
| selection CI cũ 33 file, 8 worker, chưa có `slow` | 1.348 pass (gồm test dump/restore trước đây bị deselect) | 695 s |
| tầng nhanh, 8 worker, `--ras-shuffle 20261030 --dist worksteal` | 1.362 + 45 `timing` pass | 680 s |
| tầng nhanh, 8 worker, `--ras-shuffle 7919 --dist loadfile` | 1.362 + 45 `timing` pass | 818 s |
| tầng đầy đủ 112 file (gồm `slow`), 8 worker, `worksteal` (`6ba6a3d`) | 5.657 + 51 `timing` pass, **1 fail** (xem dưới) | 6.542 s (1 h 49 m) |
| chạy lại riêng `test_publication.py`, 8 worker | 18 pass | 323 s |

Fail duy nhất của lượt tầng đầy đủ local: `test_publication::test_publish_refuses_missing_or_altered_common_object` nhận `{"error":"service_recovering"}` ở bước login đầu tiên. Recovery gate đóng ngay đầu test, không có log kèm. Chạy riêng file thì pass, nên có thể là lệ thuộc thứ tự/trạng thái do test chạy trước trên cùng worker để lại (worksteal đổi thứ tự so với các lượt tuần tự trước đây). JUnit không ghi test nào chạy trước trên worker đó, nên **chưa xác định nguyên nhân**. Không retry, không gắn marker; cần điều tra riêng (ví dụ chạy lại tầng đầy đủ với `--dist loadfile` và `--ras-shuffle` cố định để có thứ tự tái lập được, rồi khoanh vùng theo file). Đây là bằng chứng local, không thay cho run CI tầng đầy đủ.

## Thời gian CI trước/sau (GitHub Actions, runner private `ubuntu-24.04`, 2 vCPU)

Thời gian job lấy từ `startedAt`/`completedAt` của GitHub API; số test lấy từ JUnit artifact.

**Trước:**

| Run | Job API | Web | Deploy |
|---|---|---|---|
| `40475972339` (PR #72, `7766164`) | **96m14s**, 1.347 test, 33 file, 1 deselect, không có tầng | 1m08s | 1m04s |
| `40859045124` (push `main`, `959e4a4`) | 63m50s, cùng selection | 1m14s | 0m51s |

**Sau — tầng nhanh (PR):**

| Run | Cấu hình | Job API (từng shard) | Kết quả |
|---|---|---|---|
| `40998103330` (`2cdf76a`) | 2 shard × 4 worker | 37m26s / 17m25s | 1 shard fail (test SMTP `timing`, đã sửa bằng pha `timing`) |
| `41001724530` (`70f65b5`) | 4 × 2, 34 file | 9m24s / 15m00s / 10m14s / 9m45s | 1.328 pass |
| `41001743370` (dispatch, `70f65b5`) | 4 × 3, 34 file | 9m38s / 16m07s / 12m04s / 9m33s | pass; 3 worker không nhanh hơn 2 |
| `41003513311` (`1cec547`) | 5 × 2, 40 file | 11m38s / 11m42s / 9m32s / 14m39s / **17m33s** | 1.406 pass, 0 fail, 44 `slow` không chạy; job `changes` 7 s, web 1m15s, deploy 0m47s |
| cấu hình cuối `c29d25f` | 6 × 2, 40 file | **chưa chạy**: GitHub từ chối khởi chạy (hạn mức) | dự kiến shard lớn nhất ~12–13 phút từ thời lượng đo được, chưa kiểm |

**Sau — tầng đầy đủ (dispatch trên nhánh):**

| Run | Cấu hình | Kết quả |
|---|---|---|
| `40998103067` (`2cdf76a`) | 6 × 4 | **bị hủy** sau 40 phút do chính lượt dispatch đo tầng nhanh cùng nhóm concurrency (đã sửa); tiến độ 40–83 % |
| `41003534611` (`1cec547`) | 12 × 2 | shard 10/9/12 pass trong 46m24s / 49m17s / 58m01s; shard 5 xong 54m47s với 1 fail Linux (`test_case_file_strict_schema_and_location`, sửa fixture ở `8bc6b0b`); 8 shard chạm `timeout-minutes: 60` ở 61–94 %. JUnit có: 2.121 test, 2.120 pass |

**Chưa đạt:**
- Tầng nhanh: chưa có run CI ≤ 15 phút trên cấu hình cuối. Run tốt nhất 4×2 có shard dài nhất 15m00s, với 34 file.
- Tầng đầy đủ: **không đạt ≤ 45 phút** trên runner 2 vCPU. Ước lượng từ log ≈ 820 phút-shard cho cả tầng, tức cần khoảng 20 shard (~900 phút Actions mỗi lượt) để xuống 45 phút.
- Chưa có run tầng đầy đủ nào pass trên CI. Lượt local tầng đầy đủ 8 worker: 5.708/5.709 pass, 1 fail lệ thuộc thứ tự chưa rõ nguyên nhân (mục lệnh local).
- PR chỉ sửa docs/web chưa được chạy thật. Quy tắc lọc được kiểm bằng `test_m12_ci.py`; job `changes` đo được 5–7 s.

## Phát hiện cần orchestrator quyết

- **Runner 2 vCPU bị nghẽn CPU.** Mỗi test chậm 2,3–3,5 lần khi chạy 4 worker. 4 worker chỉ nhanh hơn chạy tuần tự 1,1–1,7 lần; 3 worker không hơn 2 worker (16,1 so với 15,0 phút). Song song chủ yếu đến từ số shard, tức tốn phút Actions. Ước tính mỗi lượt: PR tầng nhanh 6 × ~12 ≈ 75 phút runner (baseline 96); tầng đầy đủ ≈ 820 phút-shard (đo ở `41003534611`).
- **Hạn mức Actions.** Trong lúc đo, các run thử (gồm một run tầng đầy đủ 6 shard bị chính tôi hủy nhầm do chung nhóm concurrency, đã sửa) dùng nhiều phút runner. GitHub sau đó từ chối khởi chạy job mới: *"recent account payments have failed or your spending limit needs to be increased"*. Chủ dự án cần kiểm Billing; có thể giảm `FULL_SHARDS` hoặc tắt nightly nếu muốn tiết kiệm.
- **810 s của test evaluation không phải do migrate.** Profile cho thấy 190 request HTTP, mỗi request ~2,3 s, ~110.000 câu SQL. Phần lớn đến từ `app.auth_safety.cutover → verify` chạy ở **mỗi request**, quét mọi User/head/receipt. Đây là chi phí runtime O(số user × journal) của code ứng dụng, ngoài phạm vi M12. Test được gắn `slow` thay vì sửa.
- **Danh sách theo dõi `timing`:** vài test khóa DB có cận 2–5 s chưa gắn `timing` (`test_c2d5f1_citation_runtime`, `test_c2d5f2_discovery_runtime`, `test_m8a_rate_limit`) và đã pass dưới xdist. Nếu flake, gắn `timing` thay vì nới cận.

## File chưa vào CI (`NOT_IN_CI`, 14 file, 16 test)

| File | Lý do |
|---|---|
| `test_c2d4c/c2d5a/c2d5b/c2d5c1/c2d5c2/c2d5d1_proxy_smoke.py` | Chrome cài trên Windows (`C:\Program Files\…`) + Next server đã build |
| `test_c2d6_browser.py`, `test_c2d7_browser.py` | Chrome headless với web Next + context task |
| `test_c2d5d2a/c2d5d2b/c2d5e1/c2d5e2/c2d5f1/c2d5f2_process_smoke.py` | image API đóng băng (`RAS_TEST_PROCESS_IMAGE`) + context Compose Celery Linux |

Các file `process_smoke` có thể vào tầng đầy đủ sau này bằng cách dùng image do job deploy-config build, ngoài phạm vi M12.

## Ngoài phạm vi / chưa làm

- Chủ dự án/orchestrator cần quyết cách chạy tầng đầy đủ:
  - (a) ~20 shard runner chuẩn, tốn phút;
  - (b) runner lớn hơn (trả phí);
  - (c) self-hosted runner trên máy 20 CPU này, không tốn phút nhưng PR code chạy trên máy có demo `ras-pilot`;
  - (d) bỏ tầng đầy đủ ở mỗi push `main`, chỉ giữ nightly/dispatch.

  Cấu hình hiện tại (12 shard, timeout 60 phút) **không** đạt 45 phút và mỗi lượt tốn ~700 phút; không nên merge nguyên trạng trước khi quyết.
- Cấu hình cuối (`c29d25f`, 6 shard nhanh) và sửa fixture Linux (`8bc6b0b`) chưa có run CI vì hạn mức.
- Không liệt kê test trùng lặp: chưa thấy trường hợp rõ ràng trong phạm vi đã đọc.
