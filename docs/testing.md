# Chiến lược kiểm thử

[Mục lục](README.md) · [Yêu cầu sản phẩm](product-requirements.md) · [Search và RAG](search-and-rag.md) · [Phi chức năng](nonfunctional.md)

Đây là kế hoạch **đề xuất**. Từ M1a đã có pytest cho API (`api/tests/`: migration trên DB trống, ràng buộc schema, bootstrap một lần/đồng thời/không lộ secret, config, health) chạy trên PostgreSQL thật; cách chạy ở [dev-setup](dev-setup.md#chạy-test-api). Mỗi milestone bổ sung test cho rủi ro mà nó tạo ra; dùng fixture nhỏ có giấy phép/nguồn rõ và dữ liệu giả cho paper riêng.

| Tầng | Ca kiểm chính | Điều kiện đạt |
| --- | --- | --- |
| Unit | Chuẩn hóa định danh, duplicate trong scope, trạng thái job, ACL, locator, parser/chunker PDF/Markdown, ngưỡng body 70%/2.000 ký tự, reference match, embedding idempotency key, budget split | Cùng input cho kết quả ổn định; no-body/không đạt ngưỡng không `indexed`, partial giữ coverage; config có test biên |
| Integration | PostgreSQL/pgvector + Redis/Celery + S3 adapter/MinIO; retry, reindex dimensions, xóa/thu hồi/tombstone, provider mock usage/cost/rerank | Không mất original đã nhận, không nhân đôi vector/edge, không lộ item khác quyền; xóa logic và index/chunk/vector ngay, file ≤ 24 giờ |
| Contract | API invitation/session/bootstrap/reset, tạo upload intent riêng rồi gửi file, job/library/search/RAG/citation; adapter arXiv/parser/OpenRouter embedding/rerank/generation; schema version và lỗi | Response/status/provenance đúng hợp đồng; provider 429/thiếu budget có trạng thái/retry phù hợp; intent sai/hết hạn/tái dùng không lưu file/tạo job |
| End-to-end | Invitation → session → reset/logout; xác nhận từng upload private → ready/partial → search/rerank/RAG → mở file/vị trí; scan bị từ chối; arXiv metadata + link → `indexed` chỉ sau rights; promotion review version → common; thu hồi/xóa/restore | Các hành trình [sản phẩm](product-requirements.md) chạy bằng UI/API với hai User và Admin |
| Load | [Workload 1.000 paper/5 user](nonfunctional.md), đợt upload/daily song song; warm/cold, một paper/collection/all | Báo p95 search, RAG token đầu/hoàn tất, PDF index không gồm queue, queue riêng, lỗi/tài nguyên/cost; so với **mục tiêu đề xuất, cần đo** |
| Recovery | Kill worker ở từng stage, mất index, restore PostgreSQL/object, replay queue, lỗi provider; restore sau delete | Idempotent, rollback index version, ACL/tombstone đúng; bản đã xóa không xuất hiện lại; đo RPO/RTO so với mục tiêu thử 24 giờ |
| Security | IDOR, bootstrap chạy lại, invite 72h/session 30m idle/8h absolute/reset token 30m, upload oversize/giả MIME, parser limit, injection, cache/citation/graph leak, link file hết hạn | 0 truy cập khác quyền; lỗi không tiết lộ item riêng; không có Admin credential mặc định; reset thu hồi session cũ |

## Bộ đánh giá retrieval/RAG

**Đề xuất bộ đánh giá ban đầu, cần đo:** Ít nhất **80 câu hỏi** do người đọc paper đối chiếu trên bộ paper có quyền dùng: một paper, so sánh nhiều paper, không đáp án, PDF partial, câu kiểm quyền; thêm references, mâu thuẫn và prompt injection. Mỗi câu ghi `allowed scope`, paper/version, span bằng chứng chuẩn (trang/dòng/offset), claim được hỗ trợ và khi nào phải `insufficient_evidence`. Có cặp User A/B cùng câu hỏi nhưng quyền khác nhau; giữ một phần holdout. Nếu dữ liệu benchmark < 1.000 paper, báo rõ độ đại diện hạn chế.

Đo retrieval bằng Recall@5 trên câu có evidence chuẩn, tách keyword/vector/fusion/rerank và kiểm hiệu quả rerank. Đo **grounded answer rate**: tỷ lệ câu trả lời có nội dung mà mọi khẳng định quan trọng được span nguồn hỗ trợ; **citation accuracy**: tỷ lệ citation trỏ đúng span chứng cứ; **abstention appropriateness**: tỷ lệ câu không đủ chứng cứ được xử lý đúng. **Mục tiêu đề xuất, cần đo:** Recall@5 ≥ 0,80; grounded answers ≥ 90%; citation accuracy ≥ 95%; abstention ≥ 90%; 0 lỗi ACL. Báo mẫu số và kết quả theo nhóm câu hỏi, tách lỗi extraction/retrieval/rerank/generation, version model và chi phí. Người đọc kiểm mẫu lỗi; không chỉ dùng LLM tự chấm. Chưa biến ngưỡng thành quality gate trước baseline.

## Ma trận chống rò rỉ

Tạo item `A-private`, `B-private`, `common`, cùng DOI/hash và chuỗi nhận diện duy nhất ở từng file. Chạy với User A, User B, Admin và phiên hết hạn; so sánh response/status/count/facet/snippet/autocomplete, download/viewer, collection, RAG prompt/context/answer/citation, graph/reference edge, job/admin log, cache trước/sau revoke. Các ca bắt buộc:

1. User A không lấy được ID/file/chunk/job của B qua endpoint trực tiếp hay tham số scope tự sửa; kết quả lỗi không xác nhận item B tồn tại.
2. Search keyword và vector đã lọc quyền **trước top-k**; private của B không làm thay đổi count/facet của A hay chiếm slot làm mất kết quả hợp lệ.
3. Collection chứa ID đã mất quyền không mở lại item; câu trả lời, citation và link viewer không trỏ đến B; kiểm payload gửi LLM không chứa chunk của B.
4. Hash/DOI trùng xuyên scope không tạo thông báo “paper đã có” tiết lộ B; promotion chỉ tạo common item sau approval và không thay đổi private item.
5. Admin chỉ xem được version private khi có promotion request mở; từ chối thu hồi quyền review ngay, và không xem được version khác. Common item sau approval có `AccessRights` độc lập, đủ nguồn file/license hoặc bằng chứng, ngày/người duyệt/phạm vi; User upload một mình không đủ quyền công bố.
6. Ghi `t_rev` tại commit thu hồi; mọi request **mới** sau commit tới file (gồm range), search, RAG, chunk, citation/viewer và cache phải bị chặn dù index/cache còn stale. Job được đánh dấu hủy và không ghi/công bố kết quả sau revoke; thử worker đang ở các stage, kể cả trước gọi provider. RAG đã chạy: chặn nếu chưa gửi provider, hủy best effort và ngừng stream khi phát hiện revoke; không có phần **mới sau khi phát hiện**. Phép thử ghi rõ giới hạn: bytes đã gửi browser hoặc chunk đã gửi provider trước revoke không thể thu hồi, token stream đã phát không thể xóa. File/dẫn xuất dọn ≤ 24 giờ; file vật lý còn phục vụ item hợp lệ khác giữ nguyên. Restore backup trong 30 ngày phải áp ACL/tombstone và không tái xuất bản nội dung đã xóa.
7. Reference entry chỉ hỗ trợ câu hỏi bibliographic; không được tính làm bằng chứng cho nội dung hoặc sự đồng ý của paper được trích.

## Kiểm thử quyết định mới

- Dùng fake provider xác nhận toàn bộ **text đã trích xuất** (không gửi file binary) được chia chunk và gửi embedding; candidate chunk đã qua ACL mới gửi rerank; câu hỏi/chunk được chọn mới gửi generation. Query dùng cùng embedding model/dimensions; retry/chunk không đổi không embed lại. UI không chọn sẵn ô xác nhận; test UI chỉ bắt đầu gửi file **sau khi** API tạo upload intent thành công. Test intent không có file, gắn đúng user/upload/notice version, một lần, có expiry; thiếu/sai owner/notice/hết hạn/tái dùng hoặc hai request đua nhau phải bị từ chối, không lưu original/item/job hoặc gọi provider; staging lỗi được dọn. Nếu client vẫn cố gửi multipart không hợp lệ, test API không lưu dữ liệu/job, **không** khẳng định bytes chưa từng tới server qua mạng.
- Kiểm usage token/units, model, provider, chi phí theo embedding/rerank/generation, budget ứng dụng $10/ngày và quota request/user; 429, hết budget, timeout/concurrency giữ trạng thái đúng, không phát sinh job/vector trùng. Live smoke test chỉ trên fixture cho phép gửi ra ngoài, dừng trước khi hết budget.
- Đổi model hoặc dimensions tạo index mới, query vẫn dùng index cũ cho tới lúc chuyển active; rollback không trộn vector khác chiều/model. So sánh retrieval evaluation trước/sau.
- ArXiv paper mặc định `metadata_only`: hiện ở trang khám phá, không có full-text search hit/count/facet hoặc RAG context/citation. Fixture giấy phép không cho đăng lại không tạo `SourceFile`/chunk kho chung. Với **đúng file version**, chỉ rights record đủ nguồn/URL/bằng chứng/ngày/người duyệt/phạm vi mới qua gate; index lỗi lần đầu vẫn `metadata_only`; active index xong mới `indexed`. Reindex lỗi giữ index cũ; revoke hạ khỏi retrieval ngay. Hugging Face để sau MVP.
- Bootstrap Admin một lần với mật khẩu input an toàn, không echo/log; invite một lần hết hạn 72 giờ; session idle 30 phút hoặc absolute 8 giờ; logout/revoke server-side; reset token một lần 30 phút và reset thu hồi session cũ.
- File giới hạn cấu hình PDF 30 MB/100 trang, Markdown 2 MB; MIME/nội dung giả bị từ chối. Kiểm PDF body: ≥ 70% trang thân đạt ≥ 100 ký tự và tổng ≥ 2.000 ký tự; không đạt thì lỗi/`needs_review`, không có keyword/vector entry hoặc RAG; PDF partial đủ ngưỡng có coverage, citation chỉ trỏ phần đã index. Dùng fixture nhiều hình/công thức để báo false rejection và điều chỉnh sau đo.

**Cần kiểm/đo:** Dataset 1.000 paper có giấy phép phù hợp, khả dụng của ba model/provider và dimensions, tiêu chuẩn accessibility, false rejection của PDF và mức đạt các mục tiêu đề xuất. Xem [decisions](decisions.md).
