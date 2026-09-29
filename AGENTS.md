# Hướng dẫn agent

Đọc [mục lục docs](docs/README.md) và phần tài liệu liên quan trước mỗi task. Brief của chủ dự án và các mục **Đã xác nhận** trong [yêu cầu sản phẩm](docs/product-requirements.md), [quyền và dữ liệu](docs/access-and-data.md), [nhật ký quyết định](docs/decisions.md) là nguồn yêu cầu. Stack, ba model ID hiện chọn, key spending cap và xác nhận upload đã được chủ dự án nêu; khả dụng/provider/số chiều phải kiểm khi tích hợp. Ngưỡng hiệu năng/chất lượng là **mục tiêu đề xuất, cần đo**, chưa là SLO/gate. Các mục **Đề xuất** không tự trở thành yêu cầu bắt buộc.

## Bất biến sản phẩm và an toàn dữ liệu

- Upload của User mặc định riêng tư; trùng paper hoặc chạy lại ingest không tự công bố vào kho chung.
- Lọc quyền **trước retrieval**; kiểm lại khi mở file/chunk/citation. Không đưa nội dung paper không được phép vào prompt hoặc payload gửi LLM.
- Mọi khẳng định dựa trên paper trong câu trả lời cần nguồn mở được đến version/file và vị trí; khi thiếu bằng chứng, nêu rõ thiếu. Reference list/citation edge không chứng minh nội dung hoặc sự đồng ý của paper được trích.
- Giữ file gốc và nêu phần không trích xuất được; index toàn văn không có nghĩa gửi cả paper vào một prompt.
- Trước **mỗi upload riêng**, báo rõ text trích xuất/candidate chunk/câu hỏi sẽ được xử lý qua OpenRouter/provider ở các bước embedding/rerank/RAG, và yêu cầu User xác nhận tường minh; tạo upload intent không kèm file rồi UI mới gửi file, API kiểm intent trước khi lưu file/tạo job. Không OCR; PDF không đạt ngưỡng body cấu hình không được `indexed`.
- Daily arXiv `metadata_only` hiện ở trang khám phá, không vào full-text search/RAG; chỉ thành `indexed` sau khi quyền **đúng file version** và keyword/vector index thành công. Admin chỉ xem version riêng khi User gửi yêu cầu review version đó; từ chối thì thu hồi quyền tạm. User upload không tự chứng minh quyền phân phối.
- Khi xóa/thu hồi item, chặn **request mới** tới file/search/RAG dù cache/index cũ; đánh dấu hủy job và kiểm quyền trước gọi ngoài/công bố. RAG đang chạy dừng phát khi phát hiện thu hồi; dữ liệu đã gửi client/provider không thể lấy lại. Dọn index/chunk/vector và file theo [hợp đồng thu hồi](docs/access-and-data.md#hợp-đồng-thu-hồi-và-xóa), không ảnh hưởng item khác còn quyền; restore phải áp tombstone.
- Auth MVP dùng lời mời và session; bootstrap Admin một lần với mật khẩu nhập an toàn, không hardcode tài khoản mặc định. Embedding document/query phải dùng cùng model active, lưu model ID/số chiều/phiên bản và không gọi lại cho chunk không đổi khi retry.
- Giới hạn file, ngưỡng PDF, thời hạn auth/cleanup/backup và budget phải nằm trong config có test; không hardcode rải rác. $50/ngày là cap của OpenRouter key, $10/ngày là budget thử của ứng dụng, không phải credit được cấp mới.
- Không bổ sung tính năng AI tự động tóm tắt paper nếu chưa có yêu cầu mới.

## Quy ước commit

- Không tự thêm model AI, công cụ AI hoặc trợ lý AI vào tác giả, committer hay trailer `Co-authored-by` của commit. Không thêm trailer này chỉ vì AI đã hỗ trợ viết code hoặc vì công cụ gợi ý tự động.
- Chỉ ghi người đồng tác giả khi chủ dự án yêu cầu rõ và cung cấp thông tin cần ghi.

## Khi làm task

1. Đọc docs liên quan; nêu phạm vi, giả định và quyết định còn mở ảnh hưởng task.
2. Triển khai theo phạm vi đã chốt, thêm kiểm thử phù hợp rủi ro và kiểm bất biến quyền/dẫn nguồn khi liên quan.
3. Nếu docs và code khác nhau, kiểm tra hành vi thực tế, báo mâu thuẫn và lý do; cập nhật quyết định cùng thay đổi code/docs, không âm thầm coi code hoặc đề xuất là đúng.
4. Báo file thay đổi, kết quả kiểm thử, giới hạn và phần chưa hoàn thành.

## Môi trường Python khi phát triển

Nếu task cần chạy Python hoặc cài thư viện Python, dùng môi trường Conda hiện có tại `C:\Users\ADMIN\.conda\envs\ras`. Không tạo hoặc cài vào `venv` mới. Kiểm tra interpreter và công cụ cài đặt đang trỏ đúng môi trường này trước khi chạy/cài; không giả định dự án bắt buộc dùng Python.

## Lệnh đã kiểm chứng (M1a, M1b)

Chi tiết, bản PowerShell/Git Bash và giới hạn kiểm chứng ở [môi trường phát triển](docs/dev-setup.md). Chạy từ gốc repo; Windows PowerShell 5.1 không có `&&`. Python host: `$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"` (PowerShell) hoặc `PY="$USERPROFILE/.conda/envs/ras/python.exe"` (Git Bash).

- Chuẩn bị: `Copy-Item .env.example .env` (PowerShell) / `cp .env.example .env` (Git Bash), rồi điền mọi secret trống; không commit `.env`.
- Dựng: `docker compose up -d --build --wait` (service `migrate` tự chạy `alembic upgrade head`); hạ: `docker compose down` (giữ dữ liệu) hoặc `docker compose down -v` (xóa volume).
- Migration: `docker compose run --rm migrate`; revision hiện tại: `docker compose exec -T api alembic current`.
- Bootstrap Admin một lần: `docker compose exec api python -m app.cli.bootstrap_admin --email <email>` (hỏi mật khẩu, không echo). Cách `--password-stdin` khác nhau giữa PowerShell và Git Bash (PowerShell không có `<`, cần `$OutputEncoding` UTF-8): xem [dev-setup](docs/dev-setup.md#bootstrap-admin-đầu-tiên).
- Test API (cần `docker compose up -d --wait db`): `& $py -m pytest api -q` / `"$PY" -m pytest api -q`; cài lần đầu `& $py -m pip install -e ".\api[dev]"` / `"$PY" -m pip install -e "./api[dev]"`.
- Web: `npm ci --prefix web`, `npm run typecheck --prefix web`, `npm run build --prefix web`.
- Kết nối DB luôn qua `RAS_DB__*` → `Settings.sqlalchemy_url()`; không ghép mật khẩu vào chuỗi URL.
- Auth M1b trên stack dev: đăng nhập `http://127.0.0.1:3000/login`; Admin mời User ở `/admin`, link mời/reset nằm ở mục Dev outbox (bộ nhớ API, chỉ `RAS_ENVIRONMENT=development`). Item metadata để thử ACL thêm bằng fixture SQL qua `docker compose exec -T db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB'` (không bọc biến trong nháy kép ở PowerShell 5.1): xem [dev-setup](docs/dev-setup.md#xác-thực-phiên-và-quyền-m1b).
- Mọi route API mới phải lấy principal qua `app/api/deps.py` (session + CSRF) và lọc dữ liệu bằng `app/authz.py` trong SQL trước phân trang/đếm; item trái quyền trả `404` như không tồn tại. Test API dùng fixture `api` (đồng hồ giả, dev outbox) trong `api/tests/conftest.py`.
- Admin chỉ đọc dữ liệu private của User qua `review_grant_clause` trong `app/authz.py` (M6a, chỉ route `/admin/review-requests/*`, đúng version/file của request còn mở); không thêm nhánh Admin vào route file/library/search/RAG/collection thông thường.
- Item chung phải ghi rõ `publication_state` khi insert (`listed` cho fixture metadata như trước M6b-1; DB từ chối nếu thiếu). Bản `pending` (M6b-1: rights do Admin ghi, chưa công bố) chỉ đọc qua `pending_publication_clause` ở `/admin/pending-publications`; `visible_items_clause` và mọi route thường không thấy nó. Rights đã ghi nhận không cho mở file/chunk/search/RAG.
- M6b-2a: chỉ `/admin/common-copies/*` (qua `review_copy_admin_clause` và `app/publication.py`) chuyển bản chung `pending → listed` hoặc `→ revoked`. `listed` là metadata cho mọi tài khoản, **không** phải `indexed`: file/chunk/search/RAG của bản chung vẫn chỉ đi gate private owner cho tới PR mở nội dung theo `permits_*`. Thu hồi luôn ghi `deletion_tombstones` (không xóa được) và tăng `rights_epoch`; cleanup (`app.cli.cleanup_revoked`, beat `ras.revocation.cleanup`) phải mở rộng khi bản chung có dữ liệu dẫn xuất. Cleanup mặc định xóa metadata của bản đã thu hồi (`RevokedMetadataField`); chỉ giữ trường có hàng `revocation_metadata_retentions` cho đúng bản đó. `tombstones apply` là bước thủ công, chưa có gate sau restore: không mô tả restore là an toàn.

Schema thay đổi qua revision Alembic mới, không sửa revision đã merge; giữ test `test_orm_models_match_migrated_schema` xanh.
