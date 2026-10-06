# Prompt C2d-3: library và collection JSON writes

**Trạng thái:** đã triển khai tại `12ffccc`, đã kiểm, đã merge PR #41 `5100ff6` (2026-10-06). [Bàn giao](../c2d3-library-collection-json-writes.md) · [Inventory](../c2d3-test-inventory.json). Task tiếp [sửa final read clock](c2d-final-read-clock.md) đã giao, chưa triển khai. Yêu cầu gốc bên dưới được giữ; safety pending tới C4.

[Mục lục](../README.md) · [Quyết định phạm vi](../decisions.md#giao-c2d-3-sau-merge-pr-40-2026-10-05) · [Roadmap](../roadmap.md) · [Caller inventory](../c2d1-caller-inventory.json)

Bạn là coding agent chính. Triển khai **C2d-3 — kiểm actor và quyền tại commit, kiểm lại phản hồi sau commit cho 6 JSON writer library/collections**. Hoàn thành code, test, docs, commit/push và mở PR **tiếng Việt** để review; không merge.

## 1. Base và tài liệu cần đọc

- Fetch lại; xác minh PR #40 đã merge và base chứa head `11da7316a6be27317c2d6d27c8725aafa51533b2`. Lượt giao task xác minh `origin/main = 833db7cc1b3a4df6b683f72820fca83a17c19eb7`, merge PR #40.
- Head migration mã nguồn hiện `0039`, **không phải chứng cứ revision DB dev**. Nếu main tiến thêm, dùng head thực tế và báo khác biệt.
- Tạo nhánh `feat/m6c2b2c2d3-library-collection-json-writes` từ main đã xác minh. Workspace có docs review/handoff chưa commit: kiểm diff, giữ nguyên nội dung hợp lệ và đưa vào PR này; không reset/clean hoặc bỏ file chưa tracked.
- Đọc [AGENTS.md](../../AGENTS.md), [docs index](../README.md), [quyền và dữ liệu](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2c-2](../c2c2-invitation-runtime.md), [C2d-1](../c2d1-final-caller-reads.md), [C2d-2a](../c2d2a-resource-json-reads.md), [C2d-2b](../c2d2b-review-publication-json-reads.md), [review PR #40](../reviews/pr40-c2d2b-review-publication-reads.md) và [kiểm thử](../testing.md).
- Đọc code thật: `api/deps.py`, `api/resource_reads.py`, `identity/final_caller.py`, `identity/predicates.py`, `identity/session_commit.py`, `auth_safety.py`, `authz.py`, hai route library/collections, model/FK và caller hiện có. Các path ngắn này tương đối với `api/app/`.
- Trước sửa code, ghi tóm tắt phạm vi, lock map và mâu thuẫn docs/code. Quyết định nền C1/Origin đã chốt; không xin lại.

**Safety vẫn pending tới C4.** `docker compose up --wait` chưa ready do pending không phải lý do mở gate hoặc sửa dev trong task này.

## 2. Phạm vi chính xác

| Method/path | Handler | Quyền và hành vi phải giữ |
| --- | --- | --- |
| `PATCH /library/items/{item_id}` | `update_item` | Owner sửa private của mình; current Admin sửa common ordinary-visible. Foreign private/missing cùng `404`; User thấy common nhưng không được sửa vẫn `403` |
| `POST /collections` | `create_collection` | Collection riêng của caller; trả `201` và DTO hiện hành |
| `PATCH /collections/{collection_id}` | `rename_collection` | Đúng owner; foreign/missing cùng `404`; trả `200` |
| `DELETE /collections/{collection_id}` | `delete_collection` | Xóa membership tường minh rồi collection cùng transaction; giữ paper/version/file, trả `204` |
| `POST /collections/{collection_id}/items` | `add_collection_item` | Owner collection + item ordinary-visible; add lặp giữ hợp đồng `ON CONFLICT DO NOTHING`, trả `201` |
| `DELETE /collections/{collection_id}/items/{item_id}` | `remove_collection_item` | Owner collection; membership tồn tại thì xóa, không tồn tại `404`; trả `204` |

- Admin không được sửa private của User hoặc collection của người khác. Role Admin không cấp quyền owner.
- Common `listed/metadata_only` có thể là membership; membership không cấp file/full-text/provider/RAG. `pending/revoked` không được thêm qua ordinary visibility.
- **Remove kiểm owner collection và membership, không bắt item còn visible.** Owner phải gỡ được membership cũ tới item nay bị ẩn/thu hồi; không trả metadata/file của item đó. Đây là hợp đồng hiện có cần giữ.
- Không đổi ownership/visibility, publication/rights/index/generation, ranking, ingest, upload, file download hoặc review grant.
- GET đã chuyển trong C2d-1/2a/2b phải giữ hợp đồng. Không nhét guard ghi vào formatter dùng chung hoặc thay read helper thành writer.
- Phạm vi web chỉ là map/test thông báo lỗi mới nếu cần để diễn đạt đúng commit không rõ kết quả; không redesign UI.
- Không bổ sung journal cho membership/xóa collection, idempotency tổng quát, private deletion, provider calls, C3 operator hay C4 cutover. Restore membership cũ vẫn là giới hạn riêng.
- Ưu tiên không migration; nếu chứng minh cần, thêm revision mới sau head thực tế, không sửa revision đã merge hoặc mở quyền runtime.

## 3. Boundary transaction ghi

### Actor và khóa

Chọn helper ghi riêng hoặc tái dùng phần thích hợp của writer hiện có. Helper phải nhận explicit session ID/User ID/CSRF context và callable clock; không coi role trong `Principal` lúc dependency là role cuối.

1. Entry check safety/integrity trước khi giữ resource/journal locks. Dependency auth/CSRF/Origin thật vẫn hoạt động; không tạo principal từ input client.
2. Dùng routine runtime `lock_users` khóa **caller User NO KEY UPDATE trước resource**, chỉ caller cần thiết. Không khóa User sau item/collection hoặc journal. Kiểm lại actor sau wait; không cấp quyền raw protected write hoặc dùng owner/migrator như runtime.
3. Resource theo thứ tự tương thích reader và writer đã có: **User → permitted item (nếu cần) → owned collection → DML/FK/audit → journal exclusive**; UUID order khi nhiều hàng. Item/collection lock SELECT giữ SQL ACL. Với add membership, item lock/gate phải đi trước collection lock; không tạo collection → item ngược `resource_json`.
4. Rename/remove/delete collection không cần khóa hàng item của toàn bộ membership; không khóa foreign private chỉ vì collection có một liên kết cũ. Kiểm FK/delete và caller search/RAG/cleanup thật trước chọn lock mode.
5. Flush toàn bộ DML/FK/audit cần thiết **trước** journal: collection owner FK, membership item/collection FK, audit actor FK có thể gây khóa ngầm. Sau journal không flush mới có thể lấy khóa hàng chưa có, không thêm resource/User/session locks.
6. Lấy journal **exclusive trực tiếp** trên cùng transaction qua hợp đồng `require_final(..., writer=True)`; không shared rồi upgrade. `final_caller`/read DTO proof cũ không phải quyền commit. Không gọi entry integrity bằng connection thứ hai khi đang giữ journal.
7. Kiểm gate thật, exact session/User, latest confirmed head/receipt, credential generation, account active/proven, session chưa revoke, idle/absolute TTL, CSRF binding và **role hiện tại**. Kiểm lại SQL ownership/visibility/editability trên tài nguyên đang khóa. Clock phải mới sau các thao tác có thể chậm, ngay trước commit; không dùng `now` lúc request bắt đầu và không gia hạn idle trong helper này.
8. Commit chỉ khi mọi điều kiện cùng transaction hợp lệ. Guard từ chối trước commit phải rollback toàn bộ thay đổi nghiệp vụ, membership và mutation audit; auth activity ở dependency là transaction riêng theo hợp đồng cũ.

Không append auth event chỉ để sửa title/name/membership. Không dùng `commit_auth` như shortcut nếu chưa chứng minh hợp đồng actor/error/empty-batch/postcheck phù hợp với resource write; không thay semantics của auth commit phục vụ caller khác. Không I/O storage/provider hoặc chờ network khi giữ row/journal locks.

### Sau commit và lỗi

- Proof trước commit đã hết hiệu lực. DTO thành công phải được dựng từ đọc lại DB/current caller trong **transaction mới**, với clock và resource gate đúng quyền hiện tại; không trả ORM row/cache/profile đã chụp trước commit.
- Có thể tái dùng boundary GET phù hợp **sau** commit với clean Session; phải kiểm đủ quyền của response, không dùng shared helper trước commit ghi. Title/full-text/RAG flags của library theo current gates; DTO collection vẫn owner-only.
- Với `201` add membership, kiểm caller và membership/resource hiện hành trước phát ID. Với `204` delete/remove, kiểm caller mới nhưng không yêu cầu hàng vừa xóa còn tồn tại hoặc dựng `404` giả vì deletion đã thành công.
- Nếu caller mất quyền sau commit, không phát DTO/thông tin trái quyền. **Mutation đã commit không bị rollback bởi lỗi postcheck.** Ghi rõ trong docs/test; không hứa "chưa có gì được ghi nhận", không tự thực hiện lại mutation.
- Khi `db.commit()` ném lỗi hoặc kết quả không rõ, trả lỗi có kiểm soát, đề nghị mã `503 resource_write_outcome_uncertain`; không báo success, không suy rollback đã chắc chắn, không tự replay title/collection vào DB từ journal. Kiểm cả ca commit đã xảy ra nhưng client của DB mất phản hồi.
- Trước commit, lỗi guard giữ mã auth/ACL hiện hành (`401/403/404`), gate `503 service_recovering`; lỗi lock timeout/DB không lộ SQL/URL/secret. Scope đổi sau commit có thể dùng `409 resource_scope_changed`; mô tả đúng giai đoạn.
- Không tự retry transaction nếu commit đã bắt đầu mà outcome không chắc. Repeated add giữ dedupe cũ; create collection chưa có exactly-once/idempotency tổng quát.
- Nếu thêm message UI, nêu "chưa xác nhận kết quả, kiểm tra lại sau khi kết nối/đăng nhập" thay vì khẳng định chưa lưu. Không tự submit lại.

Handoff phải có bảng từng route: entry/current actor, resource lock/gate, flush, exclusive guard, commit và postcommit DTO/`204`, lỗi/transaction cleanup.

## 4. Kiểm chứng bắt buộc

Dùng PostgreSQL thật, role runtime thật, journal/integrity/head/receipt và predicate thật. Tài khoản qua bootstrap/invitation/login runtime; fixture đặc quyền chỉ dựng resource/corruption trong DB thử. Không biến owner/migrator thành user của HTTP.

Chỉ có thể scoped patch **cutover pending** trong fixture nghiệp vụ để kiểm writer thật trước C4; không mock actor, receipt, head, journal/integrity, session/CSRF, resource gate hoặc clock mới để làm pass. Giữ nhóm pending HTTP **không patch**, đủ 6 route, health sống/readiness đóng và không mutation.

| Nhóm | Bằng chứng cần có |
| --- | --- |
| Success/ACL cả 6 route | A/B/Admin; exact owner, foreign/missing cùng mã/body, current Admin sửa common nhưng không private của User; private/common metadata-only membership; add lặp; remove membership ẩn; delete collection không đụng item/version/file |
| Auth cuối trước commit | Dependency đã qua rồi mới đổi generation/head/receipt, revoke session/disable, current role hoặc CSRF binding ở thời điểm hợp pháp; guard từ chối và business rows/audit không đổi |
| Clock | Sau row/journal wait, sau SQL/DML; idle/absolute đúng biên và sát biên; không touch idle ở helper cuối |
| Chốt lỗi và response | Known precommit failure rollback; commit-success-then-error không báo success; caller/resource mất quyền sau known commit không nhận DTO, mutation đã commit vẫn còn; `204` không body/proof mới |
| Resource và FK races | Hai thứ tự writer/reader, add với revoke/cleanup, delete collection với add/remove/list, auth closure với resource writer; Event + PostgreSQL wait có deadline, không chỉ sleep/random |
| Reader hồi quy | C2d-1/2a/2b boundary, clock/current role và SQL trước count/page giữ nguyên; fresh library flags/collection response; no lock upgrade hoặc User lock sau journal |
| Transaction/pool | Sau success, denial, postcommit error và lock/DB error không còn transaction/checked-out connection do handler; kiểm Engine API thật, không chỉ fixture Engine |
| Gate/privilege/privacy | Pending thật cho 6 route; hold/bad/unconfirmed journal chặn commit; runtime grants không rộng thêm; log/audit không raw cookie/token/verifier/name/title/text/secret |

Trong test race, auth/revoke có thể phải chờ row lock mà writer giữ: kiểm **hai thứ tự hợp pháp**, không đòi transaction khác commit khi khóa đang chặn nó. Postcommit error có thể là `401` dù mutation đã thành công; không hạ assertion để né.

Kiểm đối chứng tối thiểu các lớp: bỏ guard trước commit; role snapshot; clock cũ; bỏ exact session/User/generation; dùng proof cũ sau commit; trả stale ORM response; đảo add collection → item hoặc flush FK sau journal. Chọn phép kiểm phù hợp thực tế, ghi node/assertion/error; không tính mutant làm pass. Khôi phục đúng bytes/hash trước suite cuối, không sửa source/migration/mutant khi suite đang chạy.

- Port các assertion CRUD cũ sang runtime fixture phù hợp; không xóa test nghiệp vụ hoặc sửa expectation thành `503` chỉ vì pending. Chỉ tách HTTP cần C4 nếu có lý do và ghi inventory cụ thể.
- Chạy nhóm writer mới + CRUD library/collections liên quan, C2a login/session + C2b actor, C2d-1/2a/2b và ORM/schema trong phạm vi ảnh hưởng. API full inventory nếu chưa chạy thì ghi đúng not-run, không cộng test lịch sử.
- Nếu không đổi web, ghi web not-run; nếu thêm message, chạy web tests/typecheck/build tương ứng. Proxy/browser tổng hợp ở C4; không dùng browser smoke giả làm chứng cứ đã mở cutover.
- Inventory phải có collect unique nodes, selected/pass/fail/error/skip/not-run, JUnit không thiếu/trùng, source SHA/dirty, hash source trước/sau, các lượt lỗi/dừng riêng. Chạy tuần tự, không sửa source khi test còn chạy.

## 5. Môi trường và docs bàn giao

- Python/cài thư viện chỉ `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước chạy, không venv.
- Chỉ PostgreSQL/container/volume/port/journal và credentials tổng hợp **cô lập**. Explicit test admin URL/DB parts trước import fixture; không fallback đọc `.env`.
- Không đọc/sửa `.env`, restart/migrate/query DB dev, động vào volume/journal dev, gọi provider thật hoặc đổi `pricing_verified`. Metadata container để chứng minh dev không đổi có thể đọc.
- Dọn đúng tài nguyên thử sau kiểm ownership label/name/ID/mount/consumer; giữ JUnit/log/manifest ngoài repo, không commit secret/journal/dump/artifact.
- Tạo `docs/c2d3-library-collection-json-writes.md` và `docs/c2d3-test-inventory.json`. Cập nhật caller inventory chỉ đánh dấu route đã chuyển thực tế; không đổi GET thành "unimplemented" hoặc coi tất cả C2d hoàn tất.
- Cập nhật docs index/decisions/roadmap/testing/dev-setup/access/auth design/AGENTS theo hành vi thật. PR #40 đã merge, docs review chưa commit được giữ trong PR này; task C2d-3 ghi đang review khi mở PR. Giữ source/dev revision và lịch sử test riêng.
- AGENTS ngắn ở phần thêm mới: liên kết handoff và lock/commit invariant cần cho caller sau; không biến đề xuất chưa triển khai thành luật đã có.
- Kiểm liên kết nội bộ/control characters và `git diff --check`. Không ghi lệnh chưa chạy.
- Commit/push, mở PR tiếng Việt, không AI author/committer/trailer/attribution; không merge.

## 6. Tiêu chí hoàn thành và báo cáo

1. Đúng 6 writer có final actor/current role/resource/clock trước commit trên transaction ghi, không shared-to-exclusive upgrade hoặc FK lock ngược.
2. Known commit có postcommit auth/resource proof mới cho payload/`204`; commit uncertain và lỗi postcheck không báo sai rằng mutation chưa xảy ra.
3. Runtime tests, races, pending thật, privacy/ACL, transaction cleanup và hồi quy liên quan đạt; inventory rõ phần chưa chạy.
4. Source migration/grants/GET contracts giữ nguyên hoặc mọi thay đổi thêm có bằng chứng/lý do; docs/report nhất quán.
5. Báo base/head/branch/PR, file thay đổi, kết quả từng lượt, đối chứng, giới hạn và cleanup. **Safety vẫn pending tới C4**; file/upload, review/publication writers, search/RAG provider callers, C3/C4 còn riêng.

Không tuyên bố toàn auth/restore/production đã an toàn hoặc toàn API xanh từ một selection. Nếu gặp mâu thuẫn làm đổi quyền/receipt/recovery model, ghi bằng chứng và báo trước khi triển khai phần phụ thuộc; các lựa chọn helper/test trong phạm vi trên tự giải quyết.
