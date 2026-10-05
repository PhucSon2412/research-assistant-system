# Prompt coding C2d-2a: final caller cho library, collections và discovery

[Mục lục](../README.md) · [Quyết định phạm vi](../decisions.md#giao-c2d-2a-sau-merge-pr-38-2026-10-05) · [Helper/bàn giao C2d-1](../c2d1-final-caller-reads.md) · [Review PR #38](../reviews/pr38-c2d1-final-caller.md)

## 1. Nhiệm vụ và base

Bạn là coding agent chính. Triển khai **C2d-2a**: final caller và kiểm lại quyền tài nguyên ở **13 route GET JSON** của library, collections và discovery dưới đây. Làm code, kiểm thử, cập nhật docs/inventory, commit/push và mở PR tiếng Việt để review; **không merge**. Không dừng ở kế hoạch khi đã đủ điều kiện thực hiện.

PR #38 đã merge: `origin/main=d06664e53383071237c21e802f439fda348a1afd`, chứa `97c49d8`; source migration `0039`. Đây là chứng cứ khảo sát ngày 2026-10-05; fetch lại, kiểm ancestor/head thực tế trước khi tạo nhánh `feat/m6c2b2c2d2a-resource-json-reads`. Không suy revision DB dev từ source. Ưu tiên dùng schema/routine hiện có; nếu thật sự cần schema, giải thích lý do và thêm revision mới sau head thực tế, không sửa migration đã merge.

Giữ docs review/handoff/task của orchestrator chưa commit, gồm review PR #38; kiểm rồi đưa cùng PR, không reset/bỏ chúng. Lượt giao task chỉ sửa tài liệu, chưa triển khai hoặc chạy test C2d-2a.

## 2. Nguồn yêu cầu và tài liệu phải đọc

- `AGENTS.md`, [docs README](../README.md), [auth restore/T14/lock order](../auth-restore-safety.md), [quyền và dữ liệu](../access-and-data.md).
- [C2d-1](../c2d1-final-caller-reads.md), [caller inventory](../c2d1-caller-inventory.json), [inventory kiểm](../c2d1-test-inventory.json), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2c-2](../c2c2-invitation-runtime.md).
- Code `api/app/identity/final_caller.py`, `api/app/api/deps.py`, `api/app/auth_safety.py`, `api/app/authz.py`, routes `library.py`, `collections.py`, `discovery.py`, helpers citations/common-rights và models liên quan.
- Test C2d-1, `test_library_acl.py`, `test_collections.py`, `test_discovery.py`, `test_citations.py`, fixture common/index/read gates và `api/tests/conftest.py`.

C1/Origin/runtime trust boundary đã chốt theo ủy quyền, không xin lại. Helper C2d-1 đã merge và được review; không coi permission của paper thay bằng session proof hoặc ngược lại. Safety pending tới C4; C3 operator/legacy recovery/initial status/production delivery và C4 cutover/storage còn riêng.

## 3. Phạm vi chính xác

| Module | Route GET cần chuyển | Số |
| --- | --- | --- |
| Library | `/library/items`, `/library/items/count`, `/library/items/{item_id}`, `/library/items/{item_id}/chunks`, `/library/items/{item_id}/metadata-candidates`, `/library/items/{item_id}/coverage`, `/library/items/{item_id}/references` | 7 |
| Collections | `/collections`, `/collections/{collection_id}/items` | 2 |
| Discovery | `/discovery/topics`, `/discovery/papers`, `/discovery/papers/{paper_id}/citing-items`, `/discovery/sources` | 4 |

Chỉ `/discovery/sources` trong bảng này cần current Admin. Các route khác giữ permission hiện hành: User chỉ private của mình và common metadata/derived đúng gate; Admin không mặc nhiên đọc private của User; collection ownership/membership chỉ thu hẹp phạm vi, không cấp quyền paper.

Không chuyển `PATCH /library/items/{id}`, collection CRUD/membership writes, upload/intent/notice/download, review/publication metadata hoặc writer, chunk viewer thuộc `/search`, search/RAG/provider, worker hay delivery. **C2d-2b** sẽ chuyển JSON GET review/publication theo grant riêng; JSON writes và storage/provider tiếp tục tách. Không đặt auth guard vào formatter/helper dùng chung với writer nếu điều đó âm thầm đổi transaction/lock của route ngoài phạm vi.

