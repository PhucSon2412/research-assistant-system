# M12 — CI nhanh và kiểm thử theo tầng (handoff)

## Sửa review 2026-11-02–03: CI đạt mục tiêu, P2 gate còn mở


**Kết quả trên code cuối `99780fb`:** PR `41614343843` và full dispatch `41615881878` đều success, đạt ≤15 phút / ≤45 phút. Hai full local sau sửa fixture đều pass; observer C2 được kiểm riêng 260 case và CI trên head mới. Giới hạn nguyên nhân của HTTP 503 cũ vẫn giữ rõ bên dưới.

Repo đã public. Log run `41020635195` attempt 2 xác nhận runner thực tế có 4 CPU / 15.983 MiB RAM;
CI đã khởi chạy trở lại. Các dòng billing và cấu hình 12 shard bên dưới là kết quả lịch sử ngày 2026-10-30.

**Bổ sung 2026-11-03, source `4025a7f`:** lượt full local seed `20261103` trên `6378842`
kết thúc **5.712 pass, 2 lỗi setup / 7.023,20 s**, không phải full pass. Có lỗi PostgreSQL
`No buffer space available (0x00002747/10055)` và một lỗi `service_recovering` ở setup Common Search.
Snapshot máy ghi 1.894 socket `CLOSE_WAIT` tới port test, RAM trống 330.044 KiB; socket thuộc các
worker Python. Không đổi thiết lập mạng hay tác động stack demo/dev để xử lý.

Đã tìm được lỗi vòng đời pool trong fixture: `app_env` gọi `get_engine.cache_clear()` mà không
`dispose()` engine đã cache. Hai ca PostgreSQL thật xác nhận connection trả về pool vẫn mở sau
cả đổi DB và teardown (**2 fail / 2,92 s** trên fixture cũ). `4025a7f` đóng pool trước khi xóa cache,
không tạo engine khi cache rỗng; toàn file helper CI cùng hai ca này **31 pass / 1,66 s**.
Code ứng dụng/gate không đổi. Đây là một nguồn giữ tài nguyên đã có regression, nhưng failure
publication cũ không giữ reason/SQLSTATE nên chưa thể kết luận hồi tố chắc chắn là cùng nguyên nhân.
Kết quả xác nhận sau các sửa fixture được chốt trong bảng CI/local bên dưới.

**Chốt thêm fixture storage tại `637d60c`:** lượt chẩn đoán `4025a7f` được dừng khoảng 45%,
không tính là full pass/failure, vì MinIO còn tích tụ socket dù PostgreSQL gần như không còn
`CLOSE_WAIT`. Fixture `upload_api` xóa bucket nhưng chưa đóng client HTTP. Ca dùng socket thật
(chỉ giữ socket, không giữ object pool làm kéo dài lifetime) **1 fail / 3,95 s** trên fixture cũ;
sau sửa, toàn helper CI **32 pass / 3,01 s**. 11 ca uploads/fault/publication cũng pass.
Client nay đóng trong `finally` cả khi setup hoặc dọn bucket lỗi, cache/override luôn được
dọn tiếp; không đổi code ứng dụng. Hai seed đầy đủ trên `637d60c` được chạy lại từ context mới,
seed sau chỉ bắt đầu khi seed trước pass. Đây là xác nhận sau thay đổi source, không retry cùng
source để lấy xanh.

**Sửa observer C2 tại `99780fb`:** full CI `41585993457` trên `637d60c` có một failure
`test_review_request_item_journal_order_both_winners[reader-withdraw]`: không thấy writer chờ khóa.
Đối chứng trên code cũ với ba backend rảnh tái hiện **1 fail / 16,41 s**. Snapshot PostgreSQL ghi
writer thật đang chờ `transactionid` nhưng không có nhãn, còn `c2d2b_writer` nằm trên backend khác
không chờ. `commit()` ngay sau `SET application_name` đã trả connection về pool trước khóa đầu tiên.
Nay withdraw/reject giữ `SET` và khóa đầu tiên cùng transaction; approval vẫn giữ ranh giới rollback
trước copy. Ca reader luôn làm nóng ba backend để bắt lại checkout sai. Mọi timeout và assertion
nghiệp vụ giữ nguyên; toàn file C2 **260 pass / 741,16 s** sau sửa.

