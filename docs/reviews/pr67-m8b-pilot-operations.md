# Review PR #67 — M8b vận hành nhóm thử

## Review lại tại `68351dc` — hai P2 đã sửa

**Kết luận (2026-10-20): hai P2 đã sửa, không thấy điểm chặn merge mới trong scope M8b đã kiểm.** PR vẫn OPEN, chưa merge; source **0052**, chưa cutover. Mục "Kết luận tại `41d07d4`" bên dưới là lịch sử trước khi sửa.

**Bản sửa:**

- **P2-1 (drill):** `drill_preflight` chạy **trước** lock và mọi side effect, chỉ đọc. Nó từ chối khi:
  - đã có journal hoặc file torn;
  - cờ bảo trì đang bật;
  - staging không rỗng;
  - `--record-state-dir` trùng thư mục của chính drill;
  - project có container/volume.

  Từ chối thì không teardown. Teardown chỉ gỡ cờ `restore` do lượt này đặt.
- **P2-2 (alert):** gửi tới từng người nhận; dedupe/nhắc lại/`RESOLVED` theo hash từng người nhận; người lỗi giữ state cũ để thử lại; có lỗi thì exit 5.

**Kiểm chứng của reviewer** (Python Conda `ras`, basetemp ngoài repo; không Docker/MinIO/SMTP thật):

- Hai repro ban đầu: trên `41d07d4` fail, nay **pass**. Ca mô tả P3 vẫn pass.
- Test M8b `replica/artifacts/alerts/ops_host`: **79/79 pass** (67 cũ + 12 regression mới).
- 4 ca độc lập thêm, **4/4 pass**:
  - B lỗi, A không bị gửi lặp, B được thử lại tới khi nhận;
  - thư mục journal dùng chung chỉ có file torn → từ chối, file giữ nguyên, không `down`;
  - staging dùng chung không rỗng → từ chối, không teardown;
  - cờ bảo trì do bên khác đặt trong lúc drill → được giữ.
- Inventory: 51/51 hash file thay đổi khớp `68351dc`. Source trước/sau review lại trùng.
- CI:
  - run `39422883314` trên `41d07d4`: **success** cả ba job;
  - run `39431941809` trên `68351dc`: web và deploy contract **success**, job API **đang chạy** lúc ghi. Chưa suy ra xanh; nên chờ job này trước khi merge.

**Giới hạn còn lại (P3, không chặn):**

- Thư mục bảo trì dùng chung với bản thật mà lúc preflight chưa có cờ thì drill vẫn chạy. Edge thật trả 503 trong thời gian drill (khoảng 1 phút), rồi teardown gỡ cờ.
- Chưa có công cụ nạp chuỗi replica làm journal local tại chỗ. Runbook đã ghi đánh đổi "restore từ backup làm mất thay đổi DB sau backup".
- Upload một PUT ≤ 5 GB, đã ghi giới hạn.
- Failure SMTP lịch sử giữ chưa rõ nguyên nhân.

**Reviewer không chạy lại:** drill/disaster/Chrome trên Docker, selection, countercheck 16. Kết quả 117/117, 16/16 và 42/42 là của agent.

**Kết luận tại `41d07d4` (2026-10-20): có hai P2 cần sửa cùng PR trước merge.** Phần lõi (đường ghi journal không đổi, replica chỉ-thêm có kiểm chain/fork, chọn chuỗi khi restore, mã hóa `age` trước khi rời host, retention giữ bản tốt, cửa sổ bảo trì luôn mở lại writer, không đọc `.env`) đúng hướng task và đã được kiểm tốt trên môi trường cô lập. Hai lỗi nằm ở công cụ vận hành: lượt diễn tập xóa journal không do nó tạo, và một người nhận alert lỗi làm im lặng các người nhận khác. [Prompt sửa hẹp](../tasks/fix-pr67-drill-teardown-alert-recipients.md).

PR OPEN, chưa merge; source **0052**, không migration. Chưa cutover; đích S3/SMTP/host thật chưa có.

## P2-1 — `ras_ops drill` xóa journal mà chính nó vừa từ chối đụng tới

