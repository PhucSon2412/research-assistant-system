# Sửa review PR #53: operator identity và seal inventory

## Base và phạm vi

Tiếp tục **PR #53**, nhánh `feat/m6c2b2c2d5e1-revoked-common-cleanup-runtime`; head được review `3c4c165`, code/test `3b54a6f`, base merge PR #52 `ba20fda`. Fetch/xác minh head trước sửa, giữ toàn bộ docs reviewer chưa commit. Commit/push bản sửa và cập nhật PR **tiếng Việt**, không merge hoặc mở task kế tiếp. Đọc [review](../reviews/pr53-c2d5e1-revoked-common-cleanup-runtime.md), [handoff](../c2d5e1-revoked-common-cleanup-runtime.md), [task gốc](m6c2b2c2d5e1-revoked-common-cleanup-runtime.md), AGENTS và các hợp đồng runtime/recovery.

Safety **pending tới C4**; không `.env`/key/dev/provider/pricing/cutover/C3/C4. Python chỉ Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip, không venv. PG/MinIO/journal/Redis và role thử phải cô lập. Không thêm AI author/trailer hoặc dòng quảng bá trong PR.

## 1. P2 chặn merge: lỗi runtime guard không chứng minh quyền operator

`publication._require_operator` hiện gọi `runtime.require_runtime`, nhưng **mọi `runtime.Halt` đều cho phép đi tiếp**. Lỗi SQL/DB hoặc profile runtime không hợp lệ vì quyền TEMP/protected write/missing privilege cũng đi nhánh này. Đây không phải bằng chứng owner/recovery/operator.

Reviewer đã tái hiện với login runtime thật và gate `auth_safety_pending` thật:

- Setup cô lập cấp thêm TEMP cho runtime: normal cleanup bị chặn `worker_runtime_required`, nhưng `operator_cleanup_revoked` xóa object và commit `purged_at`.
- Fault injection ở bước kiểm danh tính: `worker_runtime_unavailable` cũng dẫn tới object bị xóa và purge thành công.

Hai ca trả `{'deleted': 1}`; expectation phải từ chối trước mutation/S3 đều fail. Evidence và test ngoài repo tại `%TEMP%\ras-pr53-review-20261010`.

### Yêu cầu sửa

1. Operator phải có **bằng chứng quyền dương, kiểm trên connection/login thực**, độc lập với việc runtime guard thành công hay thất bại. Dùng capability/provisioning hiện có nếu đủ; ghi rõ owner/recovery nào được nhận và vì sao. Không suy operator từ `except`, `not runtime`, tên role do caller tự khai, GUC, cờ bypass hoặc engine fallback. Không mở toàn C3 để sửa chốt này.
2. Runtime vẫn bị từ chối khi profile đầy đủ, bị cấp sai TEMP/protected write, thiếu grant hoặc kiểm danh tính không thực hiện được. Lỗi SQL/DB/transaction/release phải dừng có mã kiểm soát, không tiếp tục `_purge_one`/S3, không ghi error/success cleanup để đoán kết quả.
3. Recovery operator hợp lệ vẫn cleanup/sweep khi gate đóng, kể cả object quay lại sau restore và tombstone đã purged; không gọi runtime boundary hoặc mở safety. Không đổi fence, retention, financial ledger, lock graph runtime hay admit S3 nếu không có phát hiện liên quan.
4. Không sửa migration đã dùng tại chỗ. Nếu thực sự cần schema/grants mới, khảo sát head/DB thử và thêm revision tiếp; không cấp rộng để làm test xanh.

### Kiểm bắt buộc

- Regression hai ca reviewer trên PG/MinIO/journal thật: wrong-runtime privilege và identity-check failure; gate pending/hold, không object call, không rows/counter/audit/fence thay đổi.
- Control runtime hợp lệ bị từ chối; owner/recovery hợp lệ được nhận theo contract rõ, identity/permission không đủ bị chặn. Bổ sung release lỗi nếu guard sở hữu Session riêng.
- Normal runtime cleanup vẫn dọn đúng file; recovery cleanup/sweep/recover lặp lại vẫn hoạt động khi gate đóng; không dựng lại nội dung/receipt/credential hoặc ready.
- Đưa lại cơ chế catch-all cũ làm regression fail bằng mutation/S3 assertion, không chỉ khác mã lỗi; khôi phục source trước lượt sealed.

## 2. Hoàn thiện evidence: ID ổn định và số not-run

Reviewer đối chiếu 112 artifact hash và JUnit: **1.575 unique test pass** là đúng; nhóm schema 186/186 pass. Tuy nhiên hai node `test_bad_values_no_sql_unknown[generation-<uuid1>-...]` đổi ID giữa collect và execute. Chuẩn hóa **chỉ đúng hai UUID1 đó** cho kết quả `4.551 collected = 1.575 executed + 2.976 not-run`; số **2.978** hiện đếm thêm hai ID collect cũ như thể chưa chạy. Seal exit 1 là thông tin thật, phải giữ lịch sử.

- Cho parameter ID ổn định (explicit `ids`/`pytest.param(id=...)` hoặc UUID1 fixture ổn định); giữ nguyên ý nghĩa kiểm UUID không hợp lệ. Không normalize bỏ UUID tùy tiện cho toàn inventory hoặc coi exit khác 0 là pass.
- Collect hai lần và chạy nhóm schema để chứng minh exact node identity khớp; seal phải phát hiện thiếu/trùng node và source drift như cũ. Sửa cách báo not-run theo identity chính xác, cập nhật inventory/README/AGENTS/testing/handoff thống nhất.
- Giữ original `sealed-3`/exit 1/source SHA/UUID mismatch; tách kết quả sau sửa theo code SHA mới. Không ghi đè log/XML hoặc cộng pass lịch sử làm pass mới. Không bắt buộc rerun toàn 1.575 chỉ vì sửa ID, nhưng selection hồi quy bản sửa guard/schema/recovery phải rõ, đúng JUnit/hash và not-run; kết quả các nhóm chưa rerun giữ source lịch sử.
- 24 baseline debt và bốn debt PR #50 vẫn riêng; không tự đánh dấu đã sửa, mở runtime privilege hoặc nới safety để làm xanh.

## Bàn giao

Cập nhật [review handoff](../c2d5e1-revoked-common-cleanup-runtime.md), inventory, decisions/testing/README/docs index/AGENTS với contract operator thật và số liệu tách lượt. Giữ giới hạn CLI default owner/drill chưa chạy, power loss, S3 at-least-once và C3/C4. Không cần web nếu không đổi web.

Evidence ngoài repo, quét secret UTF-8/UTF-16 trước bàn giao; dọn đúng tài nguyên thử, báo drift trung thực, không tác động dev. Báo base/code/head, file/migration, tái hiện trước sửa, regression/control/countercheck sau sửa, unique pass/not-run và giới hạn. Push lên **PR #53**, chờ review lại, **không merge**.
