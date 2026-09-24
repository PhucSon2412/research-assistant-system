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

## Khi làm task

1. Đọc docs liên quan; nêu phạm vi, giả định và quyết định còn mở ảnh hưởng task.
2. Triển khai theo phạm vi đã chốt, thêm kiểm thử phù hợp rủi ro và kiểm bất biến quyền/dẫn nguồn khi liên quan.
3. Nếu docs và code khác nhau, kiểm tra hành vi thực tế, báo mâu thuẫn và lý do; cập nhật quyết định cùng thay đổi code/docs, không âm thầm coi code hoặc đề xuất là đúng.
4. Báo file thay đổi, kết quả kiểm thử, giới hạn và phần chưa hoàn thành.

## Môi trường Python khi phát triển

Nếu task cần chạy Python hoặc cài thư viện Python, dùng môi trường Conda hiện có tại `C:\Users\ADMIN\.conda\envs\ras`. Không tạo hoặc cài vào `venv` mới. Kiểm tra interpreter và công cụ cài đặt đang trỏ đúng môi trường này trước khi chạy/cài; không giả định dự án bắt buộc dùng Python.

## Lệnh đã kiểm chứng (M1a)

Chi tiết và giải thích ở [môi trường phát triển](docs/dev-setup.md). Chạy từ gốc repo bằng Git Bash/PowerShell:

- Chuẩn bị: `cp .env.example .env` rồi điền mọi secret trống (không commit `.env`).
- Dựng: `docker compose up -d --build --wait` (service `migrate` tự chạy `alembic upgrade head`); hạ: `docker compose down` (giữ dữ liệu) hoặc `docker compose down -v` (xóa volume).
- Migration: `docker compose run --rm migrate`; revision hiện tại: `docker compose exec api alembic current`.
- Bootstrap Admin một lần: `docker compose exec api python -m app.cli.bootstrap_admin --email <email>` (hỏi mật khẩu, không echo) hoặc `... -T ... --password-stdin < <file>`.
- Test API (cần `docker compose up -d db`): `cd api && C:/Users/ADMIN/.conda/envs/ras/python.exe -m pytest -q`; cài lần đầu `C:/Users/ADMIN/.conda/envs/ras/python.exe -m pip install -e ".[dev]"`.
- Web: `cd web && npm ci && npm run typecheck && npm run build`.

Schema thay đổi qua revision Alembic mới, không sửa revision đã merge; giữ test `test_orm_models_match_migrated_schema` xanh.
