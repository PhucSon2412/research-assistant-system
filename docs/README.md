# Tài liệu dự án

Repo đã merge M3a private index (PR #6, merge commit `1c4a6f7`), M3b private search (PR #7, merge commit `5911038`), M3c collection cá nhân (PR #8, merge commit `6b0a96a`) M4a RAG một paper riêng (PR #9, merge commit `6eaaf12`) và M4b RAG theo scope (PR #10, merge commit `2977142`). M4c thêm [bộ đánh giá retrieval/RAG ban đầu](rag-evaluation.md) trong PR riêng: 80 câu nhãn agent chờ người duyệt, corpus tổng hợp, công cụ và báo cáo mẫu; streaming và đánh giá answer bằng người đọc là việc tiếp theo. PR #11 đã merge ở `99e439d`; M4d-1 (PR #12, `96e38cd`), M4d-2 (PR #13, `aef90fd`) và M4d-3 (PR #14, `2f91241`) đã merge; xem [search-and-rag](search-and-rag.md). M5a discovery arXiv metadata đã merge (PR #15, `332a9b0`; HTTP 406 live của `ListRecords` vẫn mở). M5b-1 liên kết citation arXiv chính xác có ACL đã merge (PR #16, `a03c2c5`); xem [data-model](data-model.md) và [decisions](decisions.md#quyết-định-triển-khai-m5b-1-2026-09-29). PR riêng sau đó sửa proxy web cùng origin để trình duyệt tới được `search`, `collections`, `rag` (trước đó `404` từ proxy) và đặt timeout proxy theo route (PR #17, `8265c30`); xem [decisions](decisions.md#sửa-proxy-web-cho-search-collections-và-rag-2026-09-29) và [dev-setup](dev-setup.md#proxy-web-json-search-collections-và-rag). M6a (PR #18, `a8ac982`) thêm yêu cầu duyệt paper riêng theo đúng version/file: Admin chỉ xem tạm đúng file được gửi khi yêu cầu còn mở; xem [decisions](decisions.md#quyết-định-triển-khai-m6a-2026-09-29). M6b-1 (PR riêng) thêm phê duyệt: Admin ghi quyền đã tự kiểm cho đúng file/checksum, hệ thống tạo bản chung **mới, chờ công bố** với `AccessRights` riêng. Rights đã ghi nhận chưa phải là công bố: User khác chưa thấy bản đó, chưa có index; xem [decisions](decisions.md#quyết-định-triển-khai-m6b-1-2026-09-29). M6b-1 đã merge (PR #19, `5c0f28a`). M6b-2a (PR riêng) thêm công bố **metadata** có kiểm object chung (`publication_state=listed`, vẫn `content_status=metadata_only`: chưa file gốc, search hay RAG), chống công bố trùng SHA-256, thu hồi với tombstone và cleanup xóa object + metadata mặc định (restore sau backup chưa có gate tự động); xem [decisions](decisions.md#quyết-định-triển-khai-m6b-2a-2026-09-30). M6b-2a đã merge (PR #20, `458b5dd`). M6b-2b1 (PR riêng) mở **tải file gốc** của bản chung đã công bố chỉ khi `AccessRights` còn hiệu lực cho đúng file/checksum/epoch cho phép tải và không kèm điều kiện dạng text (chưa thi hành được); API kiểm SHA-256 trước byte đầu tiên và kiểm lại quyền dưới khóa; search/RAG chung vẫn đóng; xem [decisions](decisions.md#quyết-định-triển-khai-m6b-2b1-2026-09-30). M6b-2b1 đã merge (PR #21, `87a200e`). M6b-2b2a (PR riêng) cho cleanup xóa dữ liệu dẫn xuất full text của bản chung đã thu hồi (giữ ledger chi phí embedding, không giữ vector/hash) và chặn trong DB mọi ghi muộn; chưa có ingest/index hay search/RAG chung; xem [decisions](decisions.md#quyết-định-triển-khai-m6b-2b2a-2026-09-30). Các tài liệu này mô tả yêu cầu, phương án và phép đo trên corpus tổng hợp; phần đã có code được ghi riêng ở [môi trường phát triển](dev-setup.md).

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
| [Bộ đánh giá retrieval/RAG (M4c)](rag-evaluation.md) | Dữ liệu 80 câu, manifest corpus, locator, metric, runner, phiếu người đọc, kết quả mẫu và giới hạn |
| [Roadmap](roadmap.md) | Milestone, phụ thuộc, đầu ra, rủi ro |
| [Môi trường phát triển](dev-setup.md) | Lệnh dựng/hạ Compose, migrate, bootstrap, test đã kiểm; schema M1a–M3b, auth/ACL, upload original, worker ingest và private search |
| [M2b PDF/coverage/references](m2b-pdf.md) | Parser, ngưỡng body, locator, quyền, kiểm chứng và hợp đồng bàn giao M3 |
| [Nhật ký quyết định](decisions.md) | Những điều đã chốt ngày 2026-09-24 và các câu hỏi còn mở |

Đọc [AGENTS.md](../AGENTS.md) trước khi giao task cho coding agent. Mọi mô tả “MVP” ở đây là phạm vi dự kiến, không phải trạng thái hoàn thành.