## 4. Hợp đồng caller, quyền và thời điểm kiểm

1. Giữ entry principal/session/CSRF/Origin tại `api/app/api/deps.py`. Tái dùng `identity.final_caller` với exact session ID/User ID, clock dependency và idle config; không viết predicate auth đơn giản chỉ xét status/revoked_at. Không bypass quarantine/held, latest head/receipt, generation hoặc integrity/cutover.
2. `final_caller` giữ shared journal lock trên **transaction hiện tại**, không touch/issue/commit/rollback. Không dùng connection thứ hai hoặc proof từ transaction đã commit. Không lấy thêm User/session row lock sau item/collection/journal; không dùng dependency writer một User làm final resource guard.
3. Đọc role/profile hiện hành từ proof. Khi dựng `Principal` để chạy lại SQL ACL/flags, dùng đúng User/session đã xác thực và role mới; không chỉ check session còn live rồi tiếp tục dùng role Admin cũ để dựng `can_edit` hoặc đọc trạng thái sources.
4. Lọc quyền SQL **trước count, ordering và limit/offset**, kể cả ở lần query lại. Giữ scope `mine/common/all`, owner collection, private derivative clause, common fulltext/original tuple gates và discovery tombstone/fresh citation filters. Không lấy dữ liệu toàn hệ thống rồi lọc Python; không bỏ count/filter để tránh race.
5. Trước response thành công, kể cả `items=[]`, `total=0`, coverage rỗng hoặc không có references, phải có proof caller **clock mới sau công việc DB có thể chậm** và xác nhận các hàng/flags còn trong scope. Không dùng `get_now` đầu request hoặc ORM identity map cũ cho quyết định cuối. Nếu đọc DB thêm sau proof đầu, kiểm clock/caller lần cuối trước trả.
6. Auth mất hiệu lực → `401 session_expired` + clear cookie; session hợp lệ nhưng role không còn Admin ở `/sources` → `403 forbidden`; gate/integrity/pending lỗi → `503 service_recovering`. Resource không được phép/không tồn tại vẫn `404 not_found`; không biến auth lỗi thành response 200 rỗng hoặc lộ việc resource riêng tồn tại.
7. Các route chỉ đọc không tạo auth event, job, audit link/token, provider call hoặc commit business mới. Dùng transaction ngắn và DTO allowlist. Nếu helper phụ có pending ORM write/autoflush hoặc commit, phải nhận diện, flush/khóa đúng **trước** shared journal và kiểm lại sau commit; không để DML/FK lock ẩn xuất hiện sau final gate. Ưu tiên giữ read path không pending writes.

### Không chỉ thêm một call trước `return`

Các route có nhiều query cho item/page/count/file/job/flags. Nếu resource đã bị thu hồi trước quyết định cuối, không được trả DTO dựng từ snapshot cũ dù caller còn live. Đề xuất cách thực hiện: lấy các resource lock thật sự cần theo thứ tự hiện hành, final shared gate/caller, đọc lại bằng SQL ACL trên transaction này, rồi clock/caller cuối và trả DTO; có thể refactor read-only helpers theo đúng boundary.

Agent chọn cách cụ thể theo source nhưng phải ghi cho từng route: quyền nào được bảo vệ bằng shared journal, quyền nào cần item/collection/membership lock hoặc query lại, và điểm quyết định cuối. Không hứa nhiều SELECT ở READ COMMITTED tự tạo snapshot nhất quán. Với race thay đổi page/scope, dùng retry hữu hạn hoặc lỗi có kiểm soát được ghi contract; không trả rows/count/flags bị cấm, không retry vô hạn.

Nếu cần resource locks: thu thập đúng các hàng được phép trước, khóa ổn định theo UUID và theo thứ tự hệ thống (đối chiếu search/RAG có item trước collection/membership), rồi mới journal. Không khóa toàn thư viện để giữ count và không khóa item trái quyền; không phát sinh row lock mới sau journal. Chứng minh không deadlock với revoke/collection writer/worker. Không giữ journal lock khi storage/provider/network I/O; scope này không cần các I/O đó.

