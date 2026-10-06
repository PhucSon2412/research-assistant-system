# C2d-3: sáu JSON writer library/collections

[Mục lục](README.md) · [Task](tasks/m6c2b2c2d3-library-collection-json-writes.md) · [Inventory](c2d3-test-inventory.json) · [Caller inventory](c2d1-caller-inventory.json)

Base fetch `833db7cc1b3a4df6b683f72820fca83a17c19eb7`, merge PR #40, chứa head `11da7316a6be27317c2d6d27c8725aafa51533b2`. Nhánh `feat/m6c2b2c2d3-library-collection-json-writes`. Giữ docs review/handoff chưa commit trong PR này. Không migration hoặc đổi grants: head source `0039`, không suy revision DB dev. **Safety pending tới C4**, chỉ kiểm cô lập; không merge PR.

## Boundary ghi và khóa

[`api/resource_writes.py`](../api/app/api/resource_writes.py) là boundary riêng, không sửa `final_caller`, `resource_json`, `commit_auth`, predicate hay auth dependency. Sáu route vẫn lấy principal/session/Origin/CSRF thật; helper nhận exact session/User, raw CSRF của request, callable clock và idle config. Không append auth event cho mutation metadata/membership.

1. `require_writer` kiểm integrity qua connection riêng ở entry, trước resource/journal locks. Lấy runtime `lock_users` cho đúng caller User **NO KEY UPDATE**, rồi scalar actor/profile/role/CSRF và `session_live` với exact User. Không dùng role cũ trong Principal làm quyền ghi.
2. Item trước collection; mỗi SELECT khóa giữ SQL ACL. Item metadata UPDATE; add khóa item SHARE rồi collection UPDATE; rename/remove/delete chỉ collection UPDATE. Create INSERT collection mới, không khóa tài nguyên khác. Không khóa item của membership cũ khi remove/delete, không nâng shared journal thành exclusive.
3. DML và audit/FK flush hết trước journal. Collection owner và audit actor FK tương thích User NO KEY UPDATE; membership item FK đi sau item SHARE, collection FK sau collection UPDATE. Readers search/RAG khóa item → collection → membership; cleanup tombstone → item → membership và không cần collection row lock. Remove/delete không lấy item nên không tạo vòng ngược cleanup/reader.
4. `require_final(writer=True)` lấy journal exclusive trực tiếp trên transaction ghi. Expire identities, scalar actor mới, recheck SQL resource gate không khóa mới/DML, rồi actor/clock cuối sau mọi resource SQL. Kiểm predicate latest confirmed head/receipt, generation, active/proven, exact session/User, TTL và CSRF; không touch idle. `_actor` lấy clock lại **sau chính SQL `session_live`** và so idle/absolute với scalar session mới, vì predicate có thể hoàn tất sau thời điểm mà nó dùng làm input. User và journal locks giữ trạng thái auth ổn định khi ghi; không thêm khóa hoặc gia hạn activity để xử lý clock.
5. Commit chỉ sau mọi kiểm cùng transaction. Denial/known precommit failure rollback toàn mutation/audit; auth activity của dependency thuộc transaction riêng. `finally` rollback/close giải phóng transaction/connection trên mọi đường.

| Route | Resource và flush trước journal | Gate cuối trước commit | Known commit: transaction mới |
| --- | --- | --- | --- |
| PATCH item | SQL ordinary visible → item UPDATE → metadata + audit ID/fields → flush | Ordinary visible + `can_edit_item` từ role hiện tại | `resource_json` đọc lại PATCH DTO, title/can_edit/search/RAG flags mới; item SHARE → journal shared → actor/CSRF cuối |
| POST collection | INSERT UUID mới, owner FK → flush | SQL đúng owner collection | `resource_json` đọc collection mới theo owner, collection SHARE → journal shared → actor cuối; 201 |
| PATCH collection | Owner SQL → collection UPDATE → name/updated_at → flush | SQL đúng owner | Cùng boundary đọc owner collection mới; 200 |
| DELETE collection | Owner SQL → collection UPDATE → DELETE memberships tường minh → DELETE collection → flush | Proof owner của hàng đã xóa được khóa bảo vệ tới commit; không đòi hàng đã DELETE còn tồn tại | Gate shared + exact actor/CSRF/clock mới, không lookup hàng đã xóa; 204 không body |
| POST membership | Item visible SHARE → owned collection UPDATE → INSERT ON CONFLICT DO NOTHING → flush | Owner collection + membership JOIN ordinary visible item | Đọc lại đúng membership/item/collection, `resource_json` khóa item trước collection; 201 ID chỉ sau proof mới |
| DELETE membership | Owned collection UPDATE → DELETE membership, rowcount bắt missing → flush | Owner collection SQL; không yêu cầu item visible | Gate shared + exact actor/CSRF/clock mới; 204 không body |

Collection owner của hàng deleted không thể SQL requery sau flush. Vì vậy DELETE tiêu thụ owner proof dưới UPDATE lock trước DML, giữ khóa đó tới commit; đây là khác biệt có chủ đích với recheck hàng còn tồn tại. Không dùng proof đó cho response 204 sau commit. Không thay ownership/visibility/rights/index hoặc grants. Common listed metadata-only vẫn là membership; pending/revoked bị ordinary gate chặn. Admin không sửa private hoặc collection của người khác. Remove gỡ được membership ẩn/thu hồi và không đọc payload item.

## Commit, hậu kiểm và lỗi

