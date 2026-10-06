# C2d-2b: final caller ở 9 GET review/publication JSON

**Cập nhật bản sửa 2026-10-06:** [bàn giao sửa final read clock](c2d-final-read-clock.md) lấy clock/kiểm idle và absolute sau chính SQL predicate cuối; checked_at dùng mốc mới. Giữ ACL/role/gate/locks và semantics known commit. Các số đo bên dưới là lịch sử; kết quả mới ở handoff/inventory riêng. Safety pending tới C4.

**Review PR #40 (`11da731`): 261/261 test chọn độc lập pass** — 260 C2d-2b + 1 ORM/schema; không phát hiện điểm chặn trong phạm vi 9 GET. PR #40 đã merge `833db7c`, safety pending tới C4. [Báo cáo review](reviews/pr40-c2d2b-review-publication-reads.md). Kết quả agent và reviewer giữ riêng theo source.

[Mục lục](README.md) · [Task](tasks/m6c2b2c2d2b-review-publication-json-reads.md) · [Inventory kiểm](c2d2b-test-inventory.json) · [Caller inventory](c2d1-caller-inventory.json)

Base fetch `f229141e6db46f7df663cb1d14335530a4d5e9dd`, đúng merge PR #39 và ancestor `e89815d`/`5d291c0`. Nhánh `feat/m6c2b2c2d2b-review-publication-json-reads`. Giữ docs orchestrator chưa commit trong PR. Không migration, head source `0039`; không suy revision DB dev. Triển khai đã merge PR #40 tại `833db7c`; agent không tự merge. **Safety pending tới C4**.

## Boundary và hợp đồng

Chín wrapper GET gọi riêng [`review_publication_json`](../api/app/api/review_publication_reads.py). `_read_*` dựng DTO mới; các formatter `_owner_out`, `_admin_out`, `_out`, `_full_out`, `_copy_out` vẫn dùng được bởi POST và không mang auth guard mới. Entry session/Origin/CSRF không đổi. Note/docstring listed/revoked được sửa theo search/RAG gates hiện hành và Admin history; các POST dùng chung NOTES cũng nhận câu chữ mới, **không đổi writer transaction/guard**.

1. Candidate lọc SQL trước count/order/limit/offset; chỉ dùng tìm UUID trang, không trả client.
2. Review: request SHARE theo UUID, rồi private item SHARE theo UUID; owner lock dùng requester + private owner SQL, Admin request dùng `review_history_admin_clause`, private item lock chỉ qua exact `review_grant_clause`. Request closed/inactive khóa request mà không khóa private item. Common: item SHARE theo pending hoặc reviewed-copy Admin clause; không dùng ordinary visible clause.
3. Exact session/User → `final_caller` trên cùng transaction, journal shared sau mọi resource lock. Dựng Principal/role/profile mới; cả 6 GET Admin đòi current Admin. Không User/session row lock sau resource/journal, không nâng shared lên exclusive.
4. Expire identity map + no autoflush; đọc lại toàn DTO/count/page/gates hai lần dưới boundary; final proof/clock sau mọi SQL/row wait. Common GET tính overdue bằng clock sau auxiliary SQL. Mọi nhánh notice/empty/thin history cũng đi qua proof.
5. Scope/UUID/count/DTO/profile đổi: rollback transaction đọc, bỏ mọi DTO/proof, thử lại một lần. Không lấy resource lock thêm sau journal. Còn churn → `409 resource_scope_changed`; web đã có thông điệp tải lại trang, không đổi UI.

Caller mất hiệu lực → `401 session_expired` + clear cookie; caller live mất Admin → `403 forbidden`; hold/journal/integrity/pending hoặc lock timeout → `503 service_recovering`. Timeout lấy config `recovery.gate_lock_timeout_ms`. Resource thiếu/mất quyền → 404, nhưng kiểm caller trước lỗi resource muộn. UUID string sai giữ 404; query validation giữ contract FastAPI hiện có. Không business DML/auth event/audit/job/commit mới; activity chỉ ở dependency hiện hành.

