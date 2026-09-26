# Tài liệu dự án

Repo đã merge M2b PDF extraction/coverage/references (PR #5, merge commit `f5483c8`). M3a private index đang ở nhánh review; search/RAG thuộc các PR sau. Các tài liệu này mô tả yêu cầu và phương án đề xuất, không khẳng định tính năng đã được triển khai hay hiệu năng đã được đo; phần đã có code được ghi riêng ở [môi trường phát triển](dev-setup.md).

## Cách đọc trạng thái quyết định

- **Đã xác nhận**: yêu cầu trong brief khởi tạo và các quyết định tiếp theo do chủ dự án nêu, ghi ở [nhật ký quyết định](decisions.md).
- **Đề xuất**: phương án thiết kế để có thể chia việc và đánh giá; chưa phải quyết định ràng buộc.
- **Cần xác nhận**: lựa chọn có tác động đáng kể cần chủ dự án quyết định trước phần coding phụ thuộc vào nó.

Khi có thay đổi quyết định, cập nhật tài liệu liên quan cùng thay đổi code; ghi kết luận ở [nhật ký quyết định](decisions.md).

## Mục lục

| Tài liệu | Nội dung |
| --- | --- |
| [Yêu cầu sản phẩm](product-requirements.md) | Người dùng, hành trình, MVP/Later, yêu cầu và tiêu chí chấp nhận |
| [Quyền và dữ liệu](access-and-data.md) | Sở hữu, visibility, ma trận quyền, đưa paper vào kho chung |
| [Kiến trúc đề xuất](architecture.md) | Thành phần, ingest, job, chống trùng, retry, reindex |
| [Mô hình dữ liệu](data-model.md) | Entity và quan hệ, nguồn dữ liệu, ràng buộc truy cập |
| [Search và RAG](search-and-rag.md) | Retrieval, kiểm quyền, dẫn nguồn, xử lý thiếu bằng chứng |
| [Yêu cầu phi chức năng](nonfunctional.md) | Cách đo hiệu suất, độ tin cậy, an toàn, vận hành |
| [Chiến lược kiểm thử](testing.md) | Các tầng test, bộ đánh giá retrieval/RAG, chống rò rỉ |
| [Roadmap](roadmap.md) | Milestone, phụ thuộc, đầu ra, rủi ro |
| [Môi trường phát triển](dev-setup.md) | Lệnh dựng/hạ Compose, migrate, bootstrap, test đã kiểm; schema M1a–M3a, auth/ACL, upload original và worker ingest |
| [M2b PDF/coverage/references](m2b-pdf.md) | Parser, ngưỡng body, locator, quyền, kiểm chứng và hợp đồng bàn giao M3 |
| [Nhật ký quyết định](decisions.md) | Những điều đã chốt ngày 2026-09-24 và các câu hỏi còn mở |

Đọc [AGENTS.md](../AGENTS.md) trước khi giao task cho coding agent. Mọi mô tả “MVP” ở đây là phạm vi dự kiến, không phải trạng thái hoàn thành.
