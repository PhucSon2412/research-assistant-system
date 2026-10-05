# C2d-2a: final caller và quyền ở 13 GET JSON

[Mục lục](README.md) · [Task](tasks/m6c2b2c2d2a-resource-json-reads.md) · [C2d-1](c2d1-final-caller-reads.md) · [Inventory kiểm](c2d2a-test-inventory.json) · [Caller inventory](c2d1-caller-inventory.json)

Base fetch ngày 2026-10-05: `d06664e53383071237c21e802f439fda348a1afd`, merge PR #38; nhánh `feat/m6c2b2c2d2a-resource-json-reads`. Không migration, head source `0039`; không suy revision DB dev. Giữ docs orchestrator/review #38 cùng PR. **Safety pending tới C4**, chỉ kiểm cô lập.

## Boundary và transaction

13 handler GET gọi riêng [`resource_json`](../api/app/api/resource_reads.py); `_read_*` giữ SQL ACL, shape, ordering/pagination và formatter cũ. Writer/PATCH, collection CRUD, search/RAG/file/review/publication không đi qua boundary mới. `_item_out`, `_out`, các gate SQL và helper citations dùng chung không được gắn auth guard hoặc đổi transaction của writer.

Entry dependency vẫn resolve/touch/commit/check/rollback thật. Transaction của route chỉ đọc, không pending ORM mutation; boundary từ chối session bẩn, `no_autoflush` và `expire_all` trước mỗi lượt đọc để bỏ identity map cũ. Không auth event/job/audit/business commit, provider hay storage I/O. Activity duy nhất thuộc dependency hiện hành; final guard không touch.

Thứ tự:

1. Đọc candidate bằng SQL ACL trước count/order/limit/offset. DTO này chỉ dùng thu thập UUID trang được phép, không là response.
2. Khóa item trang `FOR SHARE`, SQL ACL ngay trong lock query, UUID tăng dần; rồi khóa collection của caller `FOR SHARE`, UUID tăng dần. Không khóa toàn scope cho count. Collection parent bảo vệ membership vì mọi writer membership hiện hành khóa parent `FOR UPDATE` trước DML; không thêm khóa membership sau journal.
3. Với references, discovery papers và citing-items, lấy shared citation advisory lock trước journal. Discovery writer giữ exclusive citation lock trước commit trang/tombstone/link; ingest reconcile giữ shared citation lock và fence item `KEY SHARE` tương thích reader `SHARE`.
4. Gọi `final_caller` bằng exact session/User và clock callable, shared journal trên chính transaction; dựng Principal mới từ profile/role proof. Không User/session row lock, không connection thứ hai, không proof qua commit.
5. Đọc lại toàn DTO/count/page/flags bằng SQL ACL dưới các khóa; đọc thêm một lượt để phát hiện churn. Hai quan sát phải bằng nhau và UUID phải đúng tập đã khóa. Kiểm caller/clock lần cuối sau mọi SQL tài nguyên. Role/profile thay đổi cũng gây dựng lại DTO bằng proof mới.
6. Nếu scope/page/count thay đổi, rollback transaction đọc, bỏ DTO/proof và thử lại **một lần**. Không lấy khóa tài nguyên mới sau journal. Hai lượt vẫn không ổn định → `409 resource_scope_changed`, không payload dữ liệu. Web nhận lỗi qua API error path và hướng dẫn tải lại trang.

Các SELECT ở READ COMMITTED không tự tạo snapshot nhất quán. Khóa bảo vệ quyền của rows/flags trong response; shared journal chặn auth/common revoke đã tuân thủ journal protocol; parent collection và citation advisory bảo vệ scope liên quan. So hai quan sát phát hiện churn không được các khóa này bao phủ (ví dụ thêm item mới làm count/page đổi). Không tuyên bố snapshot toàn thư viện, chống DBA ghi SQL ngoài protocol hoặc count bất biến khi có dữ liệu mới sau boundary.

`401 session_expired` + clear cookie khi exact caller/predicate/TTL mất hiệu lực; sources current role không Admin → `403 forbidden`; integrity/hold/pending hoặc resource lock timeout → `503 service_recovering`. Resource không còn quyền → `404 not_found`, kể cả mất trong lượt đọc lại; auth được kiểm lại trước trả lỗi đó. Khóa tài nguyên dùng timeout config `recovery.gate_lock_timeout_ms`, không giá trị timeout rải rác. Malformed/missing IDs vẫn 404; shape của mọi 200 giữ nguyên.

## Map từng route

Tất cả hàng dưới dùng hai proof cùng transaction, clock cuối sau lượt SQL cuối. Count/page luôn lọc SQL trước pagination, kể cả đọc lại.

