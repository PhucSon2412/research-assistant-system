# Quyền và dữ liệu

[Mục lục](README.md) · [Mô hình dữ liệu](data-model.md) · [Search và RAG](search-and-rag.md)

## Nguyên tắc

**Đã xác nhận:** Có kho chung do Admin quản lý và kho riêng từ upload của User. Upload mặc định riêng tư. User hỏi đáp được trên kho chung và kho riêng của mình. MVP có Admin và User; Curator/quyền nhóm để sau. Đưa paper riêng vào kho chung cần quy trình rõ ràng.

**Đã xác nhận ngày 2026-09-24:** Tài khoản nội bộ cấp bằng lời mời; API kiểm session và quyền ở từng request. Admin đầu tiên chỉ được tạo bằng lệnh bootstrap chạy một lần với mật khẩu nhập an toàn, không có tài khoản mặc định hardcode. **Đề xuất:** Tách ba lớp: xác thực kiểm danh tính/mật khẩu, quản lý phiên cấp/thu hồi session, phân quyền tính ACL theo item/version/action. UI Next.js chỉ hiển thị trạng thái, FastAPI là nơi quyết định quyền.

**Đề xuất:** Quyền áp dụng ở cấp **library item** (một bản paper trong một kho), gồm `common` hoặc `private(owner_user_id)`. Metadata, file, chunk, embedding, reference trích từ item và kết quả search/RAG đi theo cùng phạm vi. Một paper học thuật có thể có item riêng và item chung độc lập; trùng nội dung không tự thay đổi visibility. Admin có quyền vận hành kho chung, không mặc nhiên đọc kho riêng của User trong giao diện hay API thông thường. Chính sách hỗ trợ/kiểm tra sự cố trên dữ liệu riêng cần quyết định riêng.

**Đề xuất:** Collection MVP thuộc một User và chỉ chứa item mà chủ collection có quyền; khi quyền thay đổi, membership không mở rộng quyền. Collection không phải ACL. Paper kho chung có thể được User thêm vào collection riêng.

## Ma trận quyền MVP đề xuất

| Hành động | User: item chung | User: item riêng của mình | User: item riêng người khác | Admin: item chung | Admin: item riêng User |
| --- | --- | --- | --- | --- | --- |
| Xem metadata / file gốc | Có nếu item chung có file; metadata-only chỉ mở link gốc | Có | Không | Có nếu có file | Chỉ version được User gửi review trong thời hạn request; còn lại không |
| Search / snippet / facet / count | Có | Có | Không | Có | Không qua luồng thông thường |
| RAG / citation / xem chunk | Có | Có | Không | Có | Không qua luồng thông thường |
| Sửa metadata, collection | Collection của mình; không sửa item | Có | Không | Sửa item chung | Không |
| Xem trạng thái job | Job chung đã công bố theo phần hiển thị User | Job của mình | Không | Mọi job kho chung | Chỉ số vận hành đã khử dữ liệu; trong review chỉ xem job/version được cấp |
| Xóa item | Không | Có, theo chính sách retention | Không | Có, theo chính sách retention | Không qua luồng thông thường |
| Quản lý nguồn discovery / công bố | Không | Gửi đề nghị công bố | Không | Có | Duyệt đề nghị và tạo item chung; không biến item riêng thành chung |

**Đề xuất:** Admin có thể là một User khi dùng kho riêng của chính mình. Ma trận dựa trên quan hệ sở hữu item, không dựa chỉ vào role. Xóa/thu hồi lập tức chặn quyền và loại chunk/vector/index của **item bị xóa khỏi đường phục vụ** trong luồng xử lý; việc dọn vật lý theo job riêng không được mở lại quyền. File gốc và dẫn xuất còn lại phải dọn trong 24 giờ theo cấu hình MVP. Nếu file vật lý vẫn phục vụ item hợp lệ khác, chỉ xóa liên kết/quyền của item bị xóa. Backup có thể giữ bản đã xóa tối đa theo retention 30 ngày nhưng restore phải áp tombstone trước khi phục vụ truy vấn.

## Quy trình đưa paper riêng vào kho chung

1. User tạo yêu cầu từ một version cụ thể, ghi nguồn/quyền phân phối mà họ biết. User upload không mặc nhiên chứng minh quyền phân phối. Hệ thống không công bố ngay.
2. Chỉ sau request, Admin được xem **đúng version ấy**, file, metadata và bằng chứng quyền phân phối bằng quyền review tạm có audit. Quyền review không cho tìm kiếm/RAG trên toàn kho riêng hoặc mở version khác.
3. Admin kiểm quyền với **đúng phiên bản file** sẽ lưu ở kho chung: nguồn file, URL giấy phép hoặc bằng chứng được phép, ngày kiểm tra, người duyệt và phạm vi sử dụng được phép. Paper đọc miễn phí trên arXiv/Hugging Face chưa đủ. Nếu từ chối, ghi lý do và chấm dứt ngay quyền xem tạm. Nếu chấp thuận, tạo item `common` và bản ghi `AccessRights` độc lập từ version được duyệt, chạy xác minh/index trong phạm vi chung; chấm dứt quyền review sau khi thao tác hoàn tất. Item riêng và collection của User giữ nguyên.
4. Ghi audit, provenance và chính sách thu hồi. Việc User xóa bản riêng sau đó không tự xóa bản chung; quyền phân phối/thu hồi của bản chung phải được xử lý riêng.