Hai full local dưới đây xác nhận fixture/harness trên `637d60c`. Từ đó tới `99780fb` chỉ thay đổi
observer của test C2; app, fixture chung và workflow không đổi. Phần riêng này được kiểm toàn file
và CI trên head mới, không lặp lại hai full local nhiều giờ chỉ vì sửa cách quan sát trong một test.

- Tầng nhanh: 6 shard × 2 worker; tầng đầy đủ: 18 shard × 3 worker, timeout 50 phút/shard.
  Mỗi run full có tối đa 18 API job và hai job web/deploy. Không dùng self-hosted runner.
- Shard chia **theo test sau khi lọc marker**, dùng giây của file chia cho số test được chọn làm trọng số.
  Nhờ đó một file lớn không còn chặn riêng cả shard. Mọi worker tính cùng một tập node trước khi xáo seed.
- Bỏ nightly. Full chạy ở push `main` và `workflow_dispatch`; quyền, HTTP guard, random credential và scan JUnit giữ nguyên.
- Một push docs nối tiếp chỉ tái dùng checks khi head PR trước đó đã có run CI thành công.
  Nếu head trước còn chạy, fail hoặc không tra được, kiểm diff toàn PR. Push mới vẫn hủy run cũ cùng PR.
- JUnit ghi `ras_worker` và `ras_previous` để dựng lại đúng thứ tự từng worker, kể cả failure ở setup.

Kiểm đích workflow/shard/scan: **40 pass**. Chuỗi tiền nhiệm lấy từ thư mục tạm worker `gw7` của lượt fail cũ:
`test_private_index.py` → `test_publish_lists_metadata_only_and_nothing_else` →
`test_publish_refuses_missing_or_altered_common_object`: **30 pass, 650,43 s** trên stack riêng.
Chưa tái hiện được lỗi cũ bằng chuỗi này; chưa kết luận nguyên nhân hoặc đánh dấu P2 đã sửa.

Đối chiếu lại evidence cũ ngày 2026-11-03: thư mục test lỗi trên `gw7` được tạo lúc
`2026-10-30T06:03:15Z`; journal còn giữ có 11 record, parser xác nhận chain hợp lệ,
không torn và không recovery hold mở. Lượt full bắt đầu `05:35:58Z`, sau các lượt xáo
thứ tự trước đó; chưa có bằng chứng hai lượt test dùng chung context bị chồng nhau.
Database test đã bị teardown, log/JUnit chỉ giữ HTTP 503, không giữ reason gate hay SQLSTATE.
Các dữ kiện này giới hạn điều tra, không chứng minh gate đã đúng hoặc đã tìm được lỗi fixture.

Các run xác nhận đọc từ GitHub và JUnit đã quét (thời gian chờ tính từ lúc tạo run đến job cuối;
tổng thời gian là cộng wall time mọi job, không phải số phút tính tiền):

| Run | Cấu hình / kết quả | Chờ cả run | API dài nhất | Tổng mọi job |
|---|---|---|---|---|
| `40475972339` | baseline, 1.347 test API pass | 96m18s | 96m14s | 98m26s |
| `41390031761` | nhanh 6 × 2, `fed2c44`, 1.408 pass | 11m11s | 10m34s | 50m07s |
| `41391198771` | chỉ docs `75fc8df`: API/web/deploy **skipped** | — | — | — |
| `41390390204` | full 18 × 3, `fed2c44`, 5.706 pass + 4 skip | — | 45m31s | — |
| `41398963818` | nhanh, `2eea465`, 1.410 pass | 10m45s | 8m58s | 51m04s |
| `41400676325` | full, `2eea465`, 5.708 pass + 4 skip + 1 fail | 35m19s | 35m06s | 478m16s |
| `41407002265` | nhanh, `33b707b`, 1.410 pass; web skipped đúng diff API | 10m05s | 9m53s | 50m14s |
| `41406996523` | full, `33b707b`, 5.707 pass + 4 skip + 2 fail | 34m33s | 33m51s | 453m23s |
| `41413723589` | nhanh, `6378842`, 1.410 pass; web skipped đúng diff API | 10m28s | 10m18s | 54m49s |
| `41415006918` | full, `6378842`, 5.710 pass + 4 skip, 5.714 ID duy nhất | 33m57s | 33m46s | 440m54s |
| `41580027455` | nhanh, `4025a7f`, 1.412 pass | 12m29s | 12m19s | 55m03s |
| `41581281471` | full, `4025a7f`, 5.712 pass + 4 skip | 35m20s | 35m07s | 494m09s |
| `41584813298` | nhanh, `637d60c`, 1.413 pass | 9m59s | 9m48s | 53m07s |
| `41585993457` | full, `637d60c`, 5.712 pass + 4 skip + 1 fail observer C2 | 32m13s | 32m03s | 453m11s |
| `41614343843` | nhanh, `99780fb`, 1.413 pass | 10m21s | 10m10s | 50m26s |
| `41615881878` | full, `99780fb`, 5.713 pass + 4 skip | 35m12s | 34m58s | 454m04s |