Admin workflow count vẫn gồm request open không có grant và closed; history clause chỉ cấp thin status/time/reason/publication note, không cấp private payload. Admin grant phải khớp requester/owner, private item, request open, version/file/checksum/epoch. Grant mất trước boundary không trả title/file/version/email/declaration/original URL cũ. Approved detail có pending_publication_id theo contract; recovery closure không phải approval và không dựng common copy/rights. Owner vẫn đọc lịch sử của mình, lời khai `verified=false`, không nhận evidence/rights của Admin.

## Map từng route

Mọi hàng dùng SQL ACL ở candidate và các lượt đọc lại, hai proof cùng transaction, clock cuối sau SQL. Khóa giữ đến transaction route đóng; response đã được phép có thể tiếp tục truyền sau nhả khóa, bytes đã gửi không thu hồi được.

| GET / handler | SQL trước count/page và resource locks | DTO phụ/quyền được giữ |
| --- | --- | --- |
| `/review-requests/notice` / `notice` | Không resource rows → journal | Config notice; empty resource scope vẫn final caller |
| `/review-requests` / `list_own` | `_owned_rows`: requester + private owner; filter item; request UUID trang → private item UUID → journal | Owner target/declaration/closure; không khóa toàn count scope |
| `/review-requests/{request_id}` / `get_own` | Cùng owner SQL; request → private item → journal | History closed còn được đọc; không foreign private hoặc rights evidence |
| `/admin/review-requests` / `admin_list` | Current Admin history SQL + open/closed trước count/page; request trang → chỉ item còn exact grant → journal | Workflow count giữ closed/inactive; private tuple/declaration/requester email/URL chỉ qua grant |
| `/admin/review-requests/{request_id}` / `admin_get` | History SQL theo ID; request → chỉ private item còn grant → journal | Thin closed/inactive 200; approved pending ID đọc lại từ AccessRights của origin request |
| `/admin/pending-publications` / `list_pending` | `pending_publication_clause` trước count/page; common item trang → journal | Metadata/rights/reviewer/version/file pending; không byte/text/provider |
| `/admin/pending-publications/{item_id}` / `get_pending` | Pending clause theo ID; common item → journal | Pending → listed/revoked trước boundary thành 404 |
| `/admin/common-copies` / `list_copies` | `review_copy_admin_clause` + state trước count/page; common item trang → journal | History pending/listed/revoked, rights/publication/job/generation/tombstone/retention/duplicate hint mới |
| `/admin/common-copies/{item_id}` / `get_copy` | Reviewed-copy clause theo ID; common item → journal | Giữ revoked 200; cleanup trả metadata còn theo retention, không rehydrate từ private/audit/journal/cache |

Review locks cùng chiều withdraw/reject request UPDATE → journal và approve request UPDATE → private item SHARE → journal. Common publish/revoke/index/worker giữ item trước rights/job/generation; reader item SHARE bảo vệ tuple và flags trước các writer này. Cleanup giữ tombstone → item: reader **không khóa tombstone**, nên không tạo vòng item → tombstone. Không khóa User/reviewer/FK hoặc mọi item để giữ count. Read boundary không I/O storage/provider/network.

Common auxiliary queries đọc publication, latest job/generation, tombstone, retention và reviewer email dưới item lock và được expire/requery. Cleanup scrub/retention ở chính item; duplicate published ID là hint quan sát của scope khác, không là permission hay reservation để publish. Hai quan sát READ COMMITTED phát hiện churn, **không là snapshot toàn hệ thống**: thêm item/request mới có thể làm count/page đổi sau boundary, publish của một duplicate ngoài trang có thể làm hint đổi. Writer vẫn kiểm lại gate/duplicate khi thao tác. Không hứa chống DBA sửa ngoài protocol hoặc hoàn tất mọi writer auth.

`original_download=open` lấy chính `common_original_rows/common_original_clause`; `fulltext_available/index.search_open` lấy common fulltext gate; `index.rag_open` đòi external processing. Gate review/pending/history không mở byte/text/provider. Publication listed chỉ là metadata; indexed không tự cấp search/RAG/download.

## Kiểm chứng

