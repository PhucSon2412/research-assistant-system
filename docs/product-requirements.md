# Yêu cầu sản phẩm

[Mục lục](README.md) · [Quyền và dữ liệu](access-and-data.md) · [Roadmap](roadmap.md)

## Mục tiêu và đối tượng

**Đã xác nhận:** Xây website để lưu và quản lý paper khoa học, khám phá paper mới định kỳ, tìm kiếm và hỏi đáp bằng RAG có dẫn đến vị trí nguồn cụ thể. Không kế thừa thiết kế của bot cũ. Không có tính năng AI tự động tóm tắt paper.

**Đề xuất:** Đối tượng MVP gồm người nghiên cứu (User) quản lý tài liệu riêng và hỏi đáp trên nội dung mình được phép xem, cùng Admin quản lý kho chung, nguồn discovery và vận hành ingest. Không giả định nhóm nghiên cứu hay Curator ở MVP.

## Hành trình chính

1. **Daily discovery:** Admin cấu hình chủ đề arXiv; lịch chạy lưu metadata và link gốc, kiểm trùng. Paper `metadata_only` **vẫn hiện ở trang khám phá**, với nhãn chỉ có metadata và link gốc; không vào full-text search hoặc RAG. Chỉ khi quyền của từng bài cho phép lưu/hiển thị lại full text, file được nhận và xử lý qua pipeline chung với upload. Sau khi index thành công mới đổi thành `indexed`. Admin xem nguồn, quyền sử dụng, trạng thái và lỗi; Hugging Face có thể bổ sung sau.
2. **User upload:** Trước **mỗi** upload riêng, User thấy thông báo rằng toàn bộ text trích xuất sẽ gửi tới OpenRouter/provider để embedding; candidate chunk được phép có thể gửi để rerank, còn câu hỏi và các chunk được chọn có thể gửi để sinh câu trả lời RAG. User xác nhận tường minh, UI tạo upload intent **không chứa file** và chỉ sau khi API chấp nhận intent mới bắt đầu gửi file. API kiểm intent gắn với đúng User/lần upload trước khi lưu file hoặc tạo job. User tải PDF có body text trích xuất được hoặc Markdown; item mặc định riêng tư. User xem tiến trình, coverage/lỗi, metadata, file gốc và vị trí nội dung được index; có thể sửa metadata và sắp xếp vào collection. PDF scan/body không đạt ngưỡng không được đánh dấu indexed.
3. **Tìm kiếm:** User chọn kho chung, kho riêng hoặc cả hai; tìm bằng từ khóa và ngữ nghĩa; kết quả chỉ gồm paper trong phạm vi có quyền, thể hiện trạng thái toàn văn và vị trí khớp.
4. **Hỏi đáp:** User chọn một paper, collection hoặc toàn bộ phạm vi được phép; hệ thống truy hồi các đoạn có quyền, trả lời với nguồn cụ thể, hoặc nói không đủ bằng chứng. Có thể mở đúng file và vị trí được dẫn.
5. **Khám phá citation:** User xem references trích xuất, liên kết paper đã biết và duyệt mạng citation; cạnh chỉ diễn đạt quan hệ trích dẫn, không phải sự tán thành.
6. **Đưa lên kho chung:** User đề nghị chia sẻ **một version cụ thể**; chỉ lúc ấy Admin có quyền xem tạm đúng version để kiểm tra quyền phân phối đối với chính file đó. User upload không là bằng chứng quyền phân phối. Từ chối thì hết quyền xem tạm; phê duyệt tạo bản chung có quyền phân phối riêng, không đổi bản riêng của User.

## Phạm vi

| MVP dự kiến — **đề xuất** | Later — **đề xuất** |
| --- | --- |
| Tài khoản Admin/User; kho chung và kho riêng; PDF/Markdown; giữ file gốc; metadata, collection, trạng thái xử lý; daily discovery có cấu hình; tìm kiếm hybrid; RAG có citation và từ chối khi thiếu bằng chứng; references và liên kết cơ bản; quy trình duyệt vào kho chung | Curator, nhóm và chia sẻ theo nhóm; HTML/XML, DOCX, BibTeX/RIS, LaTeX; DOI/arXiv URL để nhập nếu thực sự lấy được toàn văn; nhận diện cấu trúc/citation nâng cao; xếp hạng theo đồ thị; tích hợp ngoài |

**Đã xác nhận:** PDF có body text trích xuất được và Markdown là định dạng toàn văn MVP; không làm OCR trong phase hiện tại. DOI/arXiv URL import, graph nâng cao, nhóm và format khác để Later. BibTeX/RIS chỉ chứa metadata, không được coi là file toàn văn. Schema vẫn chuẩn bị identifier, version, source và access rights. Các chức năng MVP triển khai theo [roadmap](roadmap.md).

