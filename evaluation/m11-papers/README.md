# Kho paper M11

[`manifest.json`](manifest.json) chỉ chứa metadata, tác giả, license đúng version, SHA-256,
kích thước và chủ đề. PDF không chỉnh sửa được lưu ngoài repo tại `%USERPROFILE%\ras-pilot\demo-papers\`.
`ATTRIBUTION.md` ở thư mục đó giữ cả ghi công 12 paper M7c và phần M11.

Lựa chọn: LLM và information retrieval, bảy chủ đề retrieval/embedding, reranking, RAG,
đánh giá, grounding/hallucination, long context/chunking, agent tìm kiếm. Chỉ CC BY 4.0,
CC BY-SA 4.0 hoặc CC0 1.0. Lô OAI `arXivRaw` (GetRecord hoặc ListRecords) phải đồng ý với
trang abs của **đúng version**, và version đó phải là latest trong OAI. Lô Atom ghi phương pháp
`atom_and_live_abs_latest`: entry ID có version; abs hiện hành và abs đúng version đều phải
xác nhận cùng latest version và cùng license mở. Thiếu lịch sử/sai ID/bất đồng đều từ chối.
HTML/XML bằng chứng ngoài repo có SHA-256, không suy diễn trường OAI từ Atom.
`from` của OAI là ngày cập nhật metadata, không phải ngày đăng bài.
Không trùng arXiv ID/SHA-256 của corpus cũ.

Tải lại manifest đã khóa (kiểm mới hai trang abs/latest và license trước tải,
rồi SHA-256/kích thước trước khi công bố file; file hỏng hiện có được giữ nguyên để kiểm):

```powershell
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m11_papers.py fetch
```

Tìm paper mới có checkpoint, request tuần tự cách nhau ít nhất 3,2 giây:

```powershell
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m11_papers.py discover --target 1300
```

Lô đầu 300 đã khóa; ngày 2026-10-24 chủ dự án xác nhận thêm 1.000 ngoài lô này,
tổng khoảng 1.300 mới, cùng ledger/trần $30/dừng $24. Manifest chỉ nối thêm paper, không
đổi 300 đầu. OAI cache theo ngày UTC vì resumption token hết hạn hằng ngày; giữ cache cũ.
Khi OAI mới timeout, có thể dùng `discover --cached-candidates --target 1300` trên snapshot
đã lưu ngoài repo; vẫn kiểm license abs đúng version, không tự chấp nhận license từ snapshot.
Hết candidate mà chưa đủ target thì exit 2 và giữ checkpoint, không coi corpus hoàn tất.

Khi OAI timeout nhưng API Atom hoạt động, dùng nguồn chính thức thay thế:

```powershell
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m11_papers.py discover --atom-candidates --atom-pages 4 --target 1300
```

`topic-exclusions.json` giữ metadata các PDF ngoài phạm vi; `download-exclusions.json`
giữ PDF quá lớn/trùng hash. File và attribution ngoài repo được giữ cả khi không upload.

Manifest không phải kết quả đánh giá M7c và không thay nhãn/gold/human verdict.

Chỉ dẫn mới hơn ngày 2026-10-24: chủ dự án nâng trần tổng lên **$50/dừng $40** và chọn
mở rộng tiếp tới ngân sách thay vì dừng ở 1.300. Client mới hỗ trợ
`scripts/m11_library.py run --watch --until-budget`: giữ $0,813 cho 15 câu thử,
index dừng admission ở $39,187 rồi drain job có receipt. PDF đã tải nhưng chưa nạp khi
tới chốt được giữ ngoài repo, có credit và metadata lý do `budget_not_uploaded` trong
`download-exclusions.json`; không xóa item/file hay giải phóng phí chưa rõ outcome.
Kết quả nạp và giới hạn nằm trong [handoff M11](../../docs/m11-demo-knowledge-base.md).
