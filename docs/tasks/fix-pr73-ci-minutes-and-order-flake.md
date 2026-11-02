# Sửa PR #73 (M12): phút Actions, lỗi phụ thuộc thứ tự, run xác nhận

Làm tiếp trên nhánh `chore/m12-fast-ci` (HEAD `89caa8a`), trong cùng PR. Đọc [review](../reviews/pr73-m12-fast-ci.md) trước.

## 1. Lịch CI sau khi repo chuyển public

**Cập nhật 2026-11-02:** chủ dự án đã chuyển repo sang **public**. Orchestrator quét gitleaks toàn lịch sử trước khi chuyển: không có secret thật. Runner chuẩn của GitHub giờ miễn phí, số phút không còn là ràng buộc. Billing tháng mới cũng đã reset.

Workflow chạy trên repo public, nên:

- Không thêm secret.
- Không dùng `pull_request_target`.
- Giữ quyền `contents: read`.
- Giữ bước quét JUnit trước khi upload, vì log và artifact giờ ai cũng xem được.

Lịch và số shard:

- **Tầng nhanh trên PR:** mục tiêu thời gian chờ ≤ 10–15 phút. Tăng số shard nếu cần.
- **Tầng đầy đủ** chạy ở push `main` và `workflow_dispatch`:
  - chọn số shard sao cho thời gian chờ ≤ 45 phút;
  - tính cả giới hạn 20 job chạy đồng thời của tài khoản free, gồm cả job web/deploy;
  - mỗi shard có timeout hợp lý.
- **Nightly:** chỉ giữ nếu có bước bỏ qua khi commit đã có run đầy đủ thành công (logic hiện có). Nếu không chắc logic này đúng, bỏ nightly.
- PR chỉ sửa docs không chạy job API/web. PR chỉ sửa web chỉ chạy job web. Push mới trên PR hủy run cũ của cùng PR.
- Thêm lệnh tầng đầy đủ chạy trên máy này (harness, 8 worker) vào `docs/dev-setup.md`, dùng khi cần kiểm trước push. Không động tới demo `ras-pilot`, dev `ras`, Dify.
- Không tạo self-hosted runner. Trên repo public, self-hosted runner có thể chạy code PR lạ trên máy chủ dự án.

## 2. Lỗi `service_recovering` khi chạy song song

`test_publication::test_publish_refuses_missing_or_altered_common_object` fail khi chạy chung với test khác, pass khi chạy riêng file.

1. Tìm thứ tự gây lỗi: lấy danh sách test đã chạy trước nó trên cùng worker (log xdist hoặc `--ras-shuffle` cùng seed), rồi thu hẹp tới cặp nhỏ nhất tái hiện được.
2. Tìm trạng thái bị rò giữa các test. Nghi vấn:
   - biến môi trường hoặc cache `get_settings` (đường dẫn journal);
   - recovery gate hay app được cache ở cấp process/module;
   - database template hay engine dùng chung;
   - một hold không được đóng.
3. Sửa nguyên nhân trong fixture/harness. Nếu nguyên nhân nằm trong **code ứng dụng**, dừng và báo kèm bằng chứng; không tự sửa gate trong PR này.
4. Thêm test hồi quy tái hiện đúng thứ tự đó.
5. Chạy lại tầng đầy đủ trên máy này, ít nhất hai lượt với seed khác nhau. Không retry, không gắn marker để né.

## 3. Run xác nhận

- Có **một run PR tầng nhanh pass** và **một run tầng đầy đủ pass** (`workflow_dispatch`) trên cấu hình cuối, ghi thời gian chờ của shard dài nhất.
- Kiểm thật lọc đường dẫn bằng một commit chỉ sửa docs: run API/web phải bị bỏ qua.
- Nếu CI vẫn bị chặn, báo nguyên văn thông báo lỗi và dừng, không tự trigger lặp.

## Báo cáo

Báo bằng bảng ngắn:

- thời gian chờ và tổng phút của run PR;
- lệnh tầng đầy đủ local cùng thời gian đo;
- nguyên nhân gốc của lỗi thứ tự và test hồi quy;
- trạng thái CI thật.

Không merge. Giữ author/committer, không AI co-author/trailer/promo.