## 5. Giữ các quyền dữ liệu hiện có

| Nhánh | Kỳ vọng sau thay đổi |
| --- | --- |
| Library metadata | Common `listed` thấy được metadata; `pending/revoked` vẫn đóng ở route thường, private khác owner vẫn 404. `owned_by_me`, `can_edit`, fulltext/RAG flags đúng proof và gates hiện hành |
| Item detail | Version/file/job metadata đúng item; private owner thấy job như trước, không tự mở common jobs. Common `original_url` chỉ khi gate tải cho phép; không suy từ `indexed`/search quyền |
| Dữ liệu dẫn xuất | Private đúng owner, giữ quyền đọc kết quả extraction trước index như M2a/M2b. Common đúng active item/version/file/generation/rights epoch và fulltext gate; common `metadata_only` không mở text. Chunks/candidates/coverage/references từ version/file trái quyền không lọt ra |
| Coverage/references | Báo coverage partial/missing như cũ; raw bibliography và citation edge không là content evidence/đồng ý. Target bị tombstone hoặc link stale không hiển thị như target hiện hành |
| Collections | Chủ thấy collection và item có quyền; membership không cấp file/text/RAG. Count/page cùng SQL ACL; collection/membership đổi trước final không làm lọt item ngoài scope |
| Discovery | Metadata/link nguyên trạng; `metadata_only` không vào fulltext/RAG. `citing-items` chỉ private papers được phép của caller, không lộ count/item/reference của User khác hoặc tự thêm common |
| Discovery sources | Chỉ Admin còn live/current role; không thêm resumption token thô vào response hoặc log |

Giữ JSON shape, sorting, pagination và status code của request hợp lệ. Lỗi scope churn mới nếu cần phải có test/contract/UI fallback phù hợp; không đổi thuật toán citation matching hoặc bổ sung tính năng discovery.

## 6. Kiểm thử và chứng cứ

Chỉ PostgreSQL/runtime/recovery LOGIN và journal tổng hợp cô lập. Explicit `RAS_TEST_DATABASE_ADMIN_URL`/`RAS_DB__*` phải có **trước import fixture**, không fallback `.env`. Account/receipt/session qua fixture C2 đã chuyển, không raw seed User active rồi mock principal để né fence. Có thể seed resource/rights/derived rows bằng fixture setup có schema thật; không mở grant runtime hoặc thêm bypass production. Nếu dùng pipeline tạo dữ liệu, provider phải giả và settings chỉ riêng test trong bộ nhớ; không thay pricing flag vận hành.

| Nhóm | Kiểm tối thiểu |
| --- | --- |
| Inventory 13 route | Method/path/function và mọi nhánh 200, gồm empty/count/detail/partial/reference/metadata-only; chứng minh đã gọi final guard đúng boundary |
| Account sau dependency | Revoke/disable/generation cũ/quarantine/held/thiếu latest receipt hoặc journal hold/ahead/unconfirmed: không payload thành công ở các nhóm route. Predicate/head/integrity/SQL thật |
| Clock và current role | Clock tiến trong query/lock wait đúng biên idle/absolute → 401, thiếu một đơn vị trước biên vẫn hợp lệ. Helper không touch thêm; `/sources` stale Admin → 403, `can_edit` dùng role hiện hành |
| A/B/Admin | Hai User có file bytes/hash giống nhau; A không đọc private B qua list/count/detail/derivatives/collection/citing-items. Admin không đọc private A/B qua API thường; current Admin sources hợp lệ |
| Common gates | Listed metadata-only; indexed chỉ search; RAG/provider flag riêng; không download rights; sai file/version/epoch/generation/tombstone. Metadata, flags, derived DTO và original_url cùng tuân thủ quyền |
| Resource race | Revoke common trước final ở list/count/detail/derivatives: không text/link/flags stale. Collection bị xóa hoặc membership đổi; discovery tombstone/stale citation link; phân biệt auth proof và resource proof |
| Count/page | Private trái quyền hoặc common revoked khớp candidate không chiếm slot `limit=1` hoặc vào count; collection và discovery reverse count cùng scope. Không bỏ lọc trước pagination |
| Khóa | Barrier/Event và quan sát DB wait cho hai thứ tự reader/writer thắng; không User lock sau journal hoặc lock toàn scope, không transaction leak/deadlock |
| Gate pending thật | Không scoped cutover: 13 route bảo vệ 503, health sống/ready 503. Tách khỏi HTTP nghiệp vụ có scoped cutover, không sửa expectation nghiệp vụ thành 503 hàng loạt |
| Hồi quy/riêng tư | C2d-1; library/collections/discovery/citation contract liên quan; raw API/log/audit/journal không lộ secret hoặc private marker của người khác. ORM/schema nếu đổi model/routine |

