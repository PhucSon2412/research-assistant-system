# Nhật ký quyết định

[Mục lục](README.md) · [Roadmap](roadmap.md)

Brief khởi tạo và quyết định của chủ dự án ngày **2026-09-24** là nguồn cho các mục **Đã xác nhận**. Quyết định kỹ thuật đã chốt được ghi ở đây; chi tiết triển khai còn mở vẫn là **Đề xuất** hoặc **Cần xác nhận**. Khi đổi quyết định, ghi ngày/lý do và sửa docs cùng code liên quan.

## Đã xác nhận ngày 2026-09-24

| Chủ đề | Quyết định | Hệ quả thiết kế |
| --- | --- | --- |
| Stack | Next.js web; FastAPI API; PostgreSQL + pgvector cho metadata, keyword và vector search; Redis + Celery cho job; lớp lưu file S3-compatible, MinIO local; Docker Compose cho dev và triển khai đầu | Module web/API/worker riêng; PostgreSQL là nguồn chân lý, index và job có thể xây/chạy lại; chưa có code trong repo |
| Auth | Tài khoản nội bộ, cấp bằng lời mời trong MVP, chưa SSO; API kiểm session và quyền mỗi request; Admin đầu tiên từ lệnh bootstrap một lần, mật khẩu nhận bằng input an toàn, không có tài khoản/mật khẩu mặc định hardcode | Tách xác thực, quản lý phiên và phân quyền trong API/schema/test |
| OpenRouter | OpenRouter cho embedding, rerank và RAG generation MVP. Model được chủ dự án chọn: `openai/text-embedding-3-small`, `qwen/qwen3-reranker-8b`, `openai/gpt-6-sol` | Dùng model ID trong config; kiểm khả dụng, API và kích thước vector khi tích hợp; cùng embedding model cho chunk/query; rerank chỉ trên candidate đã lọc quyền; theo dõi token/chi phí từng tác vụ |
| Hạn mức key | Chủ dự án cung cấp phản hồi API của key với `limit=50`, `limit_reset=daily`, `include_byok_in_limit=false` | Đây là **giới hạn chi tiêu $50/ngày của key**, không phải credit $50 được cấp mới mỗi ngày. Số dư/usage trong phản hồi là snapshot, không ghi làm cấu hình cố định; không lưu label, organization/workspace ID hoặc secret trong repo |
| Daily | Nguồn đầu là arXiv theo vài chủ đề AI/ML; Hugging Face có thể thêm sau. Daily trước hết lưu metadata và link gốc | Chỉ lưu/hiển thị lại full text trong kho chung khi điều kiện của **từng bài** cho phép; giấy phép mặc định arXiv không tự cấp quyền đăng lại trên dịch vụ khác |
| Promotion | User yêu cầu cho một `PaperVersion` cụ thể; Admin chỉ được xem version ấy để review, có audit; khi từ chối, quyền xem tạm kết thúc | Chấp thuận tạo item/quyền phân phối riêng ở kho chung; xóa bản cá nhân không tự phá bản chung |
| OCR | Không làm OCR trong phase hiện tại; MVP nhận PDF có phần thân trích xuất được và Markdown | PDF scan hoặc không trích xuất được phần thân bị từ chối **xử lý toàn văn**, thông báo rõ và không đánh dấu indexed; partial coverage được báo, RAG chỉ dùng phần đã index |
| SLO | Đặt workload thử ban đầu và đo baseline rồi mới chốt SLO | Các con số cũ trong docs không phải cam kết; xem [phi chức năng](nonfunctional.md) |
| Later | DOI/arXiv URL import, graph nâng cao, nhóm và format khác nằm ngoài đường triển khai MVP | Schema vẫn giữ identifier, version, source và access rights để mở rộng |

## Làm rõ từ chủ dự án ngày 2026-09-24

