# Review PR #36: C2c-1 bootstrap runtime

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2c1-bootstrap-initial-receipts.md) · [Bàn giao](../c2c1-bootstrap-receipts.md)

Ngày 2026-10-04, review source `64cf34223e71a47ae6276cc627b01a0eaa963b8e` (code `51f5714`, migration `0037`), base main `1d3e558`. **Chưa đề nghị merge:** có một lỗi P2 đã tái hiện ở boundary SQL. Safety vẫn pending tới C4; không có kết luận rằng HTTP production đang mở hoặc có đường tấn công HTTP mới.

## P2: routine tạo Admin không kiểm installation mode

Vị trí: `api/migrations/versions/0037_bootstrap_receipts.sql`, routine `ras_auth_bootstrap_prepare`, phần kiểm sau bootstrap advisory lock.

Python `_history_is_new` từ chối genesis `existing_db`/`rebuilt_from_db`. SQL prepare chỉ kiểm User, singleton, receipt/head và các journal record ngoài `genesis`/recovery bookkeeping; chưa kiểm registration/genesis phải là cài mới. Một genesis `existing_db` hợp lệ vì vậy qua được SQL prepare.

Tái hiện trên PostgreSQL pgvector 16 mới, journal hợp lệ và runtime LOGIN đã provision:

1. Upgrade DB trống lên `0037`; dùng `recovery.register_existing`, rồi `recover` để hoàn tất integrity/backfill. Genesis thực là `existing_db`, không sửa journal hay trigger.
2. Chỉ fixture thay kết quả `auth_safety.cutover` thành `ready` như nhóm test C2 hiện hành. Integrity, hold, grants, routine, mirror/receipt, commit và file journal giữ thật.
3. Runtime gọi `ras_auth_bootstrap_prepare` trực tiếp, stage credential/status `reason=bootstrap` và gọi `commit_auth` (không gọi wrapper bootstrap hoặc thêm guard Python vào test).
4. Transaction commit; User có `(role, credential_state, status_safety) = ('admin', 'usable', 'proven')`, `account_live=true`.

**Kỳ vọng:** boundary SQL creation từ chối installation đã đăng ký là existing, trước INSERT User/singleton/audit và trước append. **Thực tế:** Admin cùng receipt/singleton được commit. CLI hiện hành vẫn chặn đúng; lỗi nằm ở contract của routine được cấp EXECUTE cho runtime. Guard trong CLI không thay được điều kiện creation của routine, đặc biệt khi C2c-2 sẽ bổ sung caller/routine tạo account.

## Prompt sửa cho coding agent PR #36

Giữ PR #36 mở, sửa đúng lỗi này rồi báo lại; chưa chuyển C2c-2, không mở pending hoặc tác động dev.

- Bổ sung guard DB sau bootstrap advisory lock: installation phải có registration và genesis mirror hợp lệ, khớp binding journal hiện hành và mode **new_install**. Từ chối mode khác, thiếu hoặc NULL/sai binding theo cách NULL-safe. Giữ các guard không User/singleton/receipt/head/business history đã có. Chọn SQL cụ thể theo schema thực tế; không suy cài mới chỉ từ DB trống hoặc chỉ từ một trường mode caller cung cấp.
- Giữ guard file độc lập của writer ở entry và dưới journal lock cuối. Guard SQL bổ sung không chứng minh file journal độc lập đang mới nhất, không thay integrity gate hoặc kiểm file.
- Giữ thứ tự khóa, cặp receipt/singleton/audit cùng transaction/batch, runtime/recovery privileges và pending tới C4. Không thêm force/register/init/recovery shortcut.
- Thêm migration sau head thực tế; dự kiến `0038`. Không sửa revision đã merge; chỉ cân nhắc sửa `0037` tại chỗ nếu có chứng cứ chắc chắn chưa áp vào DB lâu dài nào và ghi rõ chứng cứ.
- Test bằng runtime LOGIN thật gọi routine trực tiếp, không chỉ kiểm `_history_is_new`: `existing_db`, `rebuilt_from_db`, thiếu/null/mismatch registration/genesis phải bị từ chối; journal bytes, User/singleton/receipt và audit không đổi. Có đối chứng `new_install` hợp lệ vẫn bootstrap thành công và login bằng cặp receipt thật.
- Ca `existing_db` phải đi qua đăng ký/recover thật như bước tái hiện, không chỉ dựng `SimpleNamespace` cho Python helper. Các ca malformed binding có thể dùng fixture SQL setup tách biệt; routine và guard kiểm vẫn thật.
- Chạy migration upgrade có dữ liệu, ORM/schema, nhóm C2c-1/bootstrap và hồi quy nền bị ảnh hưởng. Kiểm đối chứng bỏ riêng guard mới phải làm test non-new fail; khôi phục bytes trước suite cuối. Báo pass/fail/not-run theo source, không cộng các lượt kiểm độc lập.
- Cập nhật bàn giao/inventory/decisions và mô tả PR tiếng Việt, commit/push theo quy ước hiện hành, chưa merge.

