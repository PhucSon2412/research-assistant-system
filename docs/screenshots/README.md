# Ảnh demo M10

**Đã cập nhật sau sửa review PR #71:** 10 ảnh ui2 chụp bằng Chrome 155.0.8059.85 trên Windows host, font tiếng Việt
và wrapping đã kiểm lại. Ảnh trước/sau đầy đủ tại `%TEMP%\ras-pr71-fix-20261022\`;
[inventory sửa review](../m10-pr71-fix-inventory.json).

Ảnh chụp Chrome trên stack C4 cô lập; toàn bộ tài khoản/paper là dữ liệu tự viết tổng hợp.
RAG dùng `c4_stack.py fake-on`, không gọi provider thật. Không ảnh chứa Gmail thật, IP, token hay secret.
Ảnh gốc và ma trận mọi trang ở 1440/390 px giữ ngoài repo theo [handoff M10](../m10-frontend-ui.md).

| Màn hình | Desktop | Mobile |
|---|---|---|
| Đăng nhập | [1440 px](m10-login-1440.png) | — |
| Thư viện + upload/consent | [1440 px](m10-library-1440.png) | [390 px](m10-library-390.png) |
| Tìm kiếm có đoạn trích | [1440 px](m10-search-1440.png) | — |
| RAG + panel nguồn | [1440 px](m10-rag-1440.png) | [390 px](m10-rag-390.png) |
| Bộ sưu tập có 3 paper | [1440 px](m10-collections-1440.png) | — |
| Admin mời/tài khoản | [1440 px](m10-admin-1440.png) | [390 px](m10-admin-390.png) |
| Đúng chunk/vị trí nguồn | — | [390 px](m10-source-390.png) |

10 ảnh PNG gốc từ browser, không chỉnh nội dung. Ảnh thư viện mobile cuộn ngang trong bảng tới cột Kho/trạng thái.
Hai ảnh RAG dùng viewport cao để thấy cả câu trả lời và panel
bằng chứng; ảnh toàn trang vẫn được giữ ngoài repo. Mọi số lượng, trạng thái, tiêu đề, URL nguồn và câu trả lời
đều do API của stack tổng hợp trả về. Câu trả lời provider giả không phải kết quả đánh giá chất lượng nghiên cứu.