`do_drill` gọi `restore()`. `restore()` thấy `RAS_JOURNAL_DIR/revocations.jsonl` đã tồn tại và dừng với `target_journal_exists`, nhưng khối `finally` của `do_drill` vẫn chạy `teardown_drill`. Hàm này `unlink` journal trong thư mục đó và mọi file `revocations.jsonl.torn-*` ([scripts/ras_ops.py:625-637](../../scripts/ras_ops.py#L625-L637), [:640-656](../../scripts/ras_ops.py#L640-L656)).

**Kịch bản:** operator tạo env diễn tập bằng cách copy env đang phục vụ, đổi `RAS_DEPLOY_PROJECT` thành `ras-pilot-drill` nhưng quên đổi `RAS_JOURNAL_DIR`. Runbook còn yêu cầu trỏ `--record-state-dir` vào state dir của deploy thật, nên việc trộn đường dẫn rất dễ xảy ra. Hậu quả:

- journal hiện hành của bản đang phục vụ bị xóa, gate đóng `journal_missing` (mất dịch vụ);
- mất các record chưa ship lên replica;
- không có công cụ nạp lại journal tại chỗ.

Cùng nhóm lỗi: drill đặt cờ bảo trì trong `RAS_MAINTENANCE_DIR` nhưng không bao giờ gỡ; nếu thư mục này dùng chung thì edge thật kẹt ở 503.

**Repro reviewer** (ngoài repo, không Docker; compose/docker thay bằng recorder, tái dùng loader của `test_m8b_ops_host`): `C:\Users\ADMIN\AppData\Local\Temp\rv67\test_rv67_drill.py`. Kết quả **1 fail**: guard trả `target_journal_exists`, sau đó file journal "đang phục vụ" không còn.

## P2-2 — người nhận alert đầu bị từ chối thì không ai nhận alert

`check` gửi lần lượt cho `alerts.recipients` và `break` ở lỗi đầu tiên. State dedupe chỉ được ghi khi không có lỗi ([api/app/cli/ops.py:303-316](../../api/app/cli/ops.py#L303-L316)).

- Người nhận đầu bị relay từ chối vĩnh viễn (sai địa chỉ, hộp thư đầy, chính sách relay): **mọi người nhận sau không bao giờ được thử gửi**, ở mọi lượt timer. Điều kiện cấu hình hai người nhận để dự phòng bị vô hiệu.
- Người nhận sau lỗi: người nhận đầu nhận lặp lại mỗi 5 phút, vì state không được ghi.

Dấu hiệu duy nhất là exit 5 trong journal systemd.

**Repro reviewer:** `C:\Users\ADMIN\AppData\Local\Temp\rv67\test_rv67_repro.py::test_one_rejected_alert_recipient_silences_the_others`. Ca này tái dùng fixture `ops_env` của PR, thay `send_email` để từ chối người nhận A. Kết quả **fail**: qua 3 lượt check chỉ có `['oncall-a', 'oncall-a', 'oncall-a']`, B không lần nào được gọi.

## P3 — không chặn merge

- **Journal local đứng sau replica khi DB không quay về.** Replicator đọc file không khóa, nên có thể ship một dòng đã `write()` nhưng chưa `fsync()`. Nếu mất điện ngay lúc đó, journal local mất dòng này trong khi replica giữ nó → `journal_behind_replica`, edge bị fence. Nếu writer chưa dừng mà ghi tiếp, kết quả thành `replica_fork` → restore `journal_sources_diverged`. Repro hàm `test_line_shipped_before_fsync_then_lost_leads_to_unresolvable_fork` pass (mô tả đúng hành vi hiện có).
  - Xác suất thấp và không làm hồi sinh dữ liệu.
  - Runbook chỉ chỉ đường "restore từ backup", làm mất thay đổi DB từ backup tới nay. Nên ghi rõ đánh đổi này, và nếu nhỏ thì thêm bước nạp chuỗi replica làm journal local khi local là prefix, rồi `recover` trên DB hiện tại.
- **Upload ciphertext bằng một `put_object`.** S3 giới hạn 5 GB mỗi PUT; dữ liệu pilot nhỏ nên chưa ảnh hưởng, ghi thành giới hạn.
- **Failure `test_recipient_reject_is_a_failure` của agent.** Reviewer chạy lại `test_m8a_smtp_delivery.py` + `test_m8a_smtp_deadline.py` 3 lần, mỗi lần 34/34 pass; chưa tái hiện và giữ là failure riêng chưa rõ nguyên nhân. Thay đổi `delivery.py` chỉ tách `_transmit`, thứ tự deadline → soạn → mật khẩu → process con tương đương.

## Kiểm chứng của reviewer

Python Conda `ras`, basetemp ngoài repo `C:\Users\ADMIN\AppData\Local\Temp\rv67`. Không Docker, không MinIO/SMTP thật, không dev/Dify/`.env`/provider.

- Test M8b `test_m8b_replica/artifacts/alerts/ops_host`: **67/67 pass**.
  - Lượt đầu dùng basetemp trong scratchpad: 65 pass / 2 fail do `WinError 206` (đường dẫn quá dài). Đây là lỗi môi trường của reviewer, không phải code; chạy lại với basetemp ngắn thì pass.
- SMTP M8a: 3 × 34/34 pass.
- Repro: **2 fail (P2-1, P2-2)**, 1 pass (mô tả P3).
- Đọc diff: replica, artifacts, s3, alerts, CLI ops, `ras_ops.py`, compose, Caddyfile, Dockerfile ops, unit systemd.
- Inventory: **49/49** hash file thay đổi khớp `41d07d4` (file thứ 50 là chính inventory). Source `api/app`, `scripts/ras_ops.py`, `deploy/` trước/sau review trùng.
- CI run `39422883314` trên `41d07d4`: web và deploy contract **success**; job API selection **đang chạy** lúc viết report. Chưa có kết quả, không suy ra xanh.

**Reviewer không chạy lại:** drill 141 kiểm, Chrome, MinIO thật, image/Docker, selection 1192, countercheck. Các số đó là kết quả của agent, không cộng thành kiểm của reviewer.