Run PR `41413723589` và mọi job cần chạy đều success; tổng phút dùng metadata đã làm mới đủ timestamp.
Full `41415006918` trên cùng code cũng success, cả 18 shard API và web/deploy đều pass;
4 skip là ca dành riêng cho Windows. Đã đạt thời gian CI nhanh ≤15 phút và full ≤45 phút.

Run full `41390390204` vượt mục tiêu 31 s: một test 1.698 s phải đợi sau 35 tiền nhiệm/962,7 s
trên cùng worker. Shard nay dùng thêm ba trọng số node thực đo và chạy test nặng trước; phần còn lại
vẫn chia giây theo file, không có inventory toàn node.

Fail duy nhất của `41400676325` là `test_busy_common_item_is_skipped_without_blocking_later_candidates`:
cận `< 2` s đo được 2,925897 s khi xdist có tải. `33b707b` chuyển đúng test này sang pha `timing`
tuần tự, giữ nguyên cận và mọi assertion; test vẫn chạy trong full. Đây không phải lỗi gate đang review.
Run full `41406996523` vẫn fail: ca SKIP LOCKED mất 2,014897 s dù đã ở pha tuần tự;
ca `test_revoke_waiting_on_the_response_lock_lets_that_response_finish` nhận revoke 503 thay vì 200.
JUnit ghi tiền nhiệm của ca thứ hai là `test_common_rag::test_revoke_during_generation_withholds_the_answer`.
Đây là kết quả trước sửa fixture `6378842`, không phải kết quả full success sau cùng trong bảng.

Sửa fixture ở `6378842`:

- Counterexample cho response/revoke: chậm proof 3 s làm PostgreSQL trả `55P03`, HTTP trả
  `503 revocation_unavailable`. Ca thử trước đây bắt đầu revoke **trước** các proof cuối, nên proof
  chậm ăn hết hạn chờ khóa 2 s. Nay đăng nhập revoker trước và bắt đầu revoke tại `release`, sau mọi
  proof thật nhưng trước rollback; quan sát đúng PID giữ khóa và câu SQL `library_items`, thay vì
  bất kỳ backend chờ khóa nào. Cùng cách đồng bộ được sửa cho ca RAG; thread được join cả khi test fail.
- Counterexample SKIP LOCKED: chậm `entry` 0,6 s/batch làm cả pass mất 4,547 s dù không chờ item.
  Cận 2 s nay đo tổng các thao tác lấy khóa thật, thay vì cả gate/proof/commit nhiều batch. Khóa đối
  kháng vẫn giữ tới khi pass hoàn tất; mọi kiểm tra skip, số reference/link và lượt sau giữ nguyên.
  Thêm biến thể proof chậm chứng minh cả pass có thể hơn 2 s trong khi thao tác khóa vẫn dưới cận.
- Sau sửa, cặp tiền nhiệm Search và hai biến thể SKIP LOCKED pass **4/4, 462,62 s** dưới cùng injection;
  RAG với proof URL chậm 3 s pass **1/1, 192,19 s**. Đây là bằng chứng fixture, không phải nguyên nhân
  đã xác minh của lỗi login `service_recovering` cũ.
- Đối chứng hẹp trên thao tác thật: plugin riêng đặt `skip=False` làm test fail đúng tại cận khóa
  (`sum(lock_times) == 2,0`, yêu cầu `< 2`); 17,02 s gồm harness. Không sửa source app để làm đối chứng.

