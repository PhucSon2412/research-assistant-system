# Bộ duyệt chất lượng M7b — hướng dẫn người đọc

Bộ này đọc offline: mở `index.html` bằng trình duyệt, không cần đăng nhập hay mạng.
Có hai phiếu riêng, **không trộn mẫu số**:

1. `label-review.csv` — duyệt 80 nhãn M4c (corpus tổng hợp 15 paper). Một dòng một câu.
2. `answer-review.csv` — chấm câu trả lời live M7a đã lưu (corpus tổng hợp khác, 2 tài liệu).
   Một dòng một citation; response không có claim có một dòng.

Mở phiếu bằng bảng tính hoặc trình soạn thảo; lưu lại dạng **CSV UTF-8**, giữ nguyên tên cột,
số dòng và mọi cột không phải cột điền. Ngày ghi dạng `YYYY-MM-DD` (đặt cột ngày là Text nếu
bảng tính tự đổi định dạng). Ô bắt đầu bằng `'` là cách phiếu chặn bảng tính chạy công thức;
giữ nguyên dấu đó.

## Phiếu nhãn (`label-review.csv`)

Mở trang của câu (cột `open_page`), đọc câu hỏi, kỳ vọng, từng side evidence với quote, vị trí
và đoạn text parser production trích ra; non-evidence và lý do; supported claim, rationale.
Liên kết "bản đọc" mở đúng dòng/trang; "file gốc" mở file trong bundle.

Điền bốn cột:

- `verdict`: `accepted` (câu hỏi, kỳ vọng, evidence/quote/vị trí, non-evidence và claim đều đúng
  và đủ), `changes_requested` (cần sửa, ghi chỗ cần sửa trong `notes`) hoặc `rejected` (không
  dùng được, ghi lý do trong `notes`).
- `reviewer`: tên người duyệt. `review_date`: ngày duyệt. `notes`: bắt buộc khi không accepted.

Cột `agent_note` chỉ là điểm agent đề nghị chú ý, không phải phán đoán. Dòng để trống là
**chưa duyệt**; được phép nộp phiếu chỉ điền một phần.

## Phiếu answer (`answer-review.csv`)

Mở trang response (cột `open_page`): câu hỏi, scope, trạng thái quan sát, mọi claim/citation,
quote và đoạn text nguồn từ parser, model/provider/generation ID. Cột `machine_*` là kiểm cấu
trúc (quote có nguyên văn trong vùng được ghi, file nào), **không** chứng minh claim đúng.
`not_available` nghĩa là run không lưu đủ dữ liệu để kiểm (ví dụ evidence pack, gold label).

- `human_claim_supported` (`yes`/`partial`/`no`): đoạn được dẫn có hỗ trợ đầy đủ claim không.
  Câu so sánh phải có chứng cứ cho đủ các phía; mâu thuẫn giữa nguồn phải được nêu; reference
  list hay đoạn prompt injection không phải chứng cứ. Mọi dòng của cùng claim phải cùng giá trị.
- `human_citation_correct` (`yes`/`no`): citation trỏ đúng tài liệu/vị trí chứa nội dung hỗ trợ.
- `human_abstention_appropriate`: chỉ cho câu kỳ vọng không trả lời (`yes`/`no`); câu kỳ vọng
  có đáp án để trống hoặc `n/a`.
- `reviewer`, `review_date`, `notes`.

Mẫu Chrome chỉ có text màn hình nên được hiển thị để tham khảo, không có dòng chấm.

## Nhập và tính kết quả (từ gốc repo, PowerShell, Conda `ras`)

```powershell
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py -m app.evaluation human-validate labels evaluation\human-review\bundle\label-review.csv
& $py -m app.evaluation human-import labels evaluation\human-review\bundle\label-review.csv
& $py -m app.evaluation human-validate answers evaluation\human-review\bundle\answer-review.csv
& $py -m app.evaluation human-import answers evaluation\human-review\bundle\answer-review.csv
& $py -m app.evaluation human-report
```

`human-validate` chỉ kiểm, không ghi. `human-import` từ chối toàn bộ phiếu nếu có lỗi (dòng
thiếu/trùng/lạ, sửa nội dung, thiếu reviewer/ngày/verdict, phán đoán mâu thuẫn, bundle cũ) và
không ghi một phần. `human-report` ghi `evaluation/human-review/report.md` và `.json`.
Tên reviewer là do người điền tự ghi, không phải danh tính đã xác thực.