## Kiểm chứng độc lập của reviewer

- Conda interpreter/pip đã xác minh đúng `C:\Users\ADMIN\.conda\envs\ras`; không venv.
- Nhóm `test_c2c1_bootstrap.py`, `test_c2c1_migration.py`, `test_bootstrap.py` và `test_orm_models_match_migrated_schema`: **97 passed**, 3 warning hiện có, 168,51 s.
- Ca mới `test_existing_registration_must_not_create_bootstrap_admin`: **1 failed đúng lỗi**, 2,51 s. Assertion quan sát Admin live sau commit, không phải lỗi môi trường/fixture.
- Runner chặn sync/async HTTPTransport, dùng explicit test admin URL và disposable runtime/recovery credentials; source Python/SQL API giữ nguyên SHA-256 trong từng lượt. Không đọc/sửa `.env`, gọi OpenRouter hoặc truy vấn DB dev. Không chạy lại toàn 597 test của coding agent hoặc web.
- Evidence và test tái hiện ở `%TEMP%/ras-pr36-review-20261004`, ngoài repo. Đã kiểm label `ras.review=pr36-20261004`, dọn PostgreSQL `ras-pr36-review-db-20261004` và xóa credentials dùng một lần. Không commit journal, credentials hay dump.

Số 597/ConPTY/restore trong inventory C2c-1 là chứng cứ riêng của coding agent. Review này không chạy lại ConPTY hoặc diễn tập dump/restore, không chứng minh cutover/production durability.

## Bản sửa của coding agent để review lại

Theo prompt trên, bản sửa thêm `0038`, không sửa `0037`: prepare kiểm registration và confirmed genesis mirror `new_install` cùng binding UUID/hash/envelope theo cách NULL-safe trước INSERT. Definer được thêm SELECT registration tối thiểu; owner/EXECUTE ACL, guard file độc lập, transaction/batch, lock order và pending giữ nguyên. Test trực tiếp runtime phủ đăng ký existing/recover thật và fixture malformed; new-install vẫn tạo receipt/login thật. Bàn giao chi tiết ở [C2c-1](../c2c1-bootstrap-receipts.md#sửa-p2-installation-tại-sql-creation). Kết quả sửa được ghi riêng, không cộng với kiểm độc lập 97 pass/1 fail hoặc lượt 597 cũ. PR vẫn mở, chưa merge; cần người review lại.

Code `d222fb1`: **626/626 pass**, 0 failure/error/skip, 3 warning hiện có, **834,11 s**; **202** source file giữ SHA-256. Collect 1402, **776 not-run**. Đối chứng bỏ riêng guard SQL mới bị bắt (1 failure/0 errors), source khôi phục trước suite cuối. [Inventory P2](../c2c1-p2-test-inventory.json) và [lệnh cô lập](../dev-setup.md#sửa-p2-installation-pr-36-cô-lập). Project/volume/credentials P2 đã dọn sau xác minh ownership; không đọc/sửa `.env`, tác động dev hay provider. Không chạy lại web/ConPTY/dump-restore trong P2; safety pending tới C4.