Một lỗi fixture khác đã có counterexample: deadline 0,5 s của ca approval-copy có thể hết khi proof SQL
chưa xong, trước khi vào SDK mà test định đo; `settled == []` gây fail giả. Bản sửa `882a2c0` dùng
timeout asyncio thật, khởi hạn tại I/O đang kiểm, giữ các kiểm tra ownership/cleanup/gate và thêm ca
riêng kiểm hạn tổng gồm proof. Counterexample trước sửa fail; sau sửa hai biến thể SDK, ca proof chậm
và ca publication cũ **4 pass / 45,86 s**. Không đổi code app.

Lượt full chẩn đoán local tại `2eea465` bị dừng ở 68% vì plugin riêng in reason vào stdout mà
`test_backup` đang kiểm; không phải bằng chứng app fail và không tính là full pass. Plugin đã chuyển
ghi reason ra file riêng. Lượt chẩn đoán kế tiếp ở `33b707b` dừng khi fixture vừa được sửa để tránh
trộn source hai phiên bản trong một kết quả; không tính pass.

Full local xác nhận source `6378842` (8 worker, gồm cả các ca Windows):

| Seed / phân phối | Ngày | Kết quả | Wall gồm cả hai pha |
|---|---|---|---|
| `20261102` / `loadfile` | 2026-11-02 | 5.714 ID duy nhất, tất cả pass, 0 skip; head đầu/cuối `6378842` | 7.844,34 s (2h10m44s) |
| `20261103` / `worksteal` | 2026-11-03 | 5.714 ID duy nhất: 5.712 pass, 2 lỗi setup; head đầu/cuối `6378842` | 7.023,20 s (1h57m03s) |

Lượt seed `20261103` bắt đầu ngày 2026-11-02 bị gián đoạn ở 19%, không còn process và không có
file kết quả khi tiếp quản ngày 2026-11-03; không tính là pass hay failure của ứng dụng. Log được giữ
riêng. Lượt xác nhận còn thiếu được khởi chạy ngày 2026-11-03 trên context mới, cùng source/seed.
Kết quả của lượt đó là hai lỗi setup đã ghi trong bảng, dẫn tới bản sửa pool `4025a7f`; không retry
cùng source để lấy xanh. Hai seed trên source sau sửa được theo dõi riêng:

| Source | Seed / phân phối | Kết quả | Wall gồm hai pha |
|---|---|---|---|
| `4025a7f` | `20261102` / `loadfile` | dừng khoảng 45% để hoàn thiện teardown client S3; không tính pass | — |
| `4025a7f` | `20261103` / `worksteal` | không chạy sau khi dừng lượt trước | — |
| `637d60c` | `20261102` / `loadfile` | 5.717 ID duy nhất, tất cả pass, 0 skip; head đầu/cuối khớp | 7.770,91 s (2h09m31s) |
| `637d60c` | `20261103` / `worksteal` | 5.717 ID duy nhất, tất cả pass, 0 skip; head đầu/cuối khớp | 6.335,30 s (1h45m35s) |

Lượt `6378842` dùng plugin autouse `guard/diagnose.py`, có hạn chế với failure ở setup.
Hai lượt `637d60c` dùng `guard/protocol_diagnose.py` trước setup, ghi thứ tự, reason gate/SQLSTATE
và snapshot trước teardown khi fail; không ghi vào stdout. JUnit mỗi lượt có đủ 5.717 metadata worker.
Lệnh test của lượt thứ hai (context đã `setup --workers 8`; plugin chẩn đoán ở `guard` của context):

```powershell
& $py scripts/m8a_test_host.py --context $ctx test --junit "$ctx/20261103.xml" -- (& $py scripts/ci_tiers.py files --tier full).Split(' ') --ras-shuffle 20261103 --dist worksteal --durations=10 -p protocol_diagnose -p no:warnings
```

Lệnh full local 8 worker nằm trong `docs/dev-setup.md`. P2 lỗi gate vẫn mở cho tới khi có nguyên nhân
và regression đúng thứ tự; không dùng các run xanh khác để thay kết luận đó.

