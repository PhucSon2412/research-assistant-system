# Sửa PR #67 — teardown diễn tập và gửi alert tới mọi người nhận

Bạn là coding agent của PR #67 (`feat/m8b-pilot-operations`, head review `41d07d4`). Sửa **trên cùng PR** hai P2 trong [report review](../reviews/pr67-m8b-pilot-operations.md). Không mở milestone mới, không đổi đường ghi journal/gate/readiness/schema, không đổi lựa chọn replica/backup/mã hóa đã được review.

Giữ report này và các docs reviewer/orchestrator đang modified/untracked; đưa chúng vào PR. Không đọc `.env`/key, không đụng dev `ras`/Dify, không gọi provider, không gửi mail thật, không đổi `pricing_verified`, không `taskkill` theo tên process.

## P2-1 — diễn tập chỉ dọn những gì chính nó tạo

- `ras_ops drill` không bao giờ được xóa hoặc ghi đè journal, cờ bảo trì, state hay staging đã tồn tại trước lượt chạy.
  - Kiểm điều kiện target **trước mọi side effect** (journal đã có, cờ bảo trì đã bật, thư mục trùng với deploy được nêu ở `--record-state-dir` nếu so được).
  - Từ chối thì thoát ngay, không teardown.
- Teardown chỉ xóa journal/torn file/cờ bảo trì mà lượt này đã tạo. Chọn cách xác định quyền sở hữu đơn giản, có test, ví dụ ghi nhận việc restore của lượt này đã tạo journal.
  - Lượt drill thành công phải gỡ cờ bảo trì do chính nó đặt.
- `down -v` giữ guard label/project hiện có.
- Regression bắt buộc:
  - repro của reviewer (env drill trỏ vào thư mục journal đang phục vụ → journal còn nguyên, exit có mã ổn định);
  - drill thành công không để lại cờ bảo trì;
  - drill lỗi giữa chừng sau khi đã tạo journal của chính nó vẫn dọn sạch.
- Cập nhật runbook diễn tập tháng: env drill phải dùng thư mục journal/state/staging/maintenance riêng; công cụ từ chối khi trùng.

## P2-2 — gửi alert độc lập cho từng người nhận

- Thử gửi cho **mọi** người nhận trong mỗi lượt check; lỗi ở một người không chặn người khác.
- Dedupe/nhắc lại/`RESOLVED` theo từng người nhận, để người đã nhận không bị gửi lặp và người lỗi được thử lại ở lượt sau.
- Có lỗi gửi ở bất kỳ người nhận nào → exit khác 0, summary nêu số đã gửi/số lỗi bằng mã. Nội dung mail vẫn chỉ mã/số/thời điểm.
- Regression:
  - người nhận A bị từ chối, B nhận được, lượt sau B không bị gửi lặp, A được thử lại;
  - B lỗi thì A không nhận lặp mỗi lượt;
  - mail `RESOLVED` tới đúng người đã được báo.

## P3 — làm nếu nhỏ, không bắt buộc

- Runbook `journal_rollback`: ghi rõ restore từ backup làm mất thay đổi DB sau backup. Nếu nhỏ, thêm bước nạp chuỗi replica làm journal local **khi local là prefix**, rồi `recover` trên DB hiện tại (dừng writer trước, không ghép chuỗi phân kỳ).
- Ghi giới hạn upload một PUT (≤ 5 GB) trong handoff.

## Kiểm chứng và bàn giao

- Chạy regression mới trên bản trước sửa (`41d07d4`) để thấy fail, sau sửa pass.
- Chạy lại test M8b liên quan (`test_m8b_ops_host`, `test_m8b_alerts`, và file khác nếu chạm) cùng countercheck nhắm hai sửa này. Không chạy lại toàn bộ diễn tập nếu không đổi đường restore/backup; nếu đổi đường restore/drill thì chạy lại phase `drill` (và `disaster` nếu chạm `restore()`) trên project cô lập.
- Ghi kết quả vào mục "Sửa review PR #67" trong handoff/inventory, giữ failure/rerun riêng.
- Push cùng PR, không merge, giữ author/committer, không AI co-author/trailer/promo.
- Ghi kết quả CI run trên commit sửa khi có; không tự nhận xanh khi run chưa xong.