**Suite cuối tại code `749a31d`: 734/734 pass**, 0 failure/error/skip, 1458.049 s; collect 2213 unique nodes, **1479 not-run**, 3 warning hiện có. Gồm C2d-2b 260 (9 pending HTTP thật), C2d-2a 301 (13 GET), C2d-1 93, C2a login/session 68, library GET 9, collection GET 1, discovery GET 1 và ORM/schema 1. JUnit khớp chính xác 734 node đã chọn, không thiếu/trùng; SHA-256 215 API Python/SQL file không đổi trong suite.

Kiểm bổ sung riêng **9/9 pass**, 21.287 s, không cộng vào 734: plugin ngoài repo quan sát transaction final inactive sau response và `app.db.get_engine().pool.checkedout() == 0` cho đủ 9 GET. Assertion pool trong success test gốc thuộc fixture Engine; probe này kiểm chính Engine HTTP API.

**9/9 đối chứng** đều có đúng 1 assertion failure, 0 error/skip và source khôi phục đúng byte/hash trước suite cuối; không tính là pass thêm. Lượt chuẩn bị riêng có lỗi resource fixture/storage/observer; lượt 260/260 trước commit cũng chồng nhau, giữ trong inventory và không cộng vào suite cuối. Không có lượt test bị dừng.

Kiểm tĩnh **54 Markdown, 744 liên kết nội bộ, 0 lỗi/control characters**; caller inventory AST **63/63**, 13 boundary C2d-2a và đúng 9 boundary C2d-2b (6 current Admin). Trạng thái mọi writer/original stream giữ riêng, chỉ cập nhật line theo source. Writer/storage/shared formatter AST giữ nguyên so base; compileall, head source `0039` và `git diff --check` đạt.

Fixture tạo account/receipts/session bằng runtime bootstrap/invitation/login thật; entry và final helpers, predicate/head/receipt/journal/clock/gate thật. Chỉ fixture nghiệp vụ thay cutover; **9 pending HTTP** giữ gate thật và health 200/ready 503. Resource setup/tuple corruption dùng đặc quyền riêng trên DB thử; final-caller matrix dùng private resource fixture của Admin, ma trận A/B dùng User invitation/login thật với cùng hash và target riêng. Closure/approval/publication/cleanup race dùng helper journal thật, originals tổng hợp trong bộ nhớ; không tính là kiểm writer HTTP hay MinIO/stream.

Các đối chứng bỏ caller/current Admin/clock/grant state/checksum/epoch/SQL owner trước pagination/resource recheck, hoặc dùng ordinary visible lock, phải thất bại ở assertions. Khôi phục byte/hash trước suite cuối; không đổi source/migration/mutant khi suite chạy. Inventory giữ từng node/JUnit/source, lượt chuẩn bị/lỗi observer/storage fixture riêng; không cộng các lượt chồng nhau thành full suite.

Conda ras đã xác minh interpreter/pip, không cài thêm hoặc tạo venv. PostgreSQL standalone `ras-c2d2b-db-20261005`, label `ras.task=c2d2b-20261005`, bind `127.0.0.1:58446`; runtime/recovery LOGIN và definer/migrator NOLOGIN. Credentials/admin URL/DB parts đặt explicit trước fixture import; runner chặn HTTPTransport sync/async và so hash API Python/SQL. Journal/log/JUnit/scripts ở `%TEMP%/ras-c2d2b-20261005`, ngoài repo. Không đọc/sửa `.env`, query/nâng/restart dev, provider, pricing flag hoặc safety ready. Đã kiểm tuyệt đối đường dẫn/ID/label/port và volume chỉ thuộc container thử trước khi dọn; xóa container cô lập, một anonymous volume và hai file credentials/postgres.env tổng hợp. Chín container dev giữ nguyên ID/image/StartedAt qua snapshot metadata chỉ đọc; không query DB dev. Evidence/JUnit/log/scripts và journal tổng hợp giữ ngoài repo.

Web không đổi: tests/typecheck/build/browser not-run. Các file test review/approval/publication/common-original/closure cũ chứa writer/storage chưa chuyển nên whole file not-run; assertions JSON liên quan được kiểm trong nhóm mới với runtime auth. Original review stream, POST/writers, upload/file/storage, search/RAG/provider/worker, full product HTTP/proxy/backup restore/drill và C3/C4 còn riêng. Không tuyên bố toàn C2d/T14 hay restore production/mất điện an toàn.
