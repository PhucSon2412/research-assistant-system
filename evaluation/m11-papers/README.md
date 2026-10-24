# Kho paper M11

[`manifest.json`](manifest.json) chỉ chứa metadata, tác giả, license đúng version, SHA-256,
kích thước và chủ đề. PDF không chỉnh sửa được lưu ngoài repo tại `%USERPROFILE%\ras-pilot\demo-papers\`.
`ATTRIBUTION.md` ở thư mục đó giữ cả ghi công 12 paper M7c và phần M11.

Lựa chọn: LLM và information retrieval, bảy chủ đề retrieval/embedding, reranking, RAG,
đánh giá, grounding/hallucination, long context/chunking, agent tìm kiếm. Chỉ CC BY 4.0,
CC BY-SA 4.0 hoặc CC0 1.0. OAI `arXivRaw` (GetRecord hoặc ListRecords) phải đồng ý với
trang abs của **đúng version**, và version đó phải là latest trong OAI. `from` của OAI
là ngày cập nhật metadata, không phải ngày đăng bài. Không trùng arXiv ID/SHA-256 của corpus cũ.

Tải lại manifest đã khóa (kiểm lại license trước tải, rồi SHA-256/kích thước):

```powershell
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m11_papers.py fetch
```

Tìm paper mới có checkpoint, request tuần tự cách nhau ít nhất 3,2 giây:

```powershell
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m11_papers.py discover --target 1300
```

Lô đầu 300 đã khóa; ngày 2026-10-24 chủ dự án yêu cầu thêm khoảng 1.000 (đang triển khai
tổng khoảng 1.300 mới, cùng ledger/trần $30/dừng $24). Manifest chỉ nối thêm paper, không
đổi 300 đầu. OAI cache theo ngày UTC vì resumption token hết hạn hằng ngày; giữ cache cũ.
Khi OAI mới timeout, có thể dùng `discover --cached-candidates --target 1300` trên snapshot
đã lưu ngoài repo; vẫn kiểm license abs đúng version, không tự chấp nhận license từ snapshot.
Hết candidate mà chưa đủ target thì exit 2 và giữ checkpoint, không coi corpus hoàn tất.

Manifest không phải kết quả đánh giá M7c và không thay nhãn/gold/human verdict.
Kết quả nạp và giới hạn nằm trong [handoff M11](../../docs/m11-demo-knowledge-base.md).
