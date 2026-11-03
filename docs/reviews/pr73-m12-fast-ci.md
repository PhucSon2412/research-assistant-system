# Review PR #73 (M12 — CI nhanh, test theo tầng) tại `89caa8a`

**Ngày:** 2026-11-02. **Base:** `959e4a4`. Review gọn theo [chính sách kiểm thử giai đoạn dùng cá nhân](../decisions.md#chính-sách-kiểm-thử-giai-đoạn-dùng-cá-nhân-2026-10-29). Orchestrator đọc diff `conftest.py` và workflow, đối chiếu danh sách run trên GitHub. Không chạy lại test.

## Kết luận

Hướng kỹ thuật đúng và đã có hiệu quả.

- **Template DB** là thay đổi đáng giá nhất. Mỗi session/worker migrate một lần vào database template, có khóa advisory, và chỉ đổi tên khi migrate xong. Mỗi session so dấu catalog và dữ liệu của bản clone với một lần `upgrade head` mới. `register_new_install` vẫn chạy cho từng test.
- **Tầng nhanh** đã chạy trên CI: shard dài nhất 15m00s (4 shard) và 17m33s (5 shard), so với 96 phút trước đây.

**Chưa merge được**, vì ba điểm dưới đây.

## Điểm chặn

1. **P2 — Lịch chạy làm cạn phút GitHub Actions.**
   - Repo đang private, nên GitHub tính phí theo **tổng số phút chạy của mọi máy**, không theo thời gian chờ. Chia shard làm ngắn thời gian chờ nhưng không giảm số phút: tầng nhanh 5–6 shard vẫn tốn khoảng 70–90 phút mỗi lần push.
   - Tầng đầy đủ tốn khoảng 820 phút mỗi lần chạy. Workflow lại chạy nó **mỗi đêm** (`cron: "23 19 * * *"`) và ở mỗi push `main`: khoảng 25.000 phút mỗi tháng, gấp hơn 10 lần hạn mức miễn phí 2.000 phút/tháng.
   - Lúc làm PR, hai run đã bị chặn vì hết hạn mức (`41005259577`, `41020635195`).
2. **P2 — Lỗi phụ thuộc thứ tự chưa giải thích, nằm ở vùng gate.**
   - Khi chạy song song, `test_publication::test_publish_refuses_missing_or_altered_common_object` fail với `service_recovering` lúc đăng nhập; chạy riêng thì pass.
   - Điều này mâu thuẫn với kết luận "không phụ thuộc thứ tự". Lỗi nằm đúng ở recovery gate, một bất biến an toàn, nên phải có nguyên nhân gốc trước khi merge. Không né bằng retry hay marker.
3. **P3 — Chưa có run nào trên cấu hình cuối.** Hai run trên cấu hình 6 shard đều bị chặn. Lọc đường dẫn cho PR chỉ sửa docs/web mới được kiểm bằng unit test.

## Ghi nhận ngoài phạm vi

Theo agent, test evaluation mất 810 s vì `app.auth_safety.cutover → verify` chạy ở **mọi request**. Nếu đúng, demo cũng chịu chi phí này cho từng request. Cần đo trên demo trước khi quyết có tối ưu hay không. Đây là vùng gate an toàn, nên nếu sửa thì làm thành task riêng, không gộp vào M12.

## Việc tiếp theo

[Prompt sửa cùng PR](../tasks/fix-pr73-ci-minutes-and-order-flake.md).

## Cập nhật 2026-11-02: repo chuyển public

Chủ dự án yêu cầu chuyển repo sang public để CI được miễn phí.

Trước khi chuyển, orchestrator quét toàn bộ 354 commit:

- **gitleaks v8.24.3**, chạy offline, có che giá trị: 1.600 phát hiện, đều là `generic-api-key` báo nhầm (tên file `auth`/`token` đi kèm SHA-256 trong inventory, cùng các dòng import).
- **grep thêm theo mẫu:** không có key OpenRouter thật, private key, key `age`, token GitHub hay AWS. Các chuỗi `sk-or-…` và mật khẩu trong test đều là giá trị giả.
- **Commit** dùng email noreply. **Email demo** chỉ xuất hiện ở dạng đã che.

Repo đã public. P2-1 đổi từ "tiết kiệm phút" sang "giữ an toàn cho workflow public và đạt thời gian chờ"; [prompt sửa](../tasks/fix-pr73-ci-minutes-and-order-flake.md) đã cập nhật theo.
