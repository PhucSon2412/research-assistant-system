# Review PR #41: C2d-3 library và collection JSON writes

**Cập nhật 2026-10-06:** PR #41 đã merge `5100ff6`, fetch/GitHub CLI xác minh. [Prompt sửa P2 final read clock](../tasks/c2d-final-read-clock.md) đã triển khai trên nhánh review, xem trạng thái khắc phục cuối report; phép kiểm bên dưới giữ theo source/thời điểm review. Safety pending tới C4.

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2d3-library-collection-json-writes.md) · [Bàn giao](../c2d3-library-collection-json-writes.md) · [Inventory agent](../c2d3-test-inventory.json)

## Kết luận

Ngày 2026-10-05: fetch/GitHub CLI xác nhận PR #41 **OPEN/chưa merge**, head `1bad143f439c69b4163baad23b7c54fac04aead5`, base `833db7cc1b3a4df6b683f72820fca83a17c19eb7` (PR #40). Code `12ffccc8ca39b0704fda29a98259b289431a9425`; head chỉ bàn giao/docs. Source migration `0039`, không truy vấn revision DB dev.

**Không phát hiện điểm chặn merge trong phạm vi 6 writer C2d-3; có thể merge PR #41 theo phạm vi đó.** Có một **P2 đã tồn tại ở helper đọc chung**, tái hiện độc lập bên dưới; cần PR sửa hẹp trước cutover, không đánh dấu toàn bộ C2d/T14 đã hoàn tất. Reviewer không sửa ứng dụng, merge, push hoặc comment PR. Docs review chưa commit: giữ để coding agent đưa vào PR phù hợp. Safety **pending tới C4**.

## Đối chiếu code

- Đúng 6 route gọi `resource_write`; 63/63 caller inventory khớp AST. GET/shared formatters không đổi so base; migrations, auth dependency/predicate/grants/commit, authz, resource/review read helpers và Compose không đổi.
- Writer riêng: entry integrity → caller User NO KEY UPDATE → permitted item khi cần → owned collection → DML/FK/audit flush → journal exclusive trực tiếp. Không shared upgrade hoặc lấy User/resource locks mới sau journal.
- Scalar actor exact session/User, current role/CSRF, latest head/receipt/predicate và TTL; `_actor` lấy clock lại sau chính SQL predicate rồi kiểm cả idle/absolute. Final resource gate và actor/clock trước commit; mọi denial dọn transaction/rollback nghiệp vụ.
- Known commit dùng transaction/proof mới, resource JSON đọc lại title/name/membership/flags theo quyền hiện hành. DELETE không đòi hàng vừa xóa còn tồn tại; mutation đã commit không bị rollback bởi postcheck. Commit exceptions trả uncertain, không retry/replay metadata.
- Owner/Admin ACL giữ nguyên; add item trước collection, repeated add dedupe; remove membership ẩn không đọc payload; delete collection giữ item/version/file. Audit chỉ IDs/field names. Web ba message phân biệt precommit/uncertain/response failure, không tự retry.

## Kiểm độc lập

| Phép kiểm | Kết quả |
| --- | --- |
| API selection tại `1bad143` | **270/270 pass**, 0 failure/error/skip, **529,945 s**, 3 warning hiện có |
| Thành phần selection | 252 C2d-3 (6 pending HTTP thật), 12 library ACL/CRUD, 5 collection GET/CRUD, 1 ORM/schema |
| JUnit/source | 270 node unique khớp selection; 217 API Python/SQL hash không đổi |
| Web | `npm test --prefix web` **45/45 pass**; `npm run typecheck --prefix web` đạt |
| Static/docs trước thêm report | 57 Markdown/866 internal links, 0 lỗi/control characters; 6 writer, nonwriter AST/grants/migrations giữ nguyên |
| Docs sau cập nhật review | 58 Markdown/881 internal links, 0 lỗi; diff check của docs tracked và report mới sạch |
| Inventory/JUnit của agent | Đọc artifact có sẵn: 1095 node unique và SHA-256 khớp inventory; không coi đây là 1095 test reviewer đã chạy |

JUnit selection SHA-256: `449d08e423f295d0b2ea31441cd55eb726ff686a11203e1630764357666dd0d2`. Evidence ngoài repo: `%TEMP%/ras-pr41-review-20261005`, gồm `selected.xml/log`, selection, source manifests, summary và cleanup JSON. Các lỗi tooling khảo sát (wildcard PowerShell/path fixture không tồn tại, validator ban đầu bỏ `line` chỉ ở một phía, tham số JUnit của ca bổ sung) được sửa ngoài repo; không là test failure sản phẩm. Suite 270 không bị dừng hoặc sửa source giữa lúc chạy.

## P2 ở helper đọc: clock trước predicate SQL

**Đã quan sát:** GET `/collections` trả **200** khi SQL kiểm session cuối hoàn tất đúng mốc idle 30 phút, nơi hợp đồng đòi `401 session_expired`.