- Paper daily `metadata_only` vẫn hiện ở trang khám phá với metadata và link gốc, nhưng **không vào full-text search hoặc RAG**. Chỉ sau khi có quyền xử lý full text, file hợp lệ và keyword/vector index thành công mới chuyển `indexed`. Trạng thái nội dung này tách khỏi trạng thái job.
- User **phải xác nhận tường minh cho từng upload riêng** trước khi gửi file. Thông báo nêu toàn bộ text trích xuất được gửi tới OpenRouter/provider để embedding; các candidate chunk được phép có thể gửi để rerank và câu hỏi/các chunk được chọn có thể gửi để sinh câu trả lời RAG. Không xác nhận thì không bắt đầu upload/job; ô xác nhận không chọn sẵn, API kiểm và ghi version thông báo, actor, thời điểm.
- Quyền đưa toàn văn vào kho chung gắn với **đúng phiên bản file**: Admin ghi nguồn file, URL giấy phép/bằng chứng, ngày kiểm tra, người duyệt và phạm vi sử dụng. Đọc miễn phí trên arXiv/Hugging Face không tự chứng minh quyền lưu/phân phối lại. Không xác minh được thì giữ `metadata_only` và link gốc, không vào full-text search/RAG. User đề xuất công bố cũng qua kiểm tra này; việc user upload không chứng minh quyền phân phối.
- Khi quyền kho chung bị thu hồi: ngay lập tức ẩn file, chặn request mới tới search/RAG và yêu cầu hủy job; xóa bản sao chung/chunk/vector theo quy trình xóa, giữ audit quyết định tối thiểu không chứa nội dung. Giới hạn với request/job đang chạy được làm rõ dưới đây. Các item riêng của người khác độc lập. Metadata/link gốc chỉ giữ nếu vẫn phù hợp chính sách nguồn.
- **Cấu hình MVP đề xuất, cần đo:** PDF ≤ 30 MB và 100 trang, Markdown ≤ 2 MB; kiểm định dạng thực và giới hạn parser. PDF có body text trên ≥ 70% trang thân (mỗi trang đó ≥ 100 ký tự) và tổng body ≥ 2.000 ký tự để xét tự động; nếu không đạt thì lỗi hoặc `needs_review`, không `indexed`; đo lại trên PDF thật. Không OCR. Các giá trị tập trung trong config/test, không hardcode rải rác.
- **Cấu hình MVP đề xuất:** Session hết hạn sau 30 phút không hoạt động hoặc 8 giờ từ login, mốc nào trước; invite một lần trong 72 giờ; reset password bằng token một lần trong 30 phút và thu hồi session cũ. Logout/thu hồi session ở server; thời hạn trong config/test.
- **Cấu hình MVP đề xuất:** Dữ liệu giữ đến khi owner xóa hoặc quyền chung bị thu hồi. Quyền, index, chunk/vector của item bị xóa phải bị loại ngay trong luồng xử lý; file và dẫn xuất còn lại xóa trong 24 giờ, giữ file vật lý nếu item khác vẫn có quyền hợp lệ. Backup PostgreSQL/object storage ít nhất hằng ngày, giữ 30 ngày; thử restore hằng tháng, mục tiêu thử RPO/RTO 24 giờ mỗi loại. Restore phải áp nhật ký tombstone độc lập với snapshot cũ để dữ liệu đã xóa không xuất hiện lại; backup có thể còn bản đã xóa đến khi hết retention.
- Benchmark ban đầu: khoảng 1.000 paper × 20 trang, 5 user đồng thời, upload/daily song song, dùng dữ liệu có quyền phù hợp; nếu ít hơn phải ghi giới hạn. Mục tiêu **đề xuất, cần đo**: search p95 < 2 giây; RAG p95 token đầu < 10 giây và hoàn tất < 45 giây; index PDF text 20 trang p95 < 5 phút không tính queue; 0 leak ACL. Bộ đánh giá ≥ 80 câu với evidence chuẩn; mục tiêu Recall@5 ≥ 0,80, grounded answers ≥ 90%, citation đúng span ≥ 95%, xử lý thiếu bằng chứng ≥ 90%, ACL leak = 0. Chỉ chốt SLO/quality gate sau baseline.
- Ngân sách **ứng dụng thử** $10/ngày, tách ít nhất embedding và generation; giới hạn mỗi request/user, cảnh báo gần hết. **Đề xuất:** thêm bucket rerank vì model này cũng phát sinh chi phí (ví dụ chia $3 embedding/$2 rerank/$5 generation, cần đo và điều chỉnh). Đây là budget dự án, tách khỏi key cap $50/ngày và không hàm ý có credit mới mỗi ngày.
- Sau MVP ưu tiên DOI/arXiv URL import metadata/link trước; kế đến Hugging Face daily, graph nâng cao sau khi reference matching có chất lượng, rồi HTML/JATS XML, DOCX, BibTeX/RIS và LaTeX; nhóm/chia sẻ sau cùng khi ACL ổn định.

