# Review PR #70 — M7c live, paper thật, AI judge

## Review lại tại `e73ec96` (code sửa `be44a03`) — P2 đã sửa

**Kết luận (2026-10-22): không thấy điểm chặn merge mới.**

- **CI:** run `39724086744` (`be44a03`) và `39730087653` (`e73ec96`) **success** cả ba job.
- **Dependency:** CI cài bằng `pip install -c api/constraints-rc2.txt` (`pypdf==6.21.1`). Conda `ras` hiện là 6.21.1.
- **Dataset:** `evaluation/rag/questions.json` chỉ đổi đúng 15 dòng phiên bản parser `6.19.0 → 6.21.1`, 0 dòng nội dung nhãn đổi.
- **Reviewer chạy:**
  - `python -m app.evaluation validate`: 80 câu/16 holdout/15 paper OK;
  - hai test từng fail trên CI: 2/2 pass;
  - `human-validate` cả hai phiếu: exit 0, 80 + 4 dòng pending, 0 dòng đã duyệt (M7b vẫn 0 phiếu thật).
- **Demo `ras-pilot`:** healthy; `RAS_GENERATION__MAX_OUTPUT_TOKENS=1024`; provider ba route bật; edge trên IP LAN hiện tại.
- **Ghi nhận:**
  - Ledger live dừng ở mốc 80 % (320/400); chấm thêm cần trần mới.
  - Danh sách hai nhãn M4c bị AI judge đánh "cần sửa" đổi giữa hai lượt (CMP-09/RCI-09 → PP-05/RCI-09). Coi là dao động của judge, giữ nguyên nhãn.

Phần dưới là review lần đầu tại `1d740e2`, giữ làm lịch sử.

**Kết luận tại `1d740e2` (2026-10-22): có một P2 chặn merge.**

- CI GitHub run `39605520554`: job API **fail**, 2 failed / 1.281 passed. Job deploy contract và web pass.
- Handoff của agent chỉ báo một failure SMTP khi chạy local, không báo kết quả GitHub.

[Prompt sửa hẹp](../tasks/fix-pr70-parser-dependency-drift.md). PR OPEN, chưa merge; source **0052**.

## P2 — dependency trôi: parser PDF của image/CI khác môi trường test và nhãn

Hai test fail trong `tests/test_evaluation.py`, lần đầu vào CI ở PR này:

- `test_committed_dataset_is_valid_and_pending_review`
- `test_pdf_gold_spans_match_extraction_and_unextracted_pages`

Cả hai báo cùng một lỗi: nhãn ghi `labelled with parser pypdf-6.19.0-layout-v2, extraction now uses pypdf-6.21.1-layout-v2`.

Reviewer kiểm:

| Môi trường | pypdf |
|---|---|
| Conda `ras` (chạy toàn bộ pytest và gắn nhãn evaluation) | **6.19.0** |
| Image **rc2** (`rasm8c-api:rc2`, demo `ras-pilot` đang chạy) | **6.21.1** |
| Runner CI (`pip install -e ./api[dev]`, `pyproject` chỉ ghi `pypdf>=5,<7`) | 6.21.1 |
| Inventory M8c (`pip freeze` của image) | `pypdf==6.21.1` |

**Hậu quả:**

- Nhãn M4c, kiểm span PDF và các lượt hồi quy chạy trên host (gồm full suite 5.579 của M8c) đều kiểm parser **6.19.0**. Demo và các bản build mới chạy **6.21.1**.
- CI bây giờ đỏ.
- Binding parser của bundle người đọc M7b và record AI judge M4c gắn với 6.19.0. Đổi parser thì report coi là stale/`source_mismatch` (đúng thiết kế binding), nên kết quả AI judge nhãn M4c phải được xác lập lại trên parser thật.

## Đã kiểm (reviewer)

- **Diff:** runtime chỉ thêm `app/evaluation/ai_review.py`; không đổi API/web/migration/`deploy/`.
  - Guard AI judge từ chối ghi vào thư mục record người đọc và từ chối trường `human_*`/`reviewer`/`verdict`.
  - CI thêm `test_evaluation.py` + hai file M7c; harness test đọc digest PostgreSQL/Redis/MinIO từ `deploy/compose.yaml` (đóng P3-1/P3-2 của PR #68).
- **Không lộ key:** 6 chuỗi dạng `sk-or-…` trong repo đều là giá trị test giả (`sk-or-v1-synthetic…`, mồi `sk-or-env-…`); không có địa chỉ Gmail. Reviewer chỉ in tiền tố đã che.
- **Demo `ras-pilot`:** 10 container chạy/healthy; image `ras-pilot-api:rc2`; `pricing_verified=true` ba route; dòng key có trong env ngoài repo (không đọc giá trị).
- **Không chạy lại:** live, judge, test M7c, countercheck. Số liệu live/judge là kết quả agent.

## P3 / quyết định orchestrator (không chặn)

- **So sánh nhiều paper 1/6** (tiêu đề mặc định là tên file, keyword không gom đủ hai paper): chủ dự án sửa tiêu đề sau upload; kịch bản demo dùng chế độ **hybrid** cho câu so sánh. Không đổi code trong PR này.
- **`max_output_tokens=512` cắt câu trả lời dài:** orchestrator chọn nâng lên **1024 chỉ trong env demo** (cấu hình, không đổi code/`deploy/`), ghi chi phí ước tính.
- **Hai nhãn M4c AI judge đánh "cần sửa":** giữ nguyên nhãn, chỉ ghi nhận; không để AI tự sửa dữ liệu đánh giá.
- **Tài khoản OpenRouter:** chủ dự án không quản lý tài khoản nhóm, nên cài đặt logging/retention vẫn "không xác minh được". Giữ ràng buộc chỉ nội dung giấy phép mở/tự viết.