Nguồn: [final_caller](../../api/app/identity/final_caller.py) lấy `at = clock()` **trước** `session_live(..., at, ...)`, rồi trả proof không lấy clock lại sau SQL đó. [resource_json](../../api/app/api/resource_reads.py) gọi helper ở boundary cuối nhưng không kiểm TTL sau khi predicate trả về. Các file này **không đổi từ base `833db7c`** trong PR #41. Chỉ GET collections đã tái hiện trực tiếp; các read caller dùng helper cần kiểm tương ứng.

**Ca tái hiện ngoài repo:** tài khoản/bootstrap/invitation/login/runtime/journal/predicate thật, chỉ cutover fixture scoped. Baseline GET 200. Wrapper gọi **SQL `session_live` thật**, sau lượt thứ hai (final boundary) mới tăng fake clock tới NOW + 30 phút, mô phỏng latency vượt TTL; không thay kết quả SQL hoặc principal. Assertion đòi 401 fail đúng với 200: **1 failure, 0 setup error/skip** (`read-clock.xml/log`, 2,49 s).

**Đối chứng ngoài repo:** chỉ ở fixture, bọc final read proof để lấy clock sau SQL, kiểm idle/absolute expiry và cập nhật checked_at; cùng ca **1/1 pass** (2,40 s, `read-clock-counter.xml/log`). Không sửa file ứng dụng hoặc mở bypass production. Hai lượt này tách khỏi 270/270; không gọi toàn project xanh.

**Vì sao không chặn PR #41:** 6 writer mới có `_actor` kiểm TTL sau predicate SQL, cả trước commit và sau read response; 6 test `test_clock_after_final_predicate_before_commit` nằm trong selection đã qua. Lỗ hổng GET cũ không được sửa hoặc tạo ra bởi PR này; production safety vẫn pending.

**Task sửa riêng đề xuất:** sửa final read helper lấy clock sau mọi predicate SQL có thể chậm; giữ exact binding/head/receipt/current role, không touch idle hoặc lấy User/session row lock sau journal. Thêm idle/absolute đúng/sát biên, pending thật và hồi quy C2d-1/2a/2b + response C2d-3. Không migration, không mở cutover; không mock live result để pass. Chốt source/JUnit/inventory và báo riêng trước C4.

## Môi trường, cleanup và giới hạn

Conda ras interpreter/pip đã xác minh; không cài thư viện/venv. PostgreSQL standalone `ras-pr41-review-db-20261005`, labels `ras.review=pr41-20261005`/`ras.task=c2d3-review-20261005`, bind `127.0.0.1:58450`. Explicit test admin URL/DB parts trước import fixture, runtime/recovery LOGIN thật, external HTTPTransport bị chặn; journal và credentials tổng hợp ngoài repo.

Đã kiểm ID/labels/mount/sole volume consumer rồi xóa container và anonymous volume đúng của review, xóa credentials. Metadata ID/image/StartedAt của **9 container dev không đổi**. Không đọc/sửa `.env`, query/nâng/restart DB dev, gọi provider hoặc đổi pricing/safety.

Reviewer chưa chạy lại **825/1095** node khác của suite agent hoặc **2195/2465** node ngoài selection độc lập; 1370 not-run trong inventory agent giữ nguyên. Không chạy source mutants/pool probe độc lập thêm (selection có assertions pool Engine API), web build/browser/proxy, storage/stream/provider/worker/restore/drill/C3/C4. Fault commit và latency là mô phỏng trong test, không chứng minh lỗi mạng/mất điện hoặc SLO. Kết quả agent/reviewer và lỗi helper đọc giữ riêng; không coi toàn auth/restore/production đã an toàn.

## Trạng thái khắc phục P2 sau merge (2026-10-06)

Bản sửa trên nhánh `fix/c2d-final-read-clock`, code `18faa30`, chưa merge: [handoff/caller map](../c2d-final-read-clock.md) và [inventory mới](../c2d-final-read-clock-test-inventory.json). **Suite cuối trên code `18faa30`: 1094/1094 pass, 0 failure/error/skip; collect 2530 unique nodes, 1436 not-run.** Ba JUnit không trùng, hợp đúng selection; SHA-256 218 API Python/SQL file không đổi trước/sau cả ba nhóm. final-core: 653.292 s / final-resources: 1308.332 s / final-reviews: 740.548 s. Không cộng các lượt trước/sau/đối chứng hoặc kết quả lịch sử PR #41. Safety pending tới C4.

Regression repo trước sửa fail đúng với 200 tại/quá idle/absolute; sau sửa đạt. Hai source mutant phục hồi helper/listing cũ bị assertions bắt, byte/hash khôi phục trước suite cuối. Báo cáo review/P2 và FAIL tại `1bad143` phía trên giữ nguyên như lịch sử, không đổi thành PASS. Safety pending tới C4; kết quả mới không chứng minh writer/file/provider/C3/C4 hoàn tất.
