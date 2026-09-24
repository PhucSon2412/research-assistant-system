# Nhật ký quyết định

[Mục lục](README.md) · [Roadmap](roadmap.md)

Brief khởi tạo và quyết định của chủ dự án ngày **2026-09-24** là nguồn cho các mục **Đã xác nhận**. Quyết định kỹ thuật đã chốt được ghi ở đây; chi tiết triển khai còn mở vẫn là **Đề xuất** hoặc **Cần xác nhận**. Khi đổi quyết định, ghi ngày/lý do và sửa docs cùng code liên quan.

## Đã xác nhận ngày 2026-09-24

| Chủ đề | Quyết định | Hệ quả thiết kế |
| --- | --- | --- |
| Stack | Next.js web; FastAPI API; PostgreSQL + pgvector cho metadata, keyword và vector search; Redis + Celery cho job; lớp lưu file S3-compatible, MinIO local; Docker Compose cho dev và triển khai đầu | Module web/API/worker riêng; PostgreSQL là nguồn chân lý, index và job có thể xây/chạy lại; nền Compose/migration/bootstrap có từ M1a ([dev-setup](dev-setup.md)) |
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

## Ghi nhận khi triển khai M1a (2026-09-24)

Các lựa chọn dưới đây là **quyết định kỹ thuật của PR M1a, đã được chủ dự án review và merge vào `main` (PR #1)**; lý do và tác động tới M1b/M1c ở [dev-setup](dev-setup.md#lựa-chọn-kỹ-thuật-ảnh-hưởng-m1bm1c).

- Migration: Alembic + SQLAlchemy 2 + psycopg 3; trạng thái/loại dạng `text` + `CHECK` thay cho PostgreSQL ENUM. Password verifier: Argon2id (argon2-cffi), DB chỉ nhận chuỗi `$argon2id$`. Bootstrap: advisory lock + hàng singleton `bootstrap_state`, từ chối khi đã có bất kỳ user; mật khẩu chỉ qua prompt không echo hoặc `--password-stdin`, tối thiểu 12 ký tự (cấu hình `RAS_PASSWORD__MIN_LENGTH`, sàn 8) — độ dài tối thiểu là **đề xuất** của agent, chưa có trong brief.
- Bất biến đưa xuống DB: visibility/owner của `LibraryItem` không đổi (promotion tạo item chung mới); scope của `IngestionJob` phải trùng scope item và upload luôn private; `SourceFile` bất biến; `AuditEvent` append-only. Mọi FK `ON DELETE RESTRICT` để xóa đi qua tombstone/cleanup (M6).
- **Mâu thuẫn wording đã xử lý:** bảng roadmap ghi M1a có “migration nền cho … rights/intent”, trong khi mục “Task đầu tiên” của roadmap và yêu cầu giao việc M1a ghi “các khóa cần mở rộng cho … acknowledgement/intent, `AccessRights`” và “chỉ triển khai phần schema cần thiết ở M1a”. PR M1a theo cách hiểu thứ hai: **chưa tạo** bảng `AccessRights`, `ProcessingAcknowledgement`, `UploadIntent`, invite/session/reset; chỉ chuẩn bị khóa nối (`(paper_versions.id, item_id)`, `(source_files.id, version_id)`, checksum, `users.status/password_changed_at`, `rights_epoch`, `cancel_requested`). Các bảng đó thuộc M1b/M1c/M5–M6.
- Kết nối DB (sửa sau review M1a): Compose truyền `RAS_DB__HOST/PORT/USER/PASSWORD/NAME` riêng, ứng dụng dựng URL bằng `URL.create`; `RAS_DATABASE_URL` chỉ nhận URL đã percent-encode. Lý do: bản đầu ghép `POSTGRES_PASSWORD` vào URL nên mật khẩu có `@` làm sai host (orchestrator tái hiện). API, Alembic, bootstrap và test cùng đi qua `Settings.sqlalchemy_url()`.
- **Bàn giao cho M1c:** `source_files.job_id` và `version_id` là hai FK độc lập. Trước khi mở đường tải file, M1c phải ràng buộc job và version cùng item (ví dụ FK kép qua `item_id` hoặc trigger như `ingestion_jobs`) và test trường hợp khác User/scope.
- Docker Hub không còn phục vụ `minio/minio`; Compose dùng `quay.io/minio/minio` pin tag `RELEASE.2025-09-07T16-13-09Z`. Cần xem lại nguồn/phiên bản MinIO (hoặc S3-compatible thay thế) trước triển khai ngoài dev.

## Ghi nhận khi triển khai M1b (2026-09-24)

Các lựa chọn dưới đây là **quyết định kỹ thuật của PR M1b (PR #2), đang chờ chủ dự án review, chưa merge**; chi tiết vận hành ở [dev-setup](dev-setup.md#xác-thực-phiên-và-quyền-m1b). Chúng hiện thực các mục **Đề xuất** về tách xác thực/phiên/phân quyền và ma trận quyền metadata; không mở rộng yêu cầu sản phẩm.

- **Token:** invitation, reset, session và CSRF đều là 256 bit từ `secrets`; DB chỉ lưu SHA-256 hex (có `CHECK` định dạng), không lưu token thô. Dùng hash nhanh vì đầu vào là ngẫu nhiên entropy cao, không phải mật khẩu, và cần tra cứu có index. Link đặt token ở fragment `#token=` để trình duyệt không gửi token lên web/API và không vào access log.
- **Dùng một lần/đồng thời:** accept invitation và confirm reset khóa dòng (`SELECT … FOR UPDATE`) rồi kiểm lại điều kiện còn hiệu lực; request thua cuộc nhận `invalid_token`. Partial unique index giữ tối đa một invitation mở mỗi email và một reset token mở mỗi user; invite/reset mới thu hồi cái cũ.
- **Session server-side:** bảng `sessions` (`last_activity_at`, `absolute_expires_at`, `revoked_at/reason`). Mỗi request kiểm và cập nhật hoạt động trong một câu `UPDATE … RETURNING`: hợp lệ khi chưa thu hồi, user `active`, `now < last_activity_at + idle` và `now < absolute_expires_at`; đúng mốc biên là hết hạn. Thời hạn lấy từ `Settings.auth` (idle đọc ở thời điểm request, absolute chốt khi login). Thời gian quyết định bằng đồng hồ ứng dụng (`app/clock.py`), để test biên chính xác.
- **Reset/disable thu hồi phiên:** cùng giao dịch đổi verifier (hoặc `status=disabled`) thu hồi mọi session và reset token mở. Login khóa dòng user và kiểm lại verifier/status trước khi tạo session, nên login đang chạy song song với reset không giữ được phiên dùng mật khẩu cũ (có test). `users.password_changed_at` được cập nhật nhưng **không** dùng để so thời điểm, vì so hai đồng hồ (ứng dụng và DB) dễ sai.
- **Cookie/CSRF:** session cookie HttpOnly, `SameSite=Lax`, `Path=/`; khi `RAS_HTTP__COOKIE_SECURE=true` thêm `Secure` và tên `__Host-ras_session`. Thao tác ghi cần header `X-CSRF-Token` bằng cookie `ras_csrf` (double submit), token phải khớp hash lưu trong session khi đã đăng nhập, và `Origin` (nếu có) phải thuộc `RAS_HTTP__ALLOWED_ORIGINS`. `RAS_ENVIRONMENT` mặc định `production`: bắt buộc cookie Secure và cấm dev outbox. `docker-compose.yml` đặt cứng `development`, cookie không Secure và `dev_outbox`: đây là cấu hình **chỉ cho dev**, không phải cấu hình triển khai; repo chưa có cấu hình HTTPS/production nào được kiểm chứng.
- **Web:** Next.js gọi API qua route proxy cùng origin (`/api/*` → `API_INTERNAL_URL`, chỉ tiền tố `auth`, `admin`, `library`), chỉ chuyển tiếp header/cookie, không quyết định quyền.
- **Không lộ tài khoản:** login sai email, sai mật khẩu hay tài khoản bị vô hiệu hóa đều trả `401 invalid_credentials` sau cùng lượng việc Argon2 (verify với verifier giả khi không có user). Yêu cầu reset luôn trả `202` với body giống nhau; gửi link chạy sau response. **Giới hạn:** nhánh email tồn tại còn làm thêm việc DB (khóa dòng, thu hồi token cũ, ghi token/audit), nên chưa chứng minh không phân biệt được qua thời gian phản hồi (timing enumeration); chưa đo. Lỗi validation không trả lại giá trị đã gửi. Audit login thất bại không ghi email đã nhập.
- **Delivery:** interface `DeliveryAdapter`; `disabled` (mặc định; từ chối tạo invitation, reset vẫn trả chung) và `dev_outbox` (chỉ `development`): link giữ trong bộ nhớ tiến trình API, giới hạn số lượng, bỏ khi token hết hạn, chỉ Admin đọc qua `GET /admin/dev-outbox` (có audit). Không ghi DB/log. Giới hạn: mất khi restart, không chia sẻ giữa nhiều process, và Admin xem được link reset của User — vì vậy chỉ dành cho dev.
- **Vai trò:** invitation không có cột/trường vai trò; accept luôn tạo `user`, trường `role` gửi kèm bị bỏ qua. Admin không tự vô hiệu hóa chính mình.
- **ACL metadata:** `app/authz.py` dựng điều kiện `common OR owner = principal` đưa vào SQL trước `ORDER/LIMIT/OFFSET/COUNT`; `scope=all|common|mine` chỉ thu hẹp. Truy cập theo ID áp lại cùng điều kiện; item riêng trái quyền, ID không tồn tại và ID sai định dạng đều trả `404 not_found` giống nhau. Admin xem/sửa item chung và item riêng của chính mình, không đọc item riêng của User khác; User sửa item riêng của mình, sửa item chung bị `403`.
- **Mâu thuẫn test M1a đã xử lý:** `test_m1a_exposes_no_auth_or_upload_routes` khẳng định không có route auth — đúng với M1a nhưng trái phạm vi M1b; đổi thành kiểm vẫn chưa có route upload/file. Các test M1a ghim revision `0001` nay dùng `HEAD_REVISION`.

**Quyết định còn mở từ M1b** (thêm vào bảng dưới): cấp vai trò Admin qua lời mời/nâng quyền; kênh gửi link thật; giới hạn tần suất/khóa tạm login và reset; chính sách session đồng thời tối đa, dọn session hết hạn.

**Điều kiện trước khi triển khai ngoài dev** (chưa đáp ứng ở M1b): cấu hình triển khai riêng đã kiểm chứng với HTTPS và cookie `Secure`; không dùng `dev_outbox`; kênh gửi invite/reset thật; rate limit/khóa tạm cho login và reset; đo và xử lý timing enumeration của yêu cầu reset. Chi tiết ở [dev-setup](dev-setup.md#trước-khi-đưa-lên-server).

**Bàn giao cho M1c từ M1b:** proxy web `/api/*` (`web/app/api/[...path]/route.ts`) đọc toàn bộ request/response vào bộ nhớ và có timeout 15 giây, không phù hợp để truyền PDF tới 30 MB; M1c cần thiết kế đường upload riêng (ví dụ stream hoặc gửi thẳng tới API/lưu trữ sau khi có intent) thay vì dùng nguyên proxy này. Trước khi mở download, vẫn phải ràng buộc `source_files.job_id` và `version_id` cùng item (mục M1a ở trên).

## Cần xác nhận hoặc đo trước phần phụ thuộc

| Ưu tiên | Vấn đề còn mở | Hướng làm việc hiện tại | Cần trước |
| --- | --- | --- | --- |
| 1 | OpenRouter routes/model availability, số chiều vector thực và provider retention có phù hợp không? | Giữ ba model ID trong config; smoke test bằng fixture có quyền, kiểm model/dimensions và privacy trước dữ liệu thật | M3–M4 |
| 1 | Ngưỡng PDF 70%/2.000 ký tự loại oan bao nhiêu paper nhiều hình/công thức? | Dùng làm ngưỡng thử, kiểm fixture thật; trường hợp chưa rõ vào `needs_review`, không `indexed` | M2 |
| 2 | SLO và quality gate thực sau benchmark? | Dùng workload, budget và mục tiêu **đề xuất, cần đo** ở [phi chức năng](nonfunctional.md) và [kiểm thử](testing.md); báo baseline theo nhóm lỗi | M3–M6 |
| 2 | Chính sách nguồn cho việc giữ metadata/link sau thu hồi và cách xử lý file đồng sở hữu vật lý? | Quyền theo item/version; audit tối thiểu, tombstone trước cleanup; không ảnh hưởng item riêng khác | M5–M6 |
| 2 | Kênh chuyển invite/reset link ở triển khai đầu là gì? | M1b có `DeliveryAdapter`, mặc định `disabled`; `dev_outbox` trong bộ nhớ chỉ ở `development`, không ghi DB/log; chốt kênh thật trước triển khai cho user | M1 trước triển khai ngoài dev |
| 2 | Có cấp vai trò Admin cho người thứ hai không, bằng cách nào? | M1b: lời mời chỉ tạo `user`, không có đường tự nâng quyền; chỉ Admin bootstrap là Admin | Khi cần Admin thứ hai |
| 2 | Giới hạn tần suất và khóa tạm cho login/reset? | M1b chưa có rate limit/lockout; phản hồi không lộ tài khoản, Argon2 làm chậm dò mật khẩu | Trước triển khai ngoài dev |
| 2 | Yêu cầu reset có lộ tài khoản qua thời gian phản hồi không? | Status/body đã giống nhau; nhánh email tồn tại làm thêm việc DB, chưa đo; cần đo và cân bằng (ví dụ xử lý nền cả hai nhánh) | Trước triển khai ngoài dev |
| 2 | Cấu hình triển khai HTTPS/production? | Chưa có; `docker-compose.yml` chỉ cho dev. Cần cấu hình riêng đã kiểm chứng: HTTPS, cookie `Secure`, không `dev_outbox`, kênh gửi thật, rate limit | Trước triển khai ngoài dev |
| 3 | Số session đồng thời tối đa, dọn session/token hết hạn? | Không giới hạn số session; hàng hết hạn/thu hồi giữ lại (không còn hiệu lực) cho tới job dọn dẹp sau này | M6 |

## Quyết định đã chốt trong brief

- Có hai đường nhận paper: daily discovery và user upload, dùng chung pipeline xử lý.
- Kho chung do Admin quản lý; user upload mặc định riêng tư; User hỏi đáp trên kho chung và kho riêng của mình; MVP có Admin/User.
- PDF/Markdown là định dạng MVP; giữ file gốc, báo phần không trích xuất được, không coi metadata-only là toàn văn.
- Search hybrid; RAG có nguồn đến vị trí cụ thể và có thể từ chối khi thiếu bằng chứng; index toàn văn không đồng nghĩa đưa cả paper vào một prompt.
- References và quan hệ citation phục vụ khám phá; citation không hàm ý đồng ý.
- Không thêm tính năng AI tự động tóm tắt paper nếu không có yêu cầu mới.

Những mục từ brief này vẫn giữ hiệu lực cùng quyết định ngày 2026-09-24. Khi phát hiện xung đột docs/code, ghi rõ vấn đề trước khi sửa; cập nhật quyết định cùng thay đổi.
