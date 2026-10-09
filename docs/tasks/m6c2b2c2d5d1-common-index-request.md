# Prompt C2d-5d1: yêu cầu lập chỉ mục bản chung qua runtime

PR #49 đã **MERGED** tại `859dfe9793dd9549e3ca9b8f0102361f414d0340` ngày 2026-10-09, chứa head `f8dc44ce5e146e5c867534f51a7e866769c1bc6b`; merge tree giống head đã review. AST mã nguồn có một head migration **0042**. Không truy vấn revision DB dev. Đây là **task đã giao, chưa triển khai/chưa kiểm thử**. Các lựa chọn dưới đây do orchestrator chốt theo ủy quyền của chủ dự án cho phạm vi này; **safety vẫn pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-5d1-sau-merge-pr-49-2026-10-09) · [Roadmap](../roadmap.md) · [Review PR #49](../reviews/pr49-c2d5c2-common-revoke.md) · [Handoff revoke](../c2d5c2-common-revoke.md) · [Caller map](../c2d1-caller-inventory.json)

Bạn là coding agent chính. Hoàn thành một PR **C2d-5d1**: khảo sát, triển khai, kiểm thử, cập nhật docs, commit/push và mở PR **tiếng Việt** để review; **không merge**. Chuyển đúng **`POST /admin/common-copies/{item_id}/index`** sang runtime thật, với current Admin/session/CSRF/clock, job gắn đúng bản chung, commit outcome độc lập teardown và response từ proof mới.

## 1. Phạm vi và thứ tự tiếp theo

Task này chỉ nhận yêu cầu tạo hoặc trả lại job index đã có. Tách **C2d-5d2 worker/ingest/index và provider dispatch** thành PR sau: chuyển API và toàn bộ pipeline trong một PR sẽ khó phân biệt lỗi quyền HTTP, lease/job và provider. Kiểm tương thích worker trong PR này không có nghĩa worker đã chuyển sang hợp đồng auth safety mới.

Ngoài phạm vi: chuyển worker/beat/cleanup/reconcile, search/rerank/RAG provider boundaries, reindex bản đã indexed, cho retry PDF cần review, OCR, model/routing/budget, C3/operator/delivery, C4/cutover và storage journal production. Không mở safety ready hoặc gọi OpenRouter/arXiv. Không đổi pipeline/ranking/coverage để làm fixture qua.

Đầu ra HTTP là job bền vững, **không phải** toàn văn đã index hoặc lời hứa provider đã chạy. Giữ `metadata_only`/active generation cho tới pipeline hiện hành công bố thành công. `provider_ready=false` không ngăn tạo job; job có thể extract rồi chờ index. Không ép bật key, `pricing_verified` hay provider để demo endpoint.

## 2. Base, tài liệu và môi trường