Daily arXiv/Hugging Face cũng dùng cùng gate quyền cho đúng version trước khi lưu/hiển thị lại full text; không xác minh được thì chỉ `metadata_only` với link gốc. Khi có yêu cầu thu hồi hoặc phát hiện quyền không hợp lệ ở item chung, thực hiện [hợp đồng thu hồi](#hợp-đồng-thu-hồi-và-xóa) dưới đây. Chỉ giữ audit tối thiểu về quyết định, không giữ nội dung đã thu hồi trong log. Item riêng của người khác xử lý độc lập; metadata/link gốc chỉ được giữ nếu chính sách nguồn còn cho phép.

Không dùng hash trùng giữa tenant để báo rằng người khác có paper nào; không trả count, lỗi, thời gian phản hồi hoặc citation tiết lộ item riêng. API, worker, search index, cache và lời gọi LLM đều phải kiểm cùng phạm vi. [Chi tiết retrieval](search-and-rag.md).

## Hợp đồng xác nhận upload riêng

**Đã xác nhận:** Trước lúc upload paper riêng, UI thông báo toàn bộ text trích xuất sẽ gửi tới OpenRouter/provider để embedding; candidate chunk có thể gửi để rerank và câu hỏi/chunk được chọn để generation. User **xác nhận tường minh cho từng upload** bằng ô không chọn sẵn. UI gọi bước tạo upload intent **không kèm file** sau xác nhận; chỉ khi bước này thành công mới bắt đầu truyền file. Một multipart request chứa cả xác nhận và file không chứng minh được yêu cầu “từ chối trước khi gửi bytes”, vì bytes có thể đã tới server trước lúc kiểm xác nhận.

**Đề xuất hợp đồng API:** `POST /uploads/intents` nhận tín hiệu xác nhận tường minh, notice version và thông tin file dự kiến, kiểm session, tạo intent ngắn hạn dùng một lần gắn với User, một lần upload và receipt gồm actor/thời điểm/notice version. Endpoint gửi file yêu cầu intent đó, kiểm owner, hiệu lực, chưa dùng, scope và thông tin file thực so với khai báo trước khi lưu original hoặc tạo item/job; claim/tiêu thụ intent nguyên tử để hai request song song không dùng lại. Intent hết hạn/hủy không tạo item/job; staging tạm nếu có được dọn. Nếu client cố gửi file không có intent hợp lệ, bytes **có thể đã đi trên mạng hoặc bị hạ tầng đệm tạm**, nhưng API không lưu original bền vững, không tạo item/job và không gọi provider; bộ đệm tạm phải được hủy. Ghi audit tối thiểu, không ghi text paper. Pipeline embedding không gửi file gốc tới provider.

## Hợp đồng thu hồi và xóa

**Đã xác nhận:** Thu hồi quyền kho chung hoặc xóa item phải lập tức chặn truy cập và dừng job; dữ liệu dẫn xuất/file được dọn theo chính sách xóa, không ảnh hưởng item hợp lệ khác. **Đề xuất định nghĩa thời điểm:** `t_rev` là lúc giao dịch ghi `revoked/deleted` và tombstone vào PostgreSQL commit. Từ `t_rev`, mọi **request mới** tới file/viewer, chunk, search, RAG, citation và link liên quan phải kiểm trạng thái này và từ chối phục vụ nội dung, kể cả khi cache/index còn bản cũ. Xóa cache/index và vô hiệu link ngay trong luồng xử lý; đường tải file đi qua API kiểm quyền theo mỗi request, không phát hành URL object dài hạn không thể thu hồi. Request range mới cũng phải bị chặn. Bản sao chung, chunk/vector và file được dọn theo thời hạn; bản vật lý dùng hợp lệ bởi item khác được giữ.

**Đề xuất xử lý công việc đang chạy:** Cùng giao dịch đặt `cancel_requested` cho job liên quan và tăng `rights_epoch`; worker kiểm tombstone/epoch tại mỗi ranh giới bước, trước mỗi lần gọi provider và trước khi ghi/công bố kết quả. Celery revoke là tín hiệu phụ, không thay thế kiểm trong worker. RAG đang chạy kiểm quyền lại trước khi gửi provider, trước khi gửi câu trả lời/citation và trước từng phần stream; khi phát hiện thu hồi, hủy provider request nếu có thể và ngừng phát phần còn lại. Không thể lấy lại bytes đã gửi tới trình duyệt hoặc nội dung đã gửi tới provider **trước `t_rev`**. Lời gọi đã qua điểm kiểm quyền trước `t_rev` có thể đang truyền khi commit xảy ra; stream có thể đã phát thêm trước khi phát hiện. Không cam kết thu hồi hồi tố hoặc đồng bộ tuyệt đối ở ranh giới mạng. Đây là giới hạn của bảo đảm “ngay lập tức” và phải có [kiểm thử](testing.md) cho ranh giới này.
