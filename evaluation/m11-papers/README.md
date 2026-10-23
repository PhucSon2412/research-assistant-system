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
& "$env:USERPROFILE\.conda\envs\ras\python.exe" scripts/m11_papers.py discover --target 300
```

Manifest không phải kết quả đánh giá M7c và không thay nhãn/gold/human verdict.
Kết quả nạp và giới hạn nằm trong [handoff M11](../../docs/m11-demo-knowledge-base.md).
