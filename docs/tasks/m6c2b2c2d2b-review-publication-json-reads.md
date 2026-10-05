# Prompt coding C2d-2b: final caller cho review/publication JSON reads

**Trạng thái triển khai:** trên nhánh review, chưa merge; xem [bàn giao](../c2d2b-review-publication-json-reads.md) và [inventory](../c2d2b-test-inventory.json). Prompt/lượt giao dưới đây giữ làm lịch sử phạm vi; safety pending tới C4.

[Mục lục](../README.md) · [Quyết định phạm vi](../decisions.md#giao-c2d-2b-sau-merge-pr-39-2026-10-05) · [Bàn giao C2d-2a](../c2d2a-resource-json-reads.md) · [Review PR #39](../reviews/pr39-c2d2a-resource-reads.md)

## 1. Nhiệm vụ và base

Bạn là coding agent chính. Triển khai **C2d-2b**: final caller, current Admin và kiểm lại quyền tài nguyên ở **9 GET JSON review/publication** dưới đây. Làm code, kiểm thử, docs/inventory, commit/push và mở PR **tiếng Việt** để chủ dự án review; **không merge**. Không dừng ở kế hoạch khi đủ điều kiện thực hiện.

PR #39 đã merge: `origin/main=f229141e6db46f7df663cb1d14335530a4d5e9dd`, chứa `e89815d` và code `5d291c0`; head migration source `0039`. Đây là chứng cứ khảo sát 2026-10-05; fetch lại, kiểm ancestor/head thực tế, rồi tạo `feat/m6c2b2c2d2b-review-publication-json-reads` từ main mới nhất. Không suy revision DB dev. Ưu tiên schema/routine hiện có; nếu cần schema, giải thích và thêm revision sau head thực tế, không sửa migration đã merge.

Giữ và đưa docs orchestrator chưa commit của lượt giao này vào PR sau khi kiểm; không reset/bỏ chúng. Review PR #39 đã nằm trong merge, không còn là docs chưa commit. Lượt giao task này chỉ khảo sát và viết tài liệu, **chưa triển khai hoặc chạy test C2d-2b**. Safety vẫn **pending tới C4**.

## 2. Tài liệu và source phải đọc

- `AGENTS.md`, [docs README](../README.md), [auth restore/T14/lock order](../auth-restore-safety.md), [quyền và dữ liệu](../access-and-data.md).
- [C2d-1/helper](../c2d1-final-caller-reads.md), [caller inventory hiện hành](../c2d1-caller-inventory.json), [C2d-2a](../c2d2a-resource-json-reads.md) và [inventory kiểm](../c2d2a-test-inventory.json); [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2c-2](../c2c2-invitation-runtime.md).
- `identity/final_caller.py`, `api/deps.py`, `api/resource_reads.py`, `auth_safety.py`, `authz.py`, routes `reviews.py`/`publications.py`; `review_closing.py`, `promotion.py`, `publication.py`, `common_index.py`, `common_original.py`, recovery/receipt/journal và models liên quan.
- Test GET/fixture runtime C2d-2a, `resource_fixtures.py`, `test_review_requests.py`, `test_review_approval.py`, `test_publication.py`, `test_review_closure_journal.py`, `test_common_original.py` và `conftest.py`.

C1/Origin/runtime trust boundary đã chốt theo ủy quyền, không xin lại. C3 operator/legacy recovery/initial status/delivery và C4 cutover/storage còn riêng. C2d-2a hoàn tất 13 GET không có nghĩa toàn C2d/T14 đã hoàn tất.

## 3. Phạm vi chính xác

| GET | Handler hiện tại | Quyền |
| --- | --- | --- |
| `/review-requests/notice` | `reviews.notice` | Caller hợp lệ; dù response chỉ là config vẫn kiểm cuối |
| `/review-requests` | `reviews.list_own` | Chỉ request của caller và private item của chính caller |
| `/review-requests/{request_id}` | `reviews.get_own` | Owner, giữ đọc lịch sử request đã đóng |
| `/admin/review-requests` | `reviews.admin_list` | Current Admin; filter open/closed, private payload qua grant |
| `/admin/review-requests/{request_id}` | `reviews.admin_get` | Current Admin; cùng grant và thin history |
| `/admin/pending-publications` | `publications.list_pending` | Current Admin + `pending_publication_clause` |
| `/admin/pending-publications/{item_id}` | `publications.get_pending` | Current Admin + pending gate |
| `/admin/common-copies` | `publications.list_copies` | Current Admin + `review_copy_admin_clause`, filter state |
| `/admin/common-copies/{item_id}` | `publications.get_copy` | Current Admin + reviewed-copy operational history |

Không chuyển GET original review `/admin/review-requests/{request_id}/files/{file_id}/original`: đó là storage/stream/audit/commit, để task file riêng. Không chuyển submit/withdraw/reject/approve, publish/revoke/index, upload/download, JSON writes, search/RAG/provider hoặc worker. Không gắn read guard vào `_owner_out`/`_admin_out`/`_full_out`/`_copy_out` dùng chung với POST rồi âm thầm đổi transaction/lock của writer.

## 4. Giữ đúng quyền review và lịch sử

1. Admin không có quyền private toàn hệ thống. Chỉ private payload của request **còn open và grant hợp lệ** qua `review_grant_clause`: requester/owner, item private, version/file/checksum, rights epoch đều đúng. Không mở version khác, job/chunk/text khác hoặc private của User khác chỉ vì có cùng bytes/hash.
2. Grant mất do withdraw/reject/approve/replay closure/epoch thay đổi trước final boundary → không trả private title, version/file metadata, requester email, declaration hoặc `original_url` từ DTO cũ.
3. **Closed Admin detail hiện trả 200 với trạng thái tối thiểu**, không phải 404 mọi trường hợp. `admin_list` đếm workflow request theo open/closed; request open nhưng grant không còn hợp lệ cũng chỉ có thin DTO. Giữ contract này, không bỏ hàng/count chỉ vì không còn grant. Approved detail có thể chứa `pending_publication_id` như hiện hành; `closed_by_recovery` không phải approval và không dựng lại common copy/rights.
4. Owner vẫn đọc request lịch sử cùng private item của mình, nhưng không được mở review của B hoặc lấy rights/bằng chứng Admin ngoài contract. Lời khai User vẫn `verified=false`.
5. Pending common metadata chỉ ở route Admin riêng. Common-copy Admin history cố ý gồm pending/listed/revoked; revoke không biến history này thành 404. Sau cleanup chỉ hiển thị metadata còn được giữ đúng retention policy, không lấy lại giá trị đã scrub từ private source, audit, journal hoặc cache.
6. Publication state, content status, quyền tải, fulltext search và external-processing/RAG là các gate riêng. Quyền review/common metadata không cấp byte/text/provider. Giữ các cờ `original_download`, `fulltext_available`, index `search_open/rag_open` từ đúng gate SQL hiện hành.

## 5. Boundary caller/resource và thứ tự khóa

- Giữ entry session/CSRF/Origin trong deps; dùng `identity.final_caller` với exact session/User, clock callable mới và idle config. Helper giữ shared journal lock **trên transaction hiện tại**, không touch/issue/commit/rollback. Không connection phụ hoặc proof vượt commit.
- Dựng Principal/role/profile từ proof mới; **6 GET Admin** yêu cầu current Admin, không giữ role của dependency làm quyết định cuối. Caller mất hiệu lực → `401 session_expired` + clear cookie; caller còn live nhưng mất Admin → `403 forbidden`; safety/integrity/hold/pending lỗi → `503 service_recovering`.
- SQL ACL trước count/order/limit/offset ở mọi lần đọc. Giữ owner filter, status/state và tuple grant. Không lấy toàn DB rồi lọc Python. UUID string sai ở các detail này hiện parse thành 404; validation query vẫn giữ contract hiện hành, không tự đổi status code.
- Trước mọi response thành công, kể cả notice/empty/count=0/thin closed DTO, cần caller proof với clock mới **sau DB work/lock wait**, và kiểm resource/flags còn đúng. Bỏ ORM identity map cũ; read path sạch/no autoflush, không auth event/job/audit hay business commit mới.
- **Không dùng nguyên xi `resource_json` của C2d-2a cho Admin review/pending/revoked.** Helper đó khóa item qua `visible_items_clause`, vốn không mở private của User cho Admin hoặc common pending/revoked. Chọn boundary đọc riêng hoặc refactor có chiến lược SQL/lock rõ, giữ đầy đủ hồi quy 13 GET cũ.
- Review read bảo vệ request/grant theo **request → private item → journal**; UUID ổn định trong mỗi nhóm, đối chiếu writer thật. Approve giữ request UPDATE rồi private item SHARE; withdraw/reject giữ request trước journal. Không dùng item → request làm vòng khóa ngược.
- Common publication read đối chiếu item → rights/job/gate → journal của publish/revoke/index. Cleanup giữ **tombstone → item**: không thêm tombstone row lock sau item rồi tạo vòng chờ với cleanup. Aux read như publication/tombstone/job/retention/duplicate phải có boundary được giải thích; không tùy tiện khóa mọi FK hoặc khóa toàn scope để giữ count.
- Tất cả resource locks cần thiết trước shared journal, không lấy User/session lock sau đó, không nâng shared lên exclusive. Không storage/provider/network I/O dưới khóa; 9 GET này không cần các I/O ấy.
- Dựng lại DTO bằng SQL hiện hành dưới boundary; nếu scope/page/count/role đổi, bỏ DTO/proof và retry hữu hạn theo convention C2d-2a hoặc trả lỗi có kiểm soát. Không lấy resource lock bổ sung sau journal. Có thể tái dùng `409 resource_scope_changed`/timeout config hiện hành khi phù hợp; ghi contract/UI fallback, không hứa các SELECT READ COMMITTED tạo snapshot nhất quán toàn hệ thống.
- Nếu resource miss/denied xuất hiện sau công việc chậm, kiểm caller trước trả lỗi resource; auth hết hạn không bị che bằng empty/404. Không bọc lỗi tùy ý thành 200.

Cho từng route, bàn giao phải nêu SQL permission, rows/locks, current role, count/page, dữ liệu phụ được đọc, điểm clock cuối và quyền được ổn định tới lúc kết thúc transaction. Response đã qua final boundary có thể tiếp tục truyền sau nhả khóa; bytes đã gửi không thu hồi được.

### Mâu thuẫn source đã thấy khi giao task

Docstring và `LISTED_NOTE` trong `routes/publications.py` còn nói indexed common chưa có search/RAG, dù PR #24/#25 đã mở theo gate. `REVOKED_NOTE` nói không route nào phục vụ nhưng route Admin history vẫn tồn tại. Sửa mô tả/note hiển thị cho đúng: listed là metadata; fulltext/RAG/download chỉ khi gate tương ứng mở; revoked bị chặn ở route nội dung thường nhưng Admin còn lịch sử quyết định đã scrub theo policy. Giữ shape/codes và không mở quyền mới. Note/formatter dùng chung POST có thể đổi câu chữ; ghi rõ tác động đó, **không đổi writer transaction/guard** ngoài task. Nếu phải thay UI để phản ánh contract này, kiểm web tương ứng.

## 6. Kiểm thử và chứng cứ tối thiểu

Chỉ PostgreSQL/runtime/recovery LOGIN + journal tổng hợp **cô lập**, credentials explicit trước import fixture; không fallback `.env`. User/receipt/session qua bootstrap/invitation/login runtime thật. Có thể setup resource/review/common rows bằng đặc quyền test và dùng helper closure/publication journal thật cho race, ghi rõ writer còn ngoài scope; không mở thêm runtime grant, mock predicate hoặc bypass production. HTTP nghiệp vụ dùng cutover scoped ở fixture; **9 pending HTTP dùng gate thật**, giữ health sống/ready 503.

| Nhóm | Tiêu chí |
| --- | --- |
| Inventory 9 GET | Path/method/function, mọi nhánh 200 notice/empty/list/detail/thin closed/pending/listed/revoked; final proof đúng transaction |
| Caller sau dependency | Revoke/disable/credential-generation/status head/receipt/quarantine/held; hold/journal ahead/unconfirmed thật → không trả payload thành công |
| Clock/current Admin | Clock tiến qua idle/absolute trong SQL và row wait → 401 đúng biên; trước biên hợp lệ; 6 GET Admin stale role → 403; không touch idle ở final |
| A/B/Admin và grant | A/B cùng hash, khác request/version/file; owner count/page/detail không lộ B; Admin chỉ private payload đúng grant. Wrong checksum/version/file/epoch, closed/recovery → thin DTO, không URL/email/declaration stale |
| Resource races | Withdraw/reject/approve đóng grant trước final: không private DTO; cả reader thắng và writer thắng bằng Event/barrier/DB wait. Pending → listed/revoked trước final: không pending DTO cũ; common history/flags/retention/job được đọc lại |
| SQL count/page | Limit=1/filter item/status/state; foreign private request không chiếm owner slot/count. Admin workflow count không bị đổi thành chỉ grant-count; giữ closed-history contract |
| Common metadata/gates | Pending chỉ Admin; listed metadata-only; indexed search-only/provider/download riêng; tuple/epoch/publication/tombstone sai đóng flags; revoked/cleanup không lộ trường đã scrub hoặc private source |
| Khóa/transaction | Không item→request deadlock; review writer/approve/revoke/cleanup và auth writer hai thứ tự thật; không User lock sau journal, không leaked connection/transaction, không DML ẩn qua read formatter |
| Hồi quy | C2d-1 và 13 GET C2d-2a liên quan; read contracts review/approval/publication/common-original, closure recovery. ORM/schema nếu đổi helper/model/routine; không tính whole mixed writer file pass từ selected GET |
| Riêng tư/note | Marker/token/verifier/link/key không vào log/audit/journal không được phép; closed thin DTO không chứa field riêng. Notes pending/listed/revoked đúng gate, không hứa production readiness |

Test phải chứng minh core guards **trong C2d-2b**, không hoãn chúng tới C4. Nếu full file còn writer/file/provider chưa chuyển, port/tách assertions GET dùng runtime auth thật và báo phần ngoài scope not-run, không đổi hàng loạt expectation thành 503 để được pass.

Đối chứng theo rủi ro: bỏ final caller/current Admin, grant state/tuple/epoch, SQL owner trước count/page, final resource recheck hoặc dùng nhầm ordinary visible lock phải làm test fail. Chứng minh lỗi khóa bằng race tất định nếu sửa lock map; khôi phục bytes **trước** suite cuối. Không đổi source/migration/mutant khi suite đang chạy.

Báo source SHA, collect/node IDs và JUnit không thiếu/trùng; pass/fail/error/skip/not-run, lượt bị dừng và lỗi chuẩn bị riêng. Không cộng lượt chồng nhau thành full suite. Nếu web thay đổi, chạy tests/typecheck/build tương ứng; nếu không, ghi not-run. Không tuyên bố browser/stream/storage/provider/drill/cutover đã kiểm bằng JSON GET tests.

## 7. Bàn giao và hoàn thành

- Tạo `docs/c2d2b-review-publication-json-reads.md` và `docs/c2d2b-test-inventory.json`. Cập nhật caller inventory source/line/status cho **đúng 9 GET**; original review stream và mọi writer giữ trạng thái riêng.
- Đồng bộ README/docs README, roadmap, decisions, testing/dev-setup; giữ kết quả agent/reviewer lịch sử theo source. AGENTS dẫn contract/lệnh thực đã kiểm; kiểm links/control characters và `git diff --check`.
- Python/cài thư viện chỉ dùng `C:\Users\ADMIN\.conda\envs\ras\python.exe`; xác minh interpreter/pip, không venv. Không đọc/sửa `.env`, truy vấn/nâng/restart dev, gọi OpenRouter, đổi `pricing_verified`, mở safety ready hoặc dùng bootstrap làm recovery.
- Tài nguyên thử riêng, ports/labels/credentials/journal ngoài repo. Kiểm đường dẫn tuyệt đối và ownership trước dọn; không đụng dev volumes/journal. Không commit secret/artifact.
- Giữ tác giả/committer hiện có, không AI trailer/attribution. Commit/push và mở PR tiếng Việt; không merge. Báo base/head/branch/commit/files, kết quả thực chạy/not-run, tài nguyên đã dọn và phần C2d/C3/C4 chưa hoàn tất.

**Hoàn thành:** 9 GET giữ contract owner/grant/thin history/admin common metadata, final caller/role/clock/head/receipt đúng transaction, SQL ACL trước count/page, resource recheck và locks không đảo thứ tự writer; tests runtime/race/pending/đối chứng phù hợp đạt, docs/inventory nhất quán. Safety vẫn **pending tới C4**; không hứa restore production/mất điện an toàn.
