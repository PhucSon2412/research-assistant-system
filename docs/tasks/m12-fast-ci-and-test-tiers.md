# M12 — CI nhanh và kiểm thử theo tầng

**Giao 2026-10-29, sau merge PR #72 (`origin/main = 959e4a4`).** Một PR. Đọc [chính sách kiểm thử giai đoạn dùng cá nhân](../decisions.md#chính-sách-kiểm-thử-giai-đoạn-dùng-cá-nhân-2026-10-29) trước khi làm.

## Bối cảnh: vì sao CI API mất ~1,5 giờ

Orchestrator đo trên JUnit của run `40475972339` (head PR #72), job API chạy 1 giờ 35 phút:

- Selection có 1.347 test, tổng 5.722 s, chạy **tuần tự** trong một job.
- Fixture `migrated_db_url` (`api/tests/conftest.py`) tạo DB từ `template0` rồi chạy `alembic upgrade` qua **52 revision cho từng test**. Đây là phần lớn thời gian của các test thường (median 1,8 s/test).
- Các vòng dump/restore chiếm khoảng 3.000 s, mỗi test khoảng 30 s:
  - `test_session_revocation_journal`: 1.242 s / 50 test;
  - `test_reset_token_journal`: 1.198 s / 92 test;
  - `test_invitation_journal`: 625 s / 88 test.
- Một test đơn lẻ mất **810 s**: `test_evaluation.py::test_acl_same_doi_hash_matrix_through_runner`.
- Mọi PR đều chạy job API, kể cả PR chỉ sửa docs hoặc web.

Chủ dự án muốn agent dành thời gian cho code, CI ngắn lại. **Không** muốn giảm độ phủ các bất biến an toàn.

## Mục tiêu đo được

| Lượt chạy | Hiện tại | Mục tiêu |
|---|---|---|
| CI trên PR, job API (thời gian chờ thực tế) | ~95 phút | **≤ 15 phút**, nên ≤ 10 |
| PR chỉ sửa docs | ~95 phút | không chạy job API/web (hoặc job no-op < 1 phút) |
| PR chỉ sửa web | ~95 phút | chỉ job web (~1–2 phút) |
| Tầng đầy đủ (push `main`, nightly, chạy tay) | không có | ≤ 45 phút |
| Agent chạy local trên Windows | tuần tự | có một lệnh song song, ghi thời gian thật |

Số trước/sau phải lấy từ JUnit/log thật của GitHub Actions, không ước lượng.

## Việc cần làm

### 1. DB mẫu đã migrate, clone cho từng test

- Migrate **một lần** cho mỗi session/worker vào một database template, đặt tên theo hash nội dung `api/migrations/` để template cũ không bị dùng nhầm. Mỗi test clone bằng `CREATE DATABASE … TEMPLATE <tên>`.
- `register_new_install` vẫn chạy **cho từng test** với journal riêng trong `tmp_path`. Template chỉ chứa schema đã migrate, chưa có genesis/registration.
- Template không được có kết nối đang mở khi clone. Dispose engine và xử lý race giữa các worker (advisory lock trên DB admin hoặc tên template theo worker).
- Giữ hành vi test:
  - test cần đúng đường `upgrade` (`test_migrations.py`, test nâng cấp từ revision cũ, test dùng `empty_db_url`) vẫn chạy migration thật;
  - thêm một kiểm tương đương: schema của bản clone khớp schema của một lần `upgrade head` mới (ví dụ so `pg_dump --schema-only` đã chuẩn hóa, hoặc so catalog). Chạy một lần mỗi session.
- `shared_migrated_engine` và các fixture tự gọi `upgrade` khác dùng cùng cơ chế nếu không cố ý kiểm migration.

### 2. Chạy song song

- Thêm `pytest-xdist` vào dev extras và pin trong constraints (dev). Chạy `-n auto` hoặc số worker cố định đã đo.
- Kiểm tài nguyên dùng chung giữa các worker:
  - role PostgreSQL cấp cluster (runtime/migrator/operator…);
  - key Redis của limiter, bucket MinIO, tên project/container Docker, cổng;
  - biến môi trường và cache `get_settings`.

  Mỗi tài nguyên phải tách theo worker, hoặc gom test xung đột bằng `xdist_group` + `--dist loadgroup`. Không được để test "may mắn pass" vì thứ tự chạy.
- Thêm matrix shard trên GitHub nếu một job vẫn quá 15 phút (ví dụ chia theo thời gian từ JUnit lần trước). Mỗi shard dùng context `m8a_test_host.py` riêng.

### 3. Tầng test

- Thêm marker `slow` (khai báo trong `pyproject`, `--strict-markers`) cho:
  - các vòng dump/restore;
  - ma trận evaluation 810 s;
  - test cần image tiến trình hoặc container host đặc biệt.

  Trước khi gắn `slow`, xem test 810 s có chậm vô lý không (ví dụ migrate lại cho từng ô ma trận). Sửa được rẻ thì sửa.
- **PR:** tầng nhanh = mọi test chạy được với harness `m8a_test_host.py`, trừ `slow`. **Mở rộng selection** thay vì chỉ giữ 31 file hiện có, nếu thời gian còn trong mục tiêu: ưu tiên ACL/search/RAG/ingest/library/review/revocation. Ghi rõ file nào chưa vào CI và vì sao (cần Chrome/image/fixture C3…).
- **Push `main` + `schedule` nightly + `workflow_dispatch`:** tầng đầy đủ gồm cả `slow`.
- **Lọc theo đường dẫn:**
  - PR chỉ sửa `docs/**`, `*.md`: không chạy job API/web;
  - PR chỉ sửa `web/**`: chỉ job web;
  - PR sửa `deploy/**`: chạy deploy-config.

  Dùng cơ chế không cần secret. Action bên thứ ba phải pin SHA; có thể tự viết một bước `git diff` nhỏ. Mọi thay đổi `api/**`, `scripts/**`, `.github/**`, constraints vẫn chạy tầng nhanh.
- Cache pip (theo `api/constraints-rc2.txt` + `pyproject`). Bỏ phần cài lặp nếu có.

### 4. Giữ nguyên các bảo đảm của CI hiện tại

- Không đọc `.env`, không gọi provider/arXiv; chặn HTTP ra ngoài như cũ.
- Credential của container cho mỗi run là ngẫu nhiên.
- Quét secret JUnit trước khi upload; job fail khi test fail. Không `continue-on-error` và không tự retry test fail.
- Deselect hiện có chỉ giữ nếu vẫn cần, kèm lý do.

### 5. Lệnh local cho agent

- Một lệnh trên Windows/Conda (`$py`) chạy tầng nhanh song song qua harness, có lọc theo file/marker. Ghi vào `docs/dev-setup.md` kèm thời gian đo thật trên máy này.
- Không động tới demo `ras-pilot`, dev `ras` hay Dify khi chạy test. Chỉ dừng project/container do lượt test tự tạo.

## Kiểm và bàn giao

- Bảng trước/sau cho từng job: wall time, số test, số file, số test `slow`, run ID.
- Ít nhất **một run tầng đầy đủ** (`workflow_dispatch` trên nhánh) pass, và một run PR tầng nhanh pass.
- Chứng minh xdist không làm test phụ thuộc thứ tự: chạy tầng nhanh hai lần với seed/thứ tự khác (ví dụ `-p randomly` nếu thêm, hoặc `--dist` khác). Ghi kết quả.
- Một PR tiếng Việt, không merge, giữ author/committer, không AI co-author/trailer/promo. Handoff ngắn `docs/m12-fast-ci.md` gồm:
  - cơ chế template, cách thêm test `slow`;
  - lệnh local;
  - các file chưa vào CI.
- **Không áp dụng** cho task này: hash-seal source, inventory JSON toàn bộ node, countercheck đột biến.

## Ngoài phạm vi

- Không đổi code ứng dụng, schema hay image.
- Không giảm assertion để test nhanh hơn.
- Không xóa test. Nếu thấy test trùng lặp rõ ràng, chỉ liệt kê trong handoff để orchestrator quyết.

**Trạng thái khi giao:** mới giao, chưa triển khai. Có thể chạy song song với [M13](m13-library-qa-and-vector-scale.md); PR nào merge sau thì rebase.
