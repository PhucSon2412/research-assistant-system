# Review PR #53 — C2d-5e1 cleanup common revoked runtime

Ngày **2026-10-10**. PR **OPEN, chưa merge**, head `3c4c165c974d022bb9c054ac443c2c6bc4f25c54`, code/test `3b54a6f705304fc0c22cf729cd2930e81fe6b32f`, base merge PR #52 `ba20fda402840243f13bd4bb27e98c4c0cb17562`. Sau code/test sealed chỉ docs; source migration **0044**, không truy vấn revision DB dev.

## Kết luận

**Có một P2 chặn merge:** operator gate fail open khi kiểm runtime lỗi hoặc runtime có profile không hợp lệ. Reviewer tái hiện hai ca với runtime login thật và safety pending: object bị xóa, `purged_at` được commit. Cần sửa và review lại trước merge. [Prompt sửa PR #53](../tasks/fix-pr53-operator-cleanup-identity-and-seal.md).

Nhóm cleanup/migration/ORM độc lập **101/101 pass**, 0 fail/error/skip; **hai regression mới 2/2 fail đúng lỗi P2**, không phải setup error. Agent có 1.575 unique pass đã đối chiếu artifact, nhưng không bao phủ hai ca này. Safety **pending tới C4**; không mở pricing/cutover hoặc chứng nhận các background/consumer còn lại.

## P2 — Không dùng lỗi runtime guard làm bằng chứng operator

Vị trí: [publication._require_operator](../../api/app/publication.py), dòng 389–402, đặc biệt `except runtime.Halt: return`.

`runtime.require_runtime` có thể từ chối vì quyền TEMP/protected DML/membership/missing grant, hoặc không kiểm được DB (`worker_runtime_unavailable`). `_require_operator` hiện cho **mọi** trường hợp này qua, như thể đã chứng minh owner/recovery/operator. Runtime cấu hình sai hoặc bước kiểm lỗi được nhận vào cleanup operator vốn bỏ gate, không tạo fence và giữ DB transaction qua S3. Tách tên helper không bảo vệ được ranh giới này.

### Tái hiện độc lập

PG/MinIO/journal cô lập, fixture tạo bản chung được runtime index với provider giả rồi runtime revoke; login vẫn `c1_runtime`. Khôi phục `auth_safety.cutover` thật, không fixture ready: normal cleanup trả `halted:recovery_gate_closed`.

| Ca | Kiểm trước | Operator cleanup thực tế | Expectation |
| --- | --- | --- | --- |
| Setup owner cấp TEMP trên **DB thử** cho runtime | Normal cleanup trả `halted:worker_runtime_required` | `{'deleted': 1}`, original vắng, tombstone purged | Từ chối trước S3/mutation |
| Inject `runtime.require_runtime` ném `worker_runtime_unavailable` một lần | Safety vẫn pending | `{'deleted': 1}`, original vắng, tombstone purged | Không đủ chứng cứ danh tính phải từ chối |

Không nới runtime production hoặc sửa code app để tái hiện. Test/evidence ngoài repo: `%TEMP%\ras-pr53-review-20261010\test_operator_identity.py`, `conftest.py`, `operator-repro.{log,xml}`. Hai case đều fail tại assertion yêu cầu `operator_cleanup_requires_operator`, với `error=None`, `object_present=False`, `purged=True`, result `deleted=1`.

### Yêu cầu sửa

Operator phải có bằng chứng quyền **dương** trên actual connection/login, không suy từ runtime guard thất bại; lỗi identity/permission/DB/release dừng trước S3. Runtime vẫn bị chặn khi profile đầy đủ, bị cấp sai quyền, thiếu grant hoặc probe lỗi. Owner/recovery hợp lệ vẫn cleanup/sweep khi gate đóng; không cần mở toàn C3. Regression cần no-I/O/no-write assertion và control operator hợp lệ; restore/recover giữ safety pending. Không thêm bypass flag, GUC hoặc owner fallback. Chi tiết phạm vi/kiểm trong prompt sửa.

## Evidence seal: pass đúng, not-run lệch hai identity

Đối chiếu **112/112 SHA-256 artifact**, source-before/source-after/current **311 file API/scripts khớp**. JUnit chín nhóm có **1.575 unique executed/pass**, không failure/error/skip. Tám countercheck cuối đều có assertion failure, không setup error; lượt gate yếu đầu được giữ riêng như agent báo. Đây là kiểm artifact, không phải reviewer rerun 1.575 test.

Nhóm schema có hai node UUID1 sinh khi import trong `test_bad_values_no_sql_unknown`; collect-only và execute khác ID. Chỉ chuẩn hóa UUID1 trong đúng hai node này thì selection/JUnit khớp, không thiếu/trùng ca. Vì vậy:

| Metric | Đối chiếu |
| --- | ---: |
| Collected logic | 4.551 |
| Executed logic | 1.575 |
| Not-run logic | **2.976** |
| Not-run raw string hiện báo | 2.978 (thêm hai ID collect đã đổi) |

Seal exit **1** phải giữ lịch sử, không được đổi thành pass. Sửa param ID ổn định và báo exact selection/not-run để các lượt sau có thể xác minh tự động; giữ raw run cũ/source SHA. [Inventory agent](../c2d5e1-test-inventory.json) chưa được reviewer sửa số liệu thay agent. 24 baseline debt theo evidence agent và bốn debt PR #50 giữ riêng; reviewer không rerun base hoặc gọi đã sửa.

## Reviewer thực chạy

Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe` **3.12.14**, pip **26.2.1**; context riêng, không `.env` fallback/provider key. Host runner chỉ cấp DB/MinIO/Redis/journal thử và chặn HTTP ngoài; không gọi OpenRouter/arXiv. Không sửa production/test trong repo.

| Nhóm | Kết quả |
| --- | ---: |
| `test_c2d5e1_cleanup_runtime.py` | 71/71 pass |
| `test_c2d5e1_migration.py` | 2/2 pass |
| Toàn `test_migrations.py`, gồm ORM/schema | 28/28 pass |
| Hai regression operator mới ngoài repo | **2 fail đúng P2**, 0 error/skip |

Nhóm 101 chạy 366,21 giây; có ba warning hiện hữu (Starlette test client, hai computed default Alembic). Theo collect agent 4.551, **4.450 node repo không rerun** trong review; hai regression mới nằm ngoài inventory repo. Source hashes 311 file hiện hành khớp sealed agent và snapshot review; snapshot review đầu được ghi khi suite đã bắt đầu, không gọi đó là manifest trước khởi chạy toàn suite.

Không rerun web/Chrome, Linux process smoke, toàn 1.575 test hoặc 24 base debt. Smoke SIGKILL worker/beat được đối chiếu trong artifact agent, không là phép thử reviewer mới. Không đo latency/SLO/charge/power-loss. CLI Compose owner bị từ chối và drill backup/restore chưa chạy lại vẫn là giới hạn; `cleanup_busy` chưa có test riêng, không suy đã được kiểm.

## Tài nguyên và evidence

Evidence: `%TEMP%\ras-pr53-review-20261010`, gồm log/XML, helper regression, agent artifact verification, source hash và `review-results.json`. Project thử `rasc2d5e181ff8a31`: đã xác minh labels/sole volume consumer rồi gỡ đúng **3 container, 3 anonymous volume, 1 network**; không tạo/tag image worker mới.

Quét generated credentials UTF-8/UTF-16: chỉ còn trong hai file cấu hình thử, đã xóa cùng `empty.env` sau cleanup. Chín container có sẵn giữ ID/name/image; StartedAt không đổi quanh cleanup, không có snapshot StartedAt lúc bắt đầu nên không khẳng định toàn khoảng review. Không stop/start/query/migrate dev, không đọc/sửa `.env`, đổi pricing hoặc gọi provider.

Hai lỗi helper review được sửa trước khi kết luận: tìm manifest bằng ngưỡng số phần tử thay vì key thực, và so đường dẫn secret-scan bằng slash thay vì backslash Windows. Không phải lỗi ứng dụng hoặc test nghiệp vụ, không tính pass. Reviewer chỉ thêm/cập nhật docs; không commit/push/merge hoặc đăng comment PR.