Test trong task này phải chứng minh core protections của 13 route, không hoãn chúng tới C4. Port fixture của các test contract có liên quan để dùng auth runtime/receipt thật, giữ assertions nghiệp vụ cũ. Nếu full file có writes ngoài scope chưa chuyển, tách inventory read đã chạy và writer not-run; không thêm bypass để cho writer cũ qua hoặc báo cả file pass khi chỉ chạy một phần.

Thực hiện đối chứng theo rủi ro: bỏ final guard, current Admin check, owner/common tuple ACL, filter trước count/page hoặc recheck resource phải làm test liên quan fail; khôi phục bytes trước suite cuối. Không sửa code/migration/mutant khi suite đang chạy. Báo source SHA, node IDs/collect, JUnit đủ nhóm không thiếu/trùng, fail/error/skip/not-run và các lỗi lượt chuẩn bị. Không cộng nhiều lượt trùng thành full suite; full product HTTP/proxy/storage/provider/drill/cutover vẫn riêng. Web không đổi thì ghi not-run; nếu đổi contract/UI, chạy checks tương ứng.

## 7. Docs, bàn giao và điều kiện hoàn thành

- Tạo `docs/c2d2a-resource-json-reads.md` và inventory kiểm theo convention hiện có. Cập nhật caller inventory source/line/final status cho đúng 13 route và ghi các route/helper dùng chung còn ngoài scope; không chuyển tất cả group library/collection/discovery thành hoàn tất nếu còn writers.
- Ghi read/lock/clock/resource ACL/count/page boundary từng nhóm và các quyết định triển khai/retry/lỗi mới. C2d-2b review/publication GET, JSON writes, file/upload, journal writes, search/RAG/provider và tổng hồi quy còn riêng.
- Đồng bộ README/docs README, roadmap, decisions, testing/dev-setup khi có chứng cứ mới; AGENTS chỉ dẫn contract/lệnh đã kiểm. Kiểm liên kết/control characters và `git diff --check`.
- Python/cài thư viện chỉ dùng `C:\Users\ADMIN\.conda\envs\ras\python.exe`, xác minh interpreter/pip trước; không venv. Không đọc/sửa `.env`, truy vấn/nâng/restart dev hoặc gọi OpenRouter, không đổi `pricing_verified` hay safety ready. C1–C3 pending/unhealthy là hành vi đã thiết kế, không sửa thành ready để demo.
- Thử container/project/ports riêng; kiểm resolved paths và ownership labels trước dọn, không xóa volume/journal dev. Không để secret/artifact/dataset riêng trong repo.
- Commit/push, mở PR tiếng Việt cho chủ dự án review; không AI author/committer/trailer/dòng attribution tự động, không merge. Báo base/head/branch/commit/file, kết quả thực chạy và not-run, lỗi các lượt trước, tài nguyên đã dọn và phạm vi chưa hoàn tất.

**Hoàn thành:** đủ 13 GET giữ contract, final caller với clock/head/receipt/gate đúng transaction, SQL ACL trước count/page và recheck đúng resource boundary, role/flags/derived/version/file không stale; tests runtime/race/pending/đối chứng phù hợp đạt; inventory/docs nhất quán. Safety vẫn pending tới C4. Bytes/response đã qua final boundary có thể tiếp tục gửi sau nhả khóa, không hứa thu hồi hồi tố hoặc restore production an toàn.