| GET / function | Resource boundary | Gate và response |
| --- | --- | --- |
| `/library/items` / `list_items` | Item UUID của trang → journal; đọc lại count/page/flags | `scope mine/common/all`, `visible_items_clause`; `owned_by_me`/`can_edit` dùng Principal mới; fulltext/RAG riêng |
| `/library/items/count` / `count_items` | Không item lock → journal; hai count mới | SQL visible/scope; common revoke phải lấy journal exclusive trước commit |
| `/library/items/{item_id}` / `get_item` | Item → journal; đọc lại versions/files/jobs/flags | Metadata common listed; private owner; jobs chỉ private; common coverage chỉ active searchable version, original link chỉ `common_original_clause` |
| `/library/items/{item_id}/chunks` / `get_chunks` | Item → journal; item gate và row gate chạy lại | Private extraction trước index; common exact active version/file/generation/epoch, locator cũ |
| `/library/items/{item_id}/metadata-candidates` / `get_metadata_candidates` | Item → journal; item/row gate chạy lại | Candidate đúng item/version/file, metadata-only chung không mở text |
| `/library/items/{item_id}/coverage` / `get_coverage` | Item → journal; item/row gate chạy lại | Partial/missing và pages rỗng vẫn final caller; version/file đúng gate |
| `/library/items/{item_id}/references` / `get_references` | Item → citation shared → journal; reference/link/target mới | Row tuple gate, hash/matcher/tombstone freshness; bibliography chỉ quan hệ cites, không content evidence |
| `/collections` / `list_collections` | Owned collection UUID → journal; danh sách mới | Owner SQL, empty cũng proof; không Admin bypass |
| `/collections/{collection_id}/items` / `list_collection_items` | Item UUID trang → collection parent → journal | Owner + membership giao visible ACL trước count/page; membership không cấp text/file; fulltext/RAG riêng |
| `/discovery/topics` / `list_topics` | Journal; topics đọc lại | Auth hiện hành, sources metadata topic, không nội dung library |
| `/discovery/papers` / `list_papers` | Citation shared → journal; count/page/topics mới | Metadata-only, topic filter và tombstone trước count/page; không text retrieval/RAG |
| `/discovery/papers/{paper_id}/citing-items` / `citing_items` | Private item UUID trang → citation shared → journal | Private owner + fresh hash/matcher/link/tombstone trước distinct count/page; không thêm common |
| `/discovery/sources` / `source_status` | Current Admin journal proof → nguồn đọc lại → current Admin proof | DTO allowlist cũ; chỉ bool `has_resumption_token`, không token thô |

Item SHARE tương thích FK KEY SHARE của collection add, citation fence và audit. Collection writer khóa một parent rồi membership; không lấy item UPDATE sau parent. Common revoke/publish/worker khóa item trước các rows phụ/journal; chúng đợi reader item SHARE, reader không đi lấy User/session. Citation writer có thể giữ LINK rồi fence item KEY SHARE, tương thích item SHARE reader. Không nâng journal shared → exclusive; retry nhả toàn bộ khóa trước lượt tiếp. Khóa không được giữ qua storage/provider/network I/O.

Response/DTO đã được cho phép tại boundary có thể tiếp tục gửi sau dependency nhả khóa; quyền thu hồi sau đó không lấy lại bytes đã gửi. Membership/role/private deletion restore chưa có bảo vệ journal mới trong PR này.

## Kiểm chứng và fixture

Kết quả/source/node/JUnit, đối chứng và các lượt chuẩn bị được ghi ở inventory sau suite cuối. Account bootstrap/invitation/login và entry dependency dùng runtime LOGIN, receipt/head/predicate/gate/journal thật; chỉ cutover scoped ở nghiệp vụ. Nhóm pending thật riêng không patch cutover, không đổi mọi expectation nghiệp vụ thành 503.

Fixture resource được tạo bằng privileged setup trên schema thật; không raw seed account usable/proven. Common copy dùng approval/closure journal và publication thật với storage bytes tổng hợp trong bộ nhớ; **không là kiểm writer HTTP/storage** ngoài phạm vi. Các writer review/publication chưa chuyển runtime auth, nên setup dùng connection đặc quyền, không mở grant runtime. Test-only corruption rights/epoch/file/head dùng đặc quyền có ghi rõ, không thêm bypass sản phẩm. Trường hợp indexed search-only dựng index dưới quyền processing hợp lệ rồi sửa riêng permission bằng setup đặc quyền để kiểm read gate; không thay `pricing_verified` hay gọi provider.

Conda ras đã xác minh interpreter/pip. PostgreSQL standalone `ras-c2d2a-db-20261005`, label `ras.task=c2d2a-20261005`, port `127.0.0.1:58445`; explicit test admin URL/DB parts trước fixture import; runtime/recovery LOGIN, definer/migrator NOLOGIN. Runner chặn HTTPTransport sync/async và kiểm hash mọi API Python/SQL trước/sau suite. Credentials/journal/log/JUnit ngoài repo `%TEMP%/ras-c2d2a-20261005`. Không `.env`, query/nâng/restart dev, provider hoặc thay pricing/safety.

C2d-2b review/publication JSON GET, library/collection writes, upload/file/storage, journal writers, search/RAG/provider/worker, tổng HTTP/proxy/restore/drill và C3/C4 còn riêng. Không tuyên bố toàn C2d/T14 hoặc restore production an toàn.