Nguồn kiểm tra: các trang model [embedding](https://openrouter.ai/openai/text-embedding-3-small), [rerank](https://openrouter.ai/qwen/qwen3-reranker-8b), [generation](https://openrouter.ai/openai/gpt-6-sol) và [rerank API](https://openrouter.ai/docs/api/api-reference/rerank/create-rerank) của OpenRouter; [arXiv bulk data](https://github.com/arXiv/arxiv-docs/blob/develop/source/help/bulk_data.md). Khả dụng, giá, provider và kích thước vector phải được kiểm lại khi tích hợp; phản hồi key do chủ dự án cung cấp là nguồn cho cấu hình `limit`/`limit_reset`, không cần gọi key từ repo.

## Làm rõ đã xác nhận ngày 2026-09-24

- Xác nhận xử lý ngoài dùng hai bước: User xác nhận riêng cho từng upload, UI tạo upload intent không có file rồi mới gửi file sau khi API chấp nhận; API kiểm intent gắn đúng upload trước khi lưu file/tạo job. Nếu client cố gửi multipart không có intent, bytes có thể đã qua mạng dù API từ chối. [Hợp đồng chi tiết](access-and-data.md#hợp-đồng-xác-nhận-upload-riêng).
- “Thu hồi ngay” áp dụng cho request **mới** từ khi giao dịch thu hồi commit (`t_rev`); cache/index/link không được vượt qua chốt quyền. Job được đánh dấu hủy, worker kiểm ở các điểm an toàn; RAG đang chạy dừng phát tiếp khi phát hiện thu hồi. Bytes đã giao cho client và nội dung đã gửi provider trước thời điểm đó không thể thu hồi. [Giới hạn và thử nghiệm](access-and-data.md#hợp-đồng-thu-hồi-và-xóa).
- M1 chia thành ba PR kiểm chứng độc lập: nền dữ liệu + bootstrap, sau đó invite/session/ACL, sau đó upload intent + lưu original. Cuối M1 vẫn demo hai User. [Roadmap](roadmap.md).

## Cần xác nhận hoặc đo trước phần phụ thuộc

| Ưu tiên | Vấn đề còn mở | Hướng làm việc hiện tại | Cần trước |
| --- | --- | --- | --- |
| 1 | OpenRouter routes/model availability, số chiều vector thực và provider retention có phù hợp không? | Giữ ba model ID trong config; smoke test bằng fixture có quyền, kiểm model/dimensions và privacy trước dữ liệu thật | M3–M4 |
| 1 | Ngưỡng PDF 70%/2.000 ký tự loại oan bao nhiêu paper nhiều hình/công thức? | Dùng làm ngưỡng thử, kiểm fixture thật; trường hợp chưa rõ vào `needs_review`, không `indexed` | M2 |
| 2 | SLO và quality gate thực sau benchmark? | Dùng workload, budget và mục tiêu **đề xuất, cần đo** ở [phi chức năng](nonfunctional.md) và [kiểm thử](testing.md); báo baseline theo nhóm lỗi | M3–M6 |
| 2 | Chính sách nguồn cho việc giữ metadata/link sau thu hồi và cách xử lý file đồng sở hữu vật lý? | Quyền theo item/version; audit tối thiểu, tombstone trước cleanup; không ảnh hưởng item riêng khác | M5–M6 |
| 2 | Kênh chuyển invite/reset link ở triển khai đầu là gì? | Delivery adapter; dev outbox có kiểm soát, không log token/mật khẩu; chốt kênh thật trước triển khai cho user | M1 trước triển khai ngoài dev |

## Quyết định đã chốt trong brief

- Có hai đường nhận paper: daily discovery và user upload, dùng chung pipeline xử lý.
- Kho chung do Admin quản lý; user upload mặc định riêng tư; User hỏi đáp trên kho chung và kho riêng của mình; MVP có Admin/User.
- PDF/Markdown là định dạng MVP; giữ file gốc, báo phần không trích xuất được, không coi metadata-only là toàn văn.
- Search hybrid; RAG có nguồn đến vị trí cụ thể và có thể từ chối khi thiếu bằng chứng; index toàn văn không đồng nghĩa đưa cả paper vào một prompt.
- References và quan hệ citation phục vụ khám phá; citation không hàm ý đồng ý.
- Không thêm tính năng AI tự động tóm tắt paper nếu không có yêu cầu mới.

Những mục từ brief này vẫn giữ hiệu lực cùng quyết định ngày 2026-09-24. Khi phát hiện xung đột docs/code, ghi rõ vấn đề trước khi sửa; cập nhật quyết định cùng thay đổi.
