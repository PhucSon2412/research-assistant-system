# C2d: sửa final read clock sau predicate SQL

**PR #42 đã merge `99db528` (2026-10-06).** [C2d-4a notice/intent](c2d4a-upload-notice-intent.md) đã triển khai trên nhánh review, chưa merge; PUT/download và các nhóm writer/provider/C3/C4 còn riêng. Source `0039`, safety pending tới C4. Các số đo bản sửa clock bên dưới là lịch sử, không cộng vào task mới.

**Review độc lập PR #42 tại `8f924c0` (2026-10-06):** 124/124 API chọn pass; 218 API Python/SQL hash không đổi. Đối chứng nạp riêng hàm cũ trong tiến trình test: 6 fail đúng/2 positive, không sửa file ứng dụng. Không phát hiện điểm chặn merge trong phạm vi PR; [report/source/JUnit/not-run](reviews/pr42-final-read-clock.md). Kết quả 1094 của agent bên dưới giữ riêng, không phải kiểm thử task mới.

[Mục lục](README.md) · [Task](tasks/c2d-final-read-clock.md) · [Inventory](c2d-final-read-clock-test-inventory.json) · [Review P2 lịch sử](reviews/pr41-c2d3-resource-writes.md#p2-ở-helper-đọc-clock-trước-predicate-sql)

Base fetch/xác minh `5100ff67c5c61754e4dd29693cb670a22ce6cf83`, PR #41 MERGED, chứa head `1bad143f439c69b4163baad23b7c54fac04aead5`. Nhánh `fix/c2d-final-read-clock`; không migration/grants, source head `0039` không là revision DB dev. Docs review/task chưa commit được giữ, đối chiếu và đưa cùng PR. **Safety pending tới C4; PR này không mở cutover.**

## Hành vi và nguyên nhân

Helper cũ lấy clock sau journal/profile nhưng trước `session_live`; SQL thật có thể kết thúc tại/quá expiry trong khi proof còn dùng thời gian cũ. Regression trong repo giữ bootstrap/invitation/login/runtime/journal/head/receipt/predicate thật, chỉ tăng clock sau SQL và scoped cutover trong fixture nghiệp vụ. Baseline collections 200; tại/quá idle/absolute thì code cũ vẫn 200. Lượt regression đã chỉnh fixture: 6 failure đúng assertion 200 thay vì 401, 2 pass sát trước hạn, 0 setup error/skip. Hai failure còn lại chứng minh `/auth/sessions` cũng có lỗi ở predicate cuối.

[`final_caller`](../api/app/identity/final_caller.py) giữ SQL predicate hiện hành với exact session/User, latest confirmed head/receipt, generation và revoked/active/proven/usable. Sau SQL cuối, lấy clock mới rồi kiểm `absolute_expires_at <= at` hoặc `last_activity_at <= at - idle`. Hết hạn trả `401 session_expired`, clear cookie, không DTO/link. Thành công dùng `checked_at=at` sau SQL; idle expiry vẫn `min(last_activity_at + idle, absolute_expires_at)`. Không query thêm cho TTL, không hardcode lifetime; lấy scalar timestamps sẵn có và không touch activity.

Đối chiếu `/auth/sessions` phát hiện khác với giả định task: clock chỉ nằm sau query danh sách, **chưa nằm sau predicate caller cuối**. Sau tái hiện đúng idle/absolute, route dùng cùng helper sau `list_live_sessions`, giữ exact caller, lọc từng session và response shape. Các file/session/provider helper chưa chuyển không được mở rộng bởi PR này.

## Transaction, khóa và commit

Helper giữ `require_final` thật và shared journal trên transaction caller, sau các resource locks. Scalar profile tránh identity map cũ; role hiện tại kiểm riêng cho Admin (`403 forbidden`). Không User/session/resource row lock mới, shared upgrade, touch, commit hoặc rollback trong helper. Gate hold/chain/pending vẫn đóng. Không đổi SQL ACL trước count/page/top-k, review exact grant, thin closed history, pending/common-copy history hoặc quyền private của Admin.

Resource: permitted item SHARE → owned collection SHARE → citation shared nếu cần → journal shared. Review: request SHARE → permitted private item SHARE qua exact grant → journal; common: item SHARE qua pending/review-copy clause → journal, không khóa tombstone. Các boundary tiếp tục requery, scope churn retry hữu hạn trên transaction mới và hai proof trong transaction đọc.

DevOutbox: flush audit FK → proof Admin → known audit commit → proof ở transaction mới → snapshot entries → proof sau snapshot → lọc TTL theo `checked_at`. Audit có thể đã lưu dù response bị từ chối. C2d-3 giữ nguyên User NO KEY UPDATE → item → collection → flush → journal exclusive → `_actor`/resource/clock trước business commit. Response DTO dùng `resource_json` trên transaction mới; 204 dùng `_actor` riêng. Hậu kiểm từ chối không chứng minh mutation chưa lưu, không rollback giả/retry/replay mutation. Commit exception vẫn uncertain.

Khóa giữ tới transaction caller đóng. Response đã qua boundary và nhả khóa có thể tiếp tục gửi; bytes đã gửi không thu hồi được. Không hứa snapshot toàn scope hoặc chống DBA ghi ngoài protocol.

## Kiểm chứng và giới hạn

**Suite cuối trên code `18faa30`: 1094/1094 pass, 0 failure/error/skip; collect 2530 unique nodes, 1436 not-run.** Ba JUnit không trùng, hợp đúng selection; SHA-256 218 API Python/SQL file không đổi trước/sau cả ba nhóm. final-core: 653.292 s / final-resources: 1308.332 s / final-reviews: 740.548 s. Không cộng các lượt trước/sau/đối chứng hoặc kết quả lịch sử PR #41. Safety pending tới C4.

Kết quả cuối/source/node/JUnit/hash và các lượt chuẩn bị được ghi trong [inventory](c2d-final-read-clock-test-inventory.json); không cộng kết quả lịch sử PR #41. [Test mới](../api/tests/test_c2d_final_read_clock.py) có idle khác mặc định 7 phút, absolute tới trước idle, đúng biên/vượt biên/sát trước 1 microsecond, latency profile/predicate, checked_at và expiry cap, stale identities/no touch/commit/row locks, exact binding và revoked/disabled/quarantine/held/missing receipt/head. HTTP phủ ba C2d-1, list/detail/count/empty resource, đủ 9 review/common, DevOutbox postcommit/link TTL, bốn writer có DTO sau known commit và session listing. Suite C2d-1/2a/2b/3 giữ thêm current role/ACL/wait/uncertain/pending/pool, login/session và ORM/schema.

Conda ras đã xác minh interpreter/pip; không cài thư viện hoặc venv. PostgreSQL standalone `ras-final-read-clock-db-20261006`, label `ras.task=final-read-clock-20261006`, bind `127.0.0.1:58453`; runtime/recovery LOGIN thật, credentials/admin URL/DB parts explicit trước fixture import. Runner chặn external HTTPTransport; evidence/journal/JUnit/manifest/scripts ngoài repo tại `%TEMP%/ras-final-read-clock-20261006`. Không đọc/sửa `.env`, query/nâng/restart dev, provider, pricing_verified hoặc safety ready.

Lượt chuẩn bị đầu 21 pass/38 failure: 6 key route sai trong fixture và tên env idle sai, cùng các assertion clock trên code cũ; không dùng lượt này làm regression chuẩn. Sau sửa fixture, regression chuẩn 6 fail/2 pass (không setup error). Nhóm mới sau sửa 59/59; mở rộng empty 5 pass/1 fail vì collections không pagination, sửa fixture dùng members rồi 6/6. Các lượt chồng nhau không cộng vào suite cuối. Evidence failure tổng hợp được loại giá trị link/token khỏi JUnit, giữ trạng thái/node/assertion clock.

Web/proxy/Compose không đổi: tests/typecheck/build/browser not-run. Upload/original stream/storage, review/publication/index writers, search/RAG/provider/worker, full product HTTP/proxy/backup restore/drill/cutover/production durability và C3/C4 vẫn riêng. Fault commit và latency clock là injection có SQL thật, không là lỗi mạng/mất điện/SLO. Inventory giải thích các nhóm not-run; safety vẫn pending tới C4.

## Caller map đối chiếu source

Mỗi read proof giữ predicate/TTL/binding/receipt/generation nêu trên. Clock cũ của helper: trước session_live; clock mới: sau session_live, checked_at cùng mốc. Sáu writer giữ `_actor` post-SQL trước commit; bốn response DTO dùng helper sửa, hai DELETE 204 dùng `_actor` riêng không đổi. Bảng này đối chiếu đủ 32 route liên quan, không coi 63 route toàn API đã chuyển.

| Route / handler | Boundary/role | Transaction/locks và response | Nhóm kiểm |
| --- | --- | --- | --- |
| `GET /admin/users` / `list_users` | final_caller; current Admin | journal shared, current Admin; không User/session/resource row lock | C2d-1 + clock/DevOutbox |
| `GET /admin/dev-outbox` / `dev_outbox` | final_caller; current Admin | audit/FK flush → journal shared → known commit → shared ở transaction mới → snapshot → proof mới | C2d-1 + clock/DevOutbox |
| `GET /auth/session` / `current_session` | final_caller; exact caller | journal shared, không User/session/resource row lock | C2d-1 + clock/DevOutbox |
| `GET /auth/sessions` / `my_sessions` | final_caller; exact caller | listing journal shared → helper trên cùng transaction; không row lock | C2a + clock listing |
| `GET /collections` / `list_collections` | resource_json; exact caller | owned collection SHARE → journal shared | C2d-2a + clock resource |
| `POST /collections` / `create_collection` | resource_write; exact caller | User NO KEY UPDATE → INSERT collection/flush FK → journal exclusive; known commit → collection SHARE → journal shared mới | C2d-3 + clock writer response |
| `PATCH /collections/{collection_id}` / `rename_collection` | resource_write; exact caller | User NO KEY UPDATE → owned collection UPDATE → flush → journal exclusive; known commit → collection SHARE → journal shared mới | C2d-3 + clock writer response |
| `DELETE /collections/{collection_id}` / `delete_collection` | resource_write; _actor mới cho 204, không DTO helper | User NO KEY UPDATE → owned collection UPDATE → DELETE/flush → journal exclusive; known commit → journal shared + _actor mới → 204 | C2d-3 + clock writer response |
| `GET /collections/{collection_id}/items` / `list_collection_items` | resource_json; exact caller | permitted item SHARE → owned collection SHARE → journal shared | C2d-2a + clock resource |
| `POST /collections/{collection_id}/items` / `add_collection_item` | resource_write; exact caller | User NO KEY UPDATE → visible item SHARE → owned collection UPDATE → flush → journal exclusive; known commit → item SHARE → collection SHARE → journal shared mới | C2d-3 + clock writer response |
| `DELETE /collections/{collection_id}/items/{item_id}` / `remove_collection_item` | resource_write; _actor mới cho 204, không DTO helper | User NO KEY UPDATE → owned collection UPDATE → DELETE membership/flush → journal exclusive; known commit → journal shared + _actor mới → 204 | C2d-3 + clock writer response |
| `GET /discovery/topics` / `list_topics` | resource_json; exact caller | journal shared; không resource rows | C2d-2a + clock resource |
| `GET /discovery/papers` / `list_papers` | resource_json; exact caller | citation shared → journal shared | C2d-2a + clock resource |
| `GET /discovery/papers/{paper_id}/citing-items` / `citing_items` | resource_json; exact caller | permitted private item SHARE → citation shared → journal shared | C2d-2a + clock resource |
| `GET /discovery/sources` / `source_status` | resource_json; current Admin | journal shared; current Admin, không resource rows | C2d-2a + clock resource |
| `GET /library/items` / `list_items` | resource_json; exact caller | permitted page item SHARE → journal shared | C2d-2a + clock resource |
| `GET /library/items/count` / `count_items` | resource_json; exact caller | journal shared; không khóa count scope | C2d-2a + clock resource |
| `GET /library/items/{item_id}` / `get_item` | resource_json; exact caller | permitted item SHARE → journal shared | C2d-2a + clock resource |
| `GET /library/items/{item_id}/chunks` / `get_chunks` | resource_json; exact caller | permitted item SHARE → journal shared | C2d-2a + clock resource |
| `GET /library/items/{item_id}/metadata-candidates` / `get_metadata_candidates` | resource_json; exact caller | permitted item SHARE → journal shared | C2d-2a + clock resource |
| `GET /library/items/{item_id}/coverage` / `get_coverage` | resource_json; exact caller | permitted item SHARE → journal shared | C2d-2a + clock resource |
| `GET /library/items/{item_id}/references` / `get_references` | resource_json; exact caller | permitted item SHARE → citation shared → journal shared | C2d-2a + clock resource |
| `PATCH /library/items/{item_id}` / `update_item` | resource_write; current role, Admin chỉ common visible | User NO KEY UPDATE → item UPDATE → flush → journal exclusive; known commit → item SHARE → journal shared mới | C2d-3 + clock writer response |
| `GET /admin/pending-publications` / `list_pending` | review_publication_json; current Admin | permitted common item SHARE → journal shared | C2d-2b + clock review/common |
| `GET /admin/pending-publications/{item_id}` / `get_pending` | review_publication_json; current Admin | permitted common item SHARE → journal shared | C2d-2b + clock review/common |
| `GET /admin/common-copies` / `list_copies` | review_publication_json; current Admin | permitted common item SHARE → journal shared | C2d-2b + clock review/common |
| `GET /admin/common-copies/{item_id}` / `get_copy` | review_publication_json; current Admin | permitted common item SHARE → journal shared | C2d-2b + clock review/common |
| `GET /review-requests/notice` / `notice` | review_publication_json; exact caller | journal shared; không resource rows | C2d-2b + clock review/common |
| `GET /review-requests` / `list_own` | review_publication_json; exact caller | request SHARE → chỉ item còn exact grant SHARE → journal shared | C2d-2b + clock review/common |
| `GET /review-requests/{request_id}` / `get_own` | review_publication_json; exact caller | request SHARE → chỉ item còn exact grant SHARE → journal shared | C2d-2b + clock review/common |
| `GET /admin/review-requests` / `admin_list` | review_publication_json; current Admin | request SHARE → chỉ item còn exact grant SHARE → journal shared | C2d-2b + clock review/common |
| `GET /admin/review-requests/{request_id}` / `admin_get` | review_publication_json; current Admin | request SHARE → chỉ item còn exact grant SHARE → journal shared | C2d-2b + clock review/common |

## Đối chứng, cleanup và bàn giao

Hai đối chứng tuần tự phục hồi source cũ: helper 4 failure đúng 200 thay vì 401 (idle/absolute tại/quá hạn), 2 pass sát trước; listing 2 failure đúng 200 thay vì 401. Không setup error/skip; mỗi source được khôi phục byte/SHA-256 trong finally trước suite cuối. Source kiểm là commit `18faa3065de52b0fec4e9f0a441fcf5ae4ebba19`; docs/inventory thêm sau suite, không thay code/test.

Đã kiểm container ID/name/label/image/port/mount/volume consumer rồi dọn đúng PostgreSQL thử và anonymous volume, xóa credentials.json/postgres.env tổng hợp. Metadata ID/image/StartedAt của 24 container có trước task giữ nguyên. Evidence/JUnit/log/scripts/journal ngoài repo được giữ; không query DB dev. Compileall, source heads `0039`, AST caller/source allowlist, Markdown/link/control characters và diff check đạt.

Bàn giao sau merge PR #42 `99db528`: [C2d-4a notice/intent](tasks/m6c2b2c2d4a-upload-notice-intent.md) chuyển hai JSON route trước file transfer. Tiếp tục các task writer/file/provider/C3/C4 riêng; không dùng kết quả này mở safety/pricing hoặc tuyên bố toàn C2d/T14/production/restore đã an toàn.