**Đề xuất cấu hình MVP, cần đo:** PDF tối đa 30 MB/100 trang, Markdown tối đa 2 MB. Kiểm định dạng thực ở backend, giới hạn thời gian/tài nguyên parser. PDF chỉ tự động được xét full text khi ít nhất 70% số trang thân có ≥ 100 ký tự text/trang và tổng phần thân ≥ 2.000 ký tự; không tính bìa, header hay references. Không đạt thì báo lỗi hoặc `needs_review`, không `indexed`; đánh giá ngưỡng trên PDF thật để tránh loại oan. Các giá trị đặt trong config và test, không hardcode rải rác.

## Yêu cầu chức năng và tiêu chí chấp nhận

| ID | Yêu cầu | Tiêu chí chấp nhận dự kiến |
| --- | --- | --- |
| F01 | Ingest full text từ discovery đủ quyền và upload đi qua cùng pipeline sau khâu nhận đầu vào | Cùng một định dạng cho ra metadata, cấu trúc, chunk và trạng thái tương đương; trace giữ `origin`; daily chưa đủ quyền dừng ở `metadata_only` và vẫn hiện trong khám phá |
| F02 | Bảo vệ file gốc và báo thiếu toàn văn | Có thể tải lại file gốc đã nhận khi có quyền; partial có coverage và locator lỗi; PDF không đạt ngưỡng thử hoặc cần review không được full-text index; metadata-only không mang nhãn đã index |
| F03 | Metadata, collection và quản trị | User quản lý item/collection riêng; Admin quản lý kho chung, nguồn, job và hàng đợi chia sẻ; mọi thao tác ghi audit |
| F04 | Tìm kiếm từ khóa + ngữ nghĩa | Chỉ paper `indexed` trong phạm vi quyền vào full-text search và RAG; `metadata_only` chỉ hiện ở trang khám phá, không làm thay đổi result/count/facet của full-text search; kết quả có snippet/locator |
| F05 | RAG theo một paper, collection hoặc toàn phạm vi | Mỗi khẳng định dựa trên paper có citation mở được đến version/file và vị trí; câu hỏi không có bằng chứng đủ nhận phản hồi nêu thiếu bằng chứng |
| F06 | References và citation graph | Reference giữ raw text/provenance; chỉ liên kết khi match đáng tin; không diễn giải cạnh citation thành đồng ý |
| F07 | Chống trùng và retry | Upload lặp trong cùng phạm vi được nhận diện; retry an toàn, không tạo thêm item/chunk/edge ngoài ý muốn; thông báo kết quả cho người dùng |
| F08 | Quyền riêng tư mặc định và công bố có kiểm soát | Upload là private trước và sau xử lý; chỉ Admin có thể công bố bản vào kho chung sau yêu cầu/kiểm tra; kiểm thử quyền theo [ma trận](access-and-data.md) |
| F09 | Provider và chi phí | Thông báo xử lý ngoài và xác nhận tường minh mỗi upload; UI chỉ gửi file sau khi nhận upload intent, API không lưu file/tạo job nếu intent thiếu, sai hoặc hết hiệu lực; embedding/rerank/generation dùng model đã chọn; retry không embed lại chunk không đổi; đo token/chi phí, giới hạn concurrency, request/user và budget ứng dụng |
| F10 | Tài khoản và phiên | Chỉ user được mời tạo tài khoản; Admin đầu tiên do bootstrap một lần với mật khẩu input an toàn; API kiểm session và ACL mỗi request; session idle/absolute timeout, logout/revoke, invite một lần và reset password bằng token một lần theo cấu hình |
| F11 | Ranh giới discovery và index | Paper daily giữ `metadata_only` trong khi chờ quyền, file hoặc index; chỉ chuyển `indexed` khi có full text được phép xử lý và active keyword/vector index thành công; thất bại vẫn chỉ hiện ở khám phá cùng lỗi phù hợp |
| F12 | Thu hồi và xóa | Từ lúc giao dịch thu hồi/xóa quyền commit, request **mới** không được phục vụ file/search/chunk/RAG của item dù cache/index chưa dọn; job nhận tín hiệu hủy và không được công bố dữ liệu mới; request đang chạy dừng phát tiếp khi phát hiện thu hồi theo điểm kiểm quyền; dữ liệu đã gửi ra trước đó không thể lấy lại; dọn file và dẫn xuất trong thời hạn cấu hình, không xóa bản vật lý còn được item hợp lệ khác dùng; restore không làm dữ liệu đã xóa xuất hiện lại |

**Đề xuất:** Metadata được phép chỉnh sửa phải giữ giá trị trích xuất gốc và người sửa; chỉ file có quyền truy cập và nội dung trích xuất thực tế mới được dùng làm bằng chứng. Tiêu chí chi tiết và phương pháp thử ở [testing](testing.md).

**Đã xác nhận:** Xác nhận tường minh mỗi upload và quy trình xét quyền/thu hồi kho chung đã được chủ dự án chốt. Model ID được chọn cho thiết kế hiện tại; khả dụng và số chiều vector cần kiểm khi tích hợp. Mọi ngưỡng số và chất lượng là **mục tiêu đề xuất, cần đo**; xem [decisions](decisions.md).
