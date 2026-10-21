# M7c — bộ paper arXiv giấy phép mở

Không có PDF hay toàn văn trong repo. Có ba thứ:

- `manifest.json`: 12 paper (ID/version arXiv, license CC BY 4.0 hoặc CC BY-SA 4.0 kiểm theo đúng version, ghi công,
  SHA-256, kích thước, trạng thái parser sản phẩm). Tải lại có kiểm license và checksum:
  `python scripts/m7c_papers.py fetch --dest <thư mục ngoài repo>`.
- `questions.json`: 36 câu hỏi do agent soạn (`authored_by: ai_agent`, chưa có người duyệt), với quote gold ngắn, trang và
  offset trên text parser production. Kiểm lại: `python scripts/m7c_papers.py parse --dest <dir> --image <API image>`,
  rồi `python scripts/m7c_papers.py questions --parsed <dir>/parsed`.
- `synthetic/`: hai ghi chú tự viết, gồm memo injection (marker `ORCHID-7731`) và ghi chú riêng của user thứ hai
  (`QUOKKA-5528`). Được upload dạng LF.

Quote ngắn trong `questions.json` lấy từ paper CC BY/CC BY-SA. Ghi công theo `manifest.json`; các paper không bị chỉnh sửa.
`.gitattributes` đặt `-text` để bytes (và SHA-256 mà record AI judge bind tới) không đổi khi checkout.
Chi tiết: [handoff M7c](../../docs/m7c-live-quality-verification.md).