Task: [m12-fast-ci-and-test-tiers](tasks/m12-fast-ci-and-test-tiers.md), áp dụng [chính sách kiểm thử giai đoạn dùng cá nhân](decisions.md#chính-sách-kiểm-thử-giai-đoạn-dùng-cá-nhân-2026-10-29). Nhánh `chore/m12-fast-ci`, base `origin/main = 959e4a4`. Không đổi code ứng dụng, schema hay image; không xóa test. Thay đổi phạm vi đo thời gian của fixture SKIP LOCKED đã được giải thích ở trên.

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
| nhanh | `pull_request` (và `workflow_dispatch` với `tier=fast`) | 40 file `FAST`, `-m "not slow"`: 1.413 test (45 `timing`) | 6 × 2 |
| đầy đủ | push `main`, `workflow_dispatch` | 112 file (mọi file trừ `NOT_IN_CI`), gồm `slow`: 5.717 test (46 `slow`, 53 `timing`) | 18 × 3 |

Shard chia theo test sau khi lọc marker, tham lam theo số giây đo từ JUnit CI (`scripts/ci_durations.json`, tách `fast`/`full`), có ba trọng số riêng cho hotspot. Chạy các test nặng trước. Mỗi shard là một runner với context harness riêng. Đã bỏ nightly.

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
$ctx = "$env:USERPROFILE\rt\m12-$([guid]::NewGuid().ToString('N').Substring(0,6))"
& $py -m pip install -c .\api\constraints-rc2.txt -c .\api\constraints-dev.txt -e ".\api[dev]"   # một lần
& $py scripts\m8a_test_host.py --context $ctx setup --workers 8
# tầng nhanh, song song + pha timing, một JUnit
& $py scripts\m8a_test_host.py --context $ctx test --junit "$ctx\fast.xml" -- (& $py scripts\ci_tiers.py files --tier fast).Split(' ') -m "not slow"
# tầng đầy đủ, gồm slow; đổi seed cho lượt kiểm thứ tự tiếp theo
& $py scripts\m8a_test_host.py --context $ctx test --junit "$ctx\full.xml" -- (& $py scripts\ci_tiers.py files --tier full).Split(' ') --ras-shuffle 20261103 --dist worksteal
# lọc theo file/marker: -- tests\test_search.py -m "not slow"; xáo thứ tự: --ras-shuffle <seed>
& $py scripts\m8a_test_host.py --context $ctx cleanup     # chỉ hạ project rasm8at… của context này
```

Harness không chạm `ras-pilot`, dev `ras` hay Dify; `cleanup` chỉ `down -v` project của chính context,
giữ log/metadata. Dùng thư mục mới cho bộ lượt tiếp theo; `setup` không ghi đè context đã tồn tại.

Đo local lịch sử ngày 2026-10-30 (20 CPU, Docker Desktop, demo `ras-pilot` vẫn chạy song song):

| Lượt | Kết quả | Wall |
|---|---|---|
| 3 file (`test_c2c2_invitations`, `test_auth_safety_foundation`, `test_c2c1_bootstrap`), tuần tự, conftest `origin/main` (migrate từng test) | 379 pass | 616 s |
| cùng 3 file, tuần tự, template | 379 pass | 398 s |
| selection CI cũ 33 file, 8 worker, chưa có `slow` | 1.348 pass (gồm test dump/restore trước đây bị deselect) | 695 s |
| tầng nhanh, 8 worker, `--ras-shuffle 20261030 --dist worksteal` | 1.362 + 45 `timing` pass | 680 s |
| tầng nhanh, 8 worker, `--ras-shuffle 7919 --dist loadfile` | 1.362 + 45 `timing` pass | 818 s |
| tầng đầy đủ 112 file (gồm `slow`), 8 worker, `worksteal` (`6ba6a3d`) | 5.657 + 51 `timing` pass, **1 fail** (xem dưới) | 6.542 s (1 h 49 m) |
| chạy lại riêng `test_publication.py`, 8 worker | 18 pass | 323 s |

Failure ngày 2026-10-30: `test_publication::test_publish_refuses_missing_or_altered_common_object` nhận `{"error":"service_recovering"}` ở login đầu tiên; chạy riêng file pass. JUnit lúc đó chưa có worker/tiền nhiệm và không giữ reason gate. Điều tra, các sửa fixture và giới hạn kết luận hiện hành nằm ở mục sửa review đầu tài liệu; không tính lượt fail này thành bằng chứng không phụ thuộc thứ tự.

## Chi tiết job CI public

Source `99780fb`. Số file là file có case thực sự được chọn trong JUnit; cột case gồm cả skip.
Cột `slow` đối chiếu marker khai báo trong source. Timestamp lấy từ REST API sau khi run hoàn tất.

PR nhanh [`41614343843`](https://github.com/PhucSon2412/research-assistant-system/actions/runs/41614343843):

| Shard | Wall | Case | File được chọn | Slow |
|---|---|---|---|---|
| 1 | 6m18s | 235 | 38 | 0 |
| 2 | 8m26s | 234 | 37 | 0 |
| 3 | 10m10s | 235 | 38 | 0 |
| 4 | 8m11s | 235 | 37 | 0 |
| 5 | 8m41s | 234 | 36 | 0 |
| 6 | 7m34s | 240 | 38 | 0 |

changes 0m07s; deploy 0m59s; web skipped.
Tổng 1413 case / 40 file / 0 slow; API cộng 49m20s; mọi job cộng 50m26s.

Dispatch đầy đủ [`41615881878`](https://github.com/PhucSon2412/research-assistant-system/actions/runs/41615881878):

| Shard | Wall | Case | File được chọn | Slow |
|---|---|---|---|---|
| 1 | 34m58s | 303 | 71 | 3 |
| 2 | 21m02s | 314 | 78 | 3 |
| 3 | 19m59s | 312 | 73 | 1 |
| 4 | 21m56s | 315 | 79 | 2 |
| 5 | 29m40s | 318 | 81 | 2 |
| 6 | 32m18s | 318 | 79 | 2 |
| 7 | 18m46s | 317 | 81 | 1 |
| 8 | 18m14s | 316 | 82 | 2 |
| 9 | 28m09s | 317 | 83 | 4 |
| 10 | 22m27s | 319 | 82 | 3 |
| 11 | 25m23s | 317 | 81 | 3 |
| 12 | 27m21s | 318 | 82 | 5 |
| 13 | 29m50s | 317 | 80 | 2 |
| 14 | 26m50s | 322 | 81 | 2 |
| 15 | 18m04s | 321 | 80 | 4 |
| 16 | 19m44s | 325 | 80 | 3 |
| 17 | 27m34s | 324 | 81 | 2 |
| 18 | 29m44s | 324 | 82 | 2 |

changes 0m08s; deploy 0m48s; web 1m09s.
Tổng 5717 case / 112 file / 46 slow; API cộng 451m59s; mọi job cộng 454m04s.

Thời gian cộng khác thời gian chờ do các shard chạy song song. Bốn skip của full dành riêng cho Windows;
cả bốn đã chạy/pass trong hai lượt full native Windows nêu trên.

## Lịch sử đo 2026-10-30 (runner private `ubuntu-24.04`, 2 vCPU)

Đây là số đo và giới hạn của lượt triển khai trước review. Cấu hình và kết quả hiện hành ở mục sửa review đầu tài liệu.

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

## Phát hiện ngày 2026-10-30 (lịch sử)

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

## Phương án chưa chốt ngày 2026-10-30 (đã thay bằng task sửa)

Task sửa ngày 2026-11-02 đã chọn runner public, tối đa 18 API shard, bỏ nightly và cấm self-hosted.
Các phương án dưới đây chỉ lưu lịch sử, không phải quyền thực hiện hiện hành.

- Chủ dự án/orchestrator cần quyết cách chạy tầng đầy đủ:
  - (a) ~20 shard runner chuẩn, tốn phút;
  - (b) runner lớn hơn (trả phí);
  - (c) self-hosted runner trên máy 20 CPU này, không tốn phút nhưng PR code chạy trên máy có demo `ras-pilot`;
  - (d) bỏ tầng đầy đủ ở mỗi push `main`, chỉ giữ nightly/dispatch.

  Cấu hình hiện tại (12 shard, timeout 60 phút) **không** đạt 45 phút và mỗi lượt tốn ~700 phút; không nên merge nguyên trạng trước khi quyết.
- Cấu hình cuối (`c29d25f`, 6 shard nhanh) và sửa fixture Linux (`8bc6b0b`) chưa có run CI vì hạn mức.
- Không liệt kê test trùng lặp: chưa thấy trường hợp rõ ràng trong phạm vi đã đọc.