Commit xác định chỉ là mutation đã lưu, **không là quyền trả DTO**. Hậu kiểm dùng transaction mới sau expire, hai quan sát resource JSON/clock/proof và actor/CSRF cuối; mọi payload lấy từ DB mới. `204` chỉ cần proof caller mới, không tạo 404 vì resource vừa xóa. Hậu kiểm có thể trả 401/403/503 hoặc 409 khi scope mất; mutation đã commit vẫn còn, không tự rollback/retry nó.

| Giai đoạn | Mã/hành vi |
| --- | --- |
| Auth/CSRF/ACL trước commit | 401 session_expired + clear cookie; 403 csrf_failed/forbidden; foreign/missing 404 not_found |
| Gate/journal cuối | 503 service_recovering; rollback business rows/audit |
| SQL/lock/DML lỗi trước commit | 503 resource_write_unavailable, không lộ SQL/URL/secret; rollback |
| Commit đã bắt đầu, exception kể cả DB đã commit | 503 resource_write_outcome_uncertain; không success, không retry/replay; rollback chỉ dọn state client |
| Known commit, resource hậu kiểm thành 404 | 409 resource_scope_changed; không DTO cũ |
| Known commit, lỗi đọc DB hậu kiểm khác | 503 resource_write_response_unavailable; đã lưu nhưng chưa đọc được response |

Web chỉ thêm map/test ba mã lỗi resource; commit uncertain yêu cầu kiểm tra lại sau khi kết nối/đăng nhập, không tự gửi lại. Mã scope changed dùng chung GET vẫn yêu cầu tải lại trang. Auth lỗi hậu kiểm vẫn giữ mã auth; nó không chứng minh mutation chưa lưu. Create chưa có exactly-once/idempotency; repeated add giữ dedupe cũ. Không journal membership/delete collection, nên restore membership cũ là giới hạn còn riêng.

## Kiểm chứng và giới hạn

**Suite cuối tại code `12ffccc`: 1095/1095 pass**, 0 failure/error/skip, 2208.995 s; collect 2465 unique nodes, **1370 not-run**. Gồm C2d-3 252 (6 pending HTTP thật), C2d-1 93, C2d-2a 301, C2d-2b 260, login/session 68, reset/status 103, library ACL/CRUD 12, collection GET/CRUD 5 và ORM/schema 1. JUnit khớp đúng selection, không thiếu/trùng; SHA-256 217 API Python/SQL file không đổi. 10/10 đối chứng bị assertions bắt, khôi phục source bytes/hash; web 45/45, typecheck/build đạt.

Đã kiểm ID/name/label/image/port/mount và consumer volume trước khi dọn đúng PostgreSQL cô lập cùng anonymous volume, xóa hai file credentials tổng hợp. Metadata ID/image/StartedAt của 24 container có trước task giữ nguyên, không query/nâng/restart DB dev. Evidence/journal/JUnit/log/scripts giữ ngoài repo. Không `.env`, provider/pricing hoặc safety ready.

Các lượt chuẩn bị giữ riêng: fail-fast 1 failure vì cookie CSRF fixture thiếu; smoke 6 pass; expanded 222 pass/2 failure observer/storage; targeted 30 pass/1 failure owner-immutable fixture; late 33 pass; fresh 11 pass. Lượt đầu đã dừng, không JUnit đầy đủ, không tính là đạt. Các lỗi chuẩn bị trên sửa fixture, không nới predicate/grants/gate. Suite lịch sử tại `895878e` đạt 1089/1089 (collect 2459, not-run 1370) nhưng chưa có ca SQL predicate cuối vượt TTL. Ca mới trước sửa fail 1/1: business commit xảy ra rồi response 401; đã sửa code để lấy clock sau predicate SQL. Nhóm clock/wait sau sửa 38/38 và 10/10 đối chứng chạy lại; các lượt này giữ riêng, không cộng vào suite cuối.

Kết quả từng lượt, node/JUnit/hash/source, đối chứng và not-run được ghi trong [inventory](c2d3-test-inventory.json). Không cộng suite lịch sử PR #40 vào lượt C2d-3. Runtime accounts qua bootstrap/invitation/login thật; fixture chỉ patch cutover pending cho nghiệp vụ. Pending HTTP giữ gate thật cho sáu route. Corruption/resource setup đặc quyền chỉ ở DB thử; không mở runtime quyền.

Conda ras; PostgreSQL standalone `ras-c2d3-db-20261005`, label `ras.task=c2d3-20261005`, bind `127.0.0.1:58449`. Credentials/admin URL/DB parts explicit trước import fixture, không fallback `.env`. Runner chặn external HTTPTransport sync/async và kiểm SHA-256 API Python/SQL trước/sau test. Evidence/journal/JUnit/log/scripts ngoài repo `%TEMP%/ras-c2d3-20261005`.

Fault commit thực hiện commit hoặc rollback DB thật rồi ném lỗi tại Python boundary; chưa là thử lỗi mạng DB/mất điện. Race dùng Event + PostgreSQL wait/deadline, không hứa response đã qua boundary bị thu hồi hồi tố. Fixture common original dùng bytes tổng hợp trong bộ nhớ khi dựng rights; chưa là MinIO/download/stream drill. Không provider thật, pricing flag, dev DB/journal hoặc readiness thay đổi.

File/upload/intent, review/publication/index writers, search/RAG provider caller, worker integration và C3/C4 còn riêng. Full business API/proxy/browser/storage/backup restore/cutover và production durability chưa được chứng minh bởi selection này. Không coi toàn C2d/T14 hoàn tất.