- Fetch lại, xác minh PR #49/main chứa các SHA trên; tạo `feat/m6c2b2c2d5d1-common-index-request` từ main đã xác minh. Nếu main tiến, ghi base/head thực tế và kiểm diff phụ thuộc.
- Giữ **mọi docs chưa commit của orchestrator**, gồm review PR #49, cập nhật merge/caller map và prompt này; mang đầy đủ vào PR. Không reset/clean làm mất chúng, không đổi kết quả lịch sử thành pass tại source mới.
- Đọc AGENTS, [quyền và dữ liệu](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [reads review/publication](../c2d2b-review-publication-json-reads.md), [resource writers](../c2d3-library-collection-json-writes.md), [clock](../c2d-final-read-clock.md), [approve/P2](../c2d5b-review-approval-original-copy.md), [publish](../c2d5c1-common-publish.md), revoke/review ở trên và [testing](../testing.md).
- Python/cài thư viện chỉ trong `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip, không tạo venv. Cấp explicit test DB/admin/runtime/recovery/storage/journal settings **trước import fixture** để không fallback đọc `.env`.
- PG/MinIO/journal, ports, container/volume và credentials thử phải cô lập. Không đọc/sửa `.env` hoặc key; không query/migrate/restart dev; không dùng owner/migrator/recovery làm connection HTTP. Không gọi provider, đổi `pricing_verified`, mở production gate hoặc dùng bootstrap để vượt quarantine/held.
- Dùng runtime role thật với generation/receipt/status/session hợp lệ trong business fixtures. Scoped fixture cutover chỉ cho bài thử nghiệp vụ; bài HTTP pending riêng phải giữ gate thật, không bypass trong production.

## 3. Khảo sát trước triển khai

Đọc `api/app/api/routes/publications.py`, `app/common_index.py`, `api/common_publish.py`, `api/common_revoke.py`, `api/resource_writes.py`, `identity/final_caller.py`, `auth_safety.py`, `db_roles.py`, models và guards `0024/0025/0042`; đọc worker `due_job_ids`/claim/checkpoint, indexer, fixtures runtime, tests common-index/publication/recovery/cleanup và form/proxy web.

Quan sát tĩnh tại base, phải đối chiếu bằng thử nghiệm:

1. Route index còn `get_db`/`get_now`, gọi `common_index.start` tự commit rồi `_copy_out` bằng caller/thời gian cũ. Chưa có actor boundary cuối và response proof mới.
2. `start` có nhánh live-job commit riêng, nhánh `IntegrityError` rollback rồi SELECT để trả job và `_refuse` audit bằng principal cũ. Không gọi nguyên helper commit này từ boundary mới hoặc dùng nó làm fallback đặc quyền.
3. Job `promotion` đã ghim item/version/file/epoch, có partial unique một job chưa `finished_at` cho mỗi file. Worker lấy job bền vững qua `due_job_ids`; HTTP hiện không cần gửi task trực tiếp tới broker.
4. `confirm_external_processing: Literal[True]` cần kiểm coercion; form index chưa có busy/ref guard như revoke. Phải đo bằng test, không ghi là bug đã tái hiện khi chưa chạy.

Ghi mâu thuẫn docs/code, thử tái hiện lỗi cần sửa và chọn phạm vi tối thiểu. Có thể tách hàm đọc/gate/DML không commit để dùng chung; đừng gắn writer guard vào formatter GET hoặc làm thay đổi worker ngoài phạm vi đã công bố.

## 4. Hợp đồng request và job

- Giữ exact POST/path và response tương thích: `item`, `job.id`, `job.state`, `created`. `created` là fact của attempt đã known commit, không suy bằng SELECT sau commit exception. Trạng thái job/item là hiện hành ở response proof, không cố định `received`.
- Body chỉ nhận xác nhận **boolean true thực sự**; missing/false/null/0/1/string/extra field không được tạo hoặc trả job thành công. Giữ Origin bắt buộc, session/CSRF và current Admin; checkbox ban đầu không chọn. Không thêm file bytes vào request.
- Server chọn và kiểm đúng tuple common item–version–file–checksum–rights–publication–epoch. Chỉ item `listed`, không revoked/tombstone, quyền hiện hành đúng file/epoch, audience hỗ trợ, cho **full-text search và external processing**, không `scope_conditions`, MIME PDF/Markdown mới đủ điều kiện. Không suy quyền từ checksum trùng, title, upload của User hoặc `indexed` của item khác.
- Dùng item lock và unique constraint hiện hành để tạo đúng một live job cho file. Job mới giữ `origin=promotion`, `scope_visibility=common`, owner NULL, actor là current Admin, source/version/file/epoch chính xác; key/label/warning/error chỉ IDs/mã, không title/text/secret vì job còn sau cleanup.
- Nếu live job đã tồn tại, chỉ trả lại khi tuple/scope/epoch khớp; không ghi đè actor/binding của job cũ, reset lease/state/cancel, tạo attempt mới hoặc gọi broker/provider. Cả nhánh reuse vẫn phải đi qua current actor/gate/clock và response proof. Live job đã bị yêu cầu hủy không được trình bày như một job mới được xếp hàng.
- Giữ hạn chế hiện hành `already_indexed` và extraction rejection/`needs_review`; không lén bổ sung reindex hoặc bỏ coverage gate. Nếu một integrity conflict không còn thuộc luồng duplicate được khóa đúng, trả lỗi có kiểm soát; không tự retry transaction hoặc suy known commit từ một SELECT.
- Job đã commit là công việc của ứng dụng theo rights/epoch của bản chung. Không tự gắn tuổi thọ job với cookie Admin hoặc hủy job chỉ vì Admin logout sau response; worker PR sau sẽ kiểm gate/quyền/lease tại checkpoint. Actor ID là attribution, không phải quyền đọc kho riêng lâu dài.
- Không I/O storage/parser/Redis/provider trong acceptance/response; không hash object ở endpoint này. Kiểm original/SHA-256 vẫn thuộc worker và download. Không tự thêm post-commit broker send: job DB tồn tại qua broker mất kết nối là hợp đồng hiện hành.

## 5. Transaction, current proof và outcome

Chọn boundary riêng (ví dụ `api/common_index_request.py`) hoặc helper tương đương có trách nhiệm rõ. Runtime business DML đã có grants; ưu tiên không migration mới. Nếu thiếu guard/quyền được chứng minh, báo và thêm revision **sau head thực tế**, giữ nguyên revision đã merge, không mở raw protected auth/journal writes hoặc làm runtime owner.

### Acceptance

1. Lấy caller User `FOR NO KEY UPDATE` trước resource, theo helper runtime hiện hành; không tin Admin role/session trong dependency là đủ cho commit.
2. Khóa common item `FOR UPDATE`, rồi rights/publication và job/version/file theo một thứ tự nhất quán với revoke. Khóa item **trước job**, không khóa tombstone sau item; kiểm tombstone bằng predicate. Giữ thứ tự cụ thể trong handoff/caller map và thử cả hai thứ tự race.
3. Kiểm eligibility, tạo job hoặc chọn live job đúng binding; flush mọi DML/FK/unique/audit trước journal. Audit mới chỉ IDs/mã; không audit denial bằng `_refuse` cũ hoặc principal stale.
4. Lấy journal **exclusive trực tiếp** qua writer boundary, không shared rồi upgrade. Sau SQL cuối kiểm gate, exact binding/job, current actor/session/head/receipt/generation/status/role/CSRF và clock mới; lấy thời gian mới sau lock wait, không dùng `get_now` lúc request bắt đầu. Không lấy khóa User/resource mới sau journal; formatter không được tạo DML/FK muộn.
5. Commit và giải phóng Session có kiểm soát. Đây là cấp công việc, không phải restriction-only như revoke: auth/gate lỗi trước commit phải rollback, không áp ngoại lệ false-mirror đóng quyền. Không tạo event journal mới cho một job nếu không có yêu cầu được chứng minh.

### Response sau known commit

- Dùng **Session/transaction mới**, exact common history gate/current Admin, item SHARE → resources/job → journal shared, current caller/clock sau SQL formatter cuối. Không dùng ORM cache, principal cũ hoặc proof precommit làm response proof.
- Job có thể được worker tiến tới state khác hoặc revoke/cleanup thắng sau acceptance. Nếu Admin còn được xem lịch sử và job retained vẫn đúng binding, cho 200 với DTO hiện hành (kể cả revoked/cancelled/scrubbed), `created` của attempt; không ép gate indexable lần nữa để hứa job còn chạy. File/metadata/link đã bị scrub không được dựng lại từ snapshot. Nếu không còn chứng minh tuple/quyền, trả denial/503 phù hợp, không payload cũ.
- Response proof phải release sạch trước trả DTO. Missing/malformed ID/denial muộn cũng có current caller/gate proof; giữ mã auth/business phù hợp, lỗi SQL/release bất ngờ là JSON 503 có kiểm soát, không traceback hoặc HTTP 500.
- Session ngắn luôn thử rollback và close; invalidate connection khi cần, không rò pool. Proof không được coi là thành công nếu release lỗi. Outcome không phụ thuộc Session còn tồn tại.

| Cửa sổ | Hợp đồng |
| --- | --- |
| Lỗi auth/gate/binding/DML trước commit | Rollback/release; không tạo job thành công, không success DTO; giữ denial xác định hoặc `503 index_start_unavailable` |
| Commit ném lỗi, dù thực tế landed hay rollback | `503 index_start_outcome_uncertain`; outcome giữ `commit_started`, không success/IDs/SELECT inference/replay/auto retry hoặc xóa job có thể đã lưu |
| Known commit nhưng teardown/response proof lỗi | Giữ job/audit, trả current denial hoặc `503 index_start_response_unavailable`; không nói chắc “chưa tạo job” |
| Known commit và response proof/release sạch | 200 DTO mới, job hiện hành và `created` của attempt |

Giữ facts `commit_started`/`committed` riêng với rollback/close/invalidate, kể cả chúng ném lỗi. Cancellation/disconnect không chứng minh job chưa lưu: nếu dùng executor/thread thì attempt giữ Session tới thread settle; không thả connection đang dùng, không trả DTO khi đã ngắt, không tự POST lại. Test riêng landed/lost-ack và rolled-back commit exception, không dùng fault injection làm bằng chứng crash/mất điện thật.

## 6. Web và proxy

- Form index có ref guard đồng bộ + busy disable để hai submit cùng tick chỉ gửi một POST. Checkbox rỗng ban đầu, thông báo text gửi provider giữ rõ; sau thành công bỏ tick/refresh từ GET hiện hành, sau lỗi bỏ tick.
- Mất response/proxy 504/commit uncertain/postcommit response unavailable: thông báo **không xác định kết quả**, job có thể đã tạo; không tự resend hoặc khẳng định rollback. Hướng dẫn tải lại trạng thái; lần POST chủ động mới sau đó vẫn qua dedup server.
- Chỉ gọi callback thành công khi có response thành công; lỗi refresh GET sau POST thành công không được biến thành thông báo chắc chắn POST thất bại. Không suy indexed/provider đã chạy từ `created=true`.
- Giữ normal proxy timeout 15 giây cho đúng route index vì request chỉ làm DB. Không lấy timeout publish/approval hoặc mục tiêu p95 làm timeout mới; không thêm config pool/deadline I/O không dùng. Kiểm bằng proxy/browser thật.

## 7. Kiểm chứng bắt buộc và giới hạn báo cáo

Chọn test theo rủi ro, không viết hàng loạt ca chỉ lặp implementation; inventory phải giữ exact node IDs, source và kết quả thật.

1. **Runtime/ACL/input:** Admin hợp lệ tạo/reuse job; A/B/khách và Admin mất quyền không thành công. Private/pending/revoked/tombstone/wrong tuple/rights/epoch/conditions/MIME đều đóng; cùng checksum private/common/owner khác không lộ hoặc bị sửa. Missing/false/coerced confirmation/extra field, CSRF/Origin và ID sai có mã ổn định, không side effect.
2. **Clock/current actor:** logout/reset/disable/credential hoặc status head/receipt/generation đổi, role Admin mất, idle/absolute đúng biên sau lock wait/SQL cuối; cả create và reuse. Có control trước hạn; response proof mới kiểm lại sau known commit. Không chỉ thay dependency principal thành giả.
3. **Job/dedup:** hai POST đồng thời chỉ một job mới/audit success đúng semantics; retry request chủ động nhận cùng live job đúng binding. Binding lệch/cancelled/finished/indexed/needs_review không bị reset. Redis unavailable không làm mất job DB; endpoint không cần worker/provider để thành công.
4. **Race tất định:** index thắng/revoke thắng, job advances/cancels/cleanup giữa commit và response, auth writer thắng ở precommit hoặc response. Quan sát lock wait thay sleep đoán; không deadlock item↔job/journal, không locks/resources lấy sau journal. Revoke thắng trước acceptance không có job mới; index thắng thì revoke hủy job và chặn nội dung ngay.
5. **Outcome/release/cancel:** DML/FK/flush lỗi, commit landed/lost response và rollback exception, close/rollback/invalidate lỗi tại proof/acceptance/response, cancellation khi worker thread đang giữ Session. Không 500/pool leak/IDs/retry hoặc xóa job sau uncertain; known commit còn job khi response lỗi. Đối chứng bỏ outcome fact không được pass nhờ teardown.
6. **Compatibility:** hồi quy publication/revoke, common-index job/schema, cleanup và worker checkpoint liên quan với provider giả/HTTP ngoài bị chặn. Fixture tạo bản chung qua runtime approve/publish/revoke; owner connection chỉ migration/setup corruption có nhãn. Không đổi các test worker sang principal bypass để gọi là runtime worker đã đạt.
7. **Web:** tests/typecheck/build; Chrome thật qua Next proxy tới API runtime + PG/journal thật: unticked không POST, hai submit một POST, success tạo/reuse và refresh, lost response/504 unknown không resend, session hoặc role bị thu hồi bị chặn. Không chỉ gọi thẳng port API hoặc kiểm build. Nếu test chặn response bằng barrier/network proxy, ghi chính xác lớp giả lập; không tuyên bố clean Compose build nếu chưa chạy.
8. **Pending:** HTTP không mock production gate, `/healthz` sống, readiness và POST index vẫn đóng với safety pending; không dùng business-fixture cutover làm chứng cứ mở gate thật. Nếu chuyển worker chỉ trong fixture để dựng dữ liệu, ghi riêng không phải implementation của PR này.

Chạy nhóm mới và hồi quy trực tiếp đủ cho rủi ro; nếu không chạy toàn inventory, ghi **not-run**, không tính skip/pass. Chuẩn bị JUnit/selection, source hash trước/sau, distinct unique pass không cộng rerun, errors/failures/interrupted runs riêng. Kiểm đối chứng ngoài suite cuối, lưu raw XML/log vào tên riêng cho từng lượt (không ghi đè như observer yếu PR #49), khôi phục bytes/hash rồi mới suite cuối. Không sửa source trong lúc suite chạy. Mọi lỗi chuẩn bị/flake phải ghi assertion và nguyên nhân đã/chưa chứng minh.

Lấy baseline container ID/image/**StartedAt** ngay từ đầu và trước/sau cleanup; chỉ dọn exact tài nguyên thử có ownership/sole-consumer rõ. Không suy “dev nguyên trạng” từ snapshot mới nếu baseline drift; không điều tra bằng query/restart dev. Quét secret không in giá trị; không commit journal/paper/credentials/artifact/backup. Evidence ngoài repo có manifest/hash và đường dẫn ổn định; cleanup credentials có thể xóa chúng nhưng không xóa log/XML chứng cứ cần review.

## 8. Hoàn thành và bàn giao

- Một PR chỉ phạm vi trên; runtime role thật, job bền vững/dedup/binding đúng, current caller/clock ở hai boundary, outcome/release không lộ lỗi, UI/proxy không auto retry, hồi quy đủ và safety pending giữ nguyên.
- Tạo `docs/c2d5d1-common-index-request.md` và `docs/c2d5d1-test-inventory.json`: phase/lock/caller map, legacy helper còn ai gọi, commit matrix, source SHA, selection/JUnit/hash/evidence, pass/fail/not-run, giới hạn. Cập nhật README/docs index/roadmap/decisions/testing/AGENTS khi có kết quả thật; lệnh AGENTS chỉ thêm khi đã chạy.
- Cập nhật caller inventory đúng một POST đã chuyển; worker/cleanup/provider/C3/C4 vẫn mở. Bàn giao C2d-5d2 bằng danh sách entrypoint/checkpoint/engine/role và lock/gate/provider/lease chưa chuyển, không chỉ ghi “worker tiếp theo”. Worker phải kiểm quyền bản chung tại thời điểm xử lý, không dùng Admin session của POST đã xong làm quyền vĩnh viễn.
- Commit/push và mở PR tiếng Việt; giữ tác giả/committer hiện có, không AI author/trailer/`Co-authored-by` hoặc dòng quảng bá AI. Không merge. Báo file đổi, kết quả thực chạy, lỗi/giới hạn/not-run và task tiếp; không tự bắt đầu C2d-5d2/C3/C4.

Lượt orchestrator giao task chỉ kiểm Git/source/docs/interpreter và tính nhất quán tài liệu; không chạy API/web/PG/MinIO/browser/provider test, không thao tác stack dev. Kết quả PR #49 giữ nguyên theo source review, chưa rerun tại merge.
