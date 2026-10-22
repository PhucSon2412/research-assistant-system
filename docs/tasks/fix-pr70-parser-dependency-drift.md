# Sửa PR #70 — thống nhất dependency giữa image, CI và môi trường test

Bạn là coding agent của PR #70 (`feat/m7c-live-quality-verification`, head review `1d740e2`). Sửa **trên cùng PR** P2 trong [report](../reviews/pr70-m7c-live-quality-verification.md): CI đỏ vì pypdf trôi.

| Môi trường | pypdf |
|---|---|
| Image rc2 / demo / CI | 6.21.1 |
| Conda `ras` / nhãn evaluation | 6.19.0 |

Giữ mọi docs reviewer/orchestrator modified/untracked và đưa vào PR. Không đọc `.env`; key chỉ từ `%USERPROFILE%\ras-pilot\secrets\openrouter_api_key`. Không đổi image rc2, `deploy/`, migration hay code runtime ngoài phạm vi dưới. Không hạ/xóa demo. Không `taskkill` theo tên process.

## 1. Một nguồn dependency: theo image đang chạy

Orchestrator chọn chuẩn là **dependency của image rc2**, vì demo đang chạy parser đó.

- Tạo file constraints từ `pip freeze` của `rasm8c-api:rc2`, chạy không mạng, chỉ các gói Python: ví dụ `api/constraints-rc2.txt`.
- CI cài bằng `pip install -c api/constraints-rc2.txt -e "./api[dev]"`. Gói chỉ có ở `[dev]` mà image không có thì để range như cũ, ghi rõ.
- Cập nhật Conda `ras` theo cùng constraints (cài vào đúng env `ras`, không tạo venv). Ghi lệnh vào [dev-setup](../dev-setup.md).
- Không nới `pyproject`. Ghi trong handoff: build image sau này phải dùng constraints này hoặc digest đã nghiệm thu.

## 2. Xác lập lại dữ liệu đánh giá trên pypdf 6.21.1

- Chạy kiểm span của bộ M4c và bộ paper M7c bằng parser 6.21.1. Báo riêng:
  - (a) text/span khớp, chỉ khác chuỗi phiên bản;
  - (b) text/offset/trang thay đổi.
- (a): cập nhật binding phiên bản parser của dataset theo cơ chế hiện có, không sửa nội dung nhãn.
- (b): sửa gold span theo text mới, có bằng chứng từng câu; không đổi kỳ vọng nghiệp vụ.
- Dựng lại bundle người đọc M7b (`human-prepare`). Hiện 0 phiếu thật, không mất gì; `human-validate` phải sạch.
- Record AI judge bị stale do binding đổi thì chạy lại judge cho đúng phần stale, trong **trần còn lại của task** (tổng ledger vẫn ≤ $5/400 call, dừng ở 80 %). Report AI judge ghi rõ parser đã dùng.
- Ghi rõ lượt live M7c (stack C4) dùng pypdf nào và kết quả có còn hiệu lực không.

## 3. Cấu hình demo (quyết định orchestrator)

- Trong env demo ngoài repo: nâng giới hạn output generation từ 512 lên **1024** bằng biến cấu hình hiện có (không đổi code/`deploy/`), recreate đúng service cần thiết theo runbook M9, kiểm readiness. Ghi chi phí ước tính mỗi câu.
- Cập nhật kịch bản demo trong handoff M9: câu so sánh dùng chế độ **hybrid**; nhắc sửa tiêu đề paper sau upload; nhắc chỉ upload tài liệu giấy phép mở/tự viết.
- Hai nhãn M4c AI judge đánh "cần sửa": **không sửa**, chỉ ghi nhận.

## 4. Kiểm chứng và bàn giao

- Chạy lại `test_evaluation.py`, `test_m7c_*`, `test_human_review*` và các file test parser/ingest PDF dưới constraints mới. Ghi pass/fail/not-run.
- **Đợi CI GitHub trên commit sửa chạy xong và báo đúng kết quả** (run ID, từng job); không thay bằng kết quả local.
- Cập nhật handoff M7c (mục "Sửa review PR #70") và inventory. Push cùng PR, không merge, giữ author/committer, không AI co-author/trailer/promo.
