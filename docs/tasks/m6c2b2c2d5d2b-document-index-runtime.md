# Prompt C2d-5d2b: document embedding/index qua runtime

## 1. Nhiệm vụ và base

Bạn là coding agent chính. Hoàn thành **một PR C2d-5d2b**: khảo sát, triển khai, kiểm thử, cập nhật docs, commit/push và mở PR **tiếng Việt** cho chủ dự án review; **không merge**. Chuyển luồng document embedding/index của private upload và common promotion sang runtime thật: dispatch/claim index → reservation → kiểm trước provider → heartbeat → tiếp nhận kết quả/chi phí → keyword/vector entries → công bố generation. Extraction C2d-5d2a đã merge; không nối lại indexer legacy để có đường chạy tạm.

PR #51 đã merge tại **`9fb8890568af85b7beea4503f93f12e2896e6c89`**, head `b569d0c`, code/test đã kiểm `dfca84a`, production `5b505d3`. Source migration **0042**. Fetch lại, xác minh ancestor/head thực tế rồi tạo `feat/m6c2b2c2d5d2b-document-index-runtime` từ `origin/main`. Không suy revision DB dev. Giữ mọi docs review/giao task chưa commit, gồm [review PR #51](../reviews/pr51-c2d5d2a-worker-runtime-extraction.md); không reset/clean/ghi đè thay đổi có sẵn. Có thể commit docs có sẵn riêng trước code.

Đọc [AGENTS](../../AGENTS.md), [mục lục](../README.md), [yêu cầu](../product-requirements.md), [quyền và dữ liệu](../access-and-data.md), [quyết định giao](../decisions.md#giao-c2d-5d2b-sau-merge-pr-51-2026-10-10), [handoff extraction](../c2d5d2a-worker-runtime-extraction.md), [HTTP index request](../c2d5d1-common-index-request.md), [revoke runtime](../c2d5c2-common-revoke.md), [nền C1](../c1-auth-safety-foundation.md), [search/RAG](../search-and-rag.md) và [kiểm thử](../testing.md).

**Safety vẫn pending tới C4.** Không đọc/sửa `.env` hoặc key; không query/migrate/restart dev hay tài nguyên có sẵn; không gọi OpenRouter/arXiv thật, đổi pricing mặc định, mở cutover hoặc chạy C3/C4. Python/cài thư viện chỉ trong Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước chạy, không tạo venv. Provision/migrate chỉ trên project thử cô lập, bằng bước host riêng.

## 2. Phạm vi và khảo sát bắt buộc

| Trong PR này | Task riêng |
| --- | --- |
| Private/common document index, index-state dispatch/claim, job/call lease, reservation, provider admission/outcome, công bố keyword/vector generation | Query embedding/rerank/search API và RAG/generation runtime; ranking/evidence pack/streaming |
| Document heartbeat/reaper và accounting tối thiểu cần để luồng này hoạt động đúng; tương thích shared capacity/budget | Chuyển toàn bộ discovery/citation/cleanup/reconcile nền, operator/delivery/C3, Compose/API cutover/C4 |
| Fake transport qua adapter thật, PG/MinIO/Redis/worker/beat cô lập | Provider live/charge, benchmark/SLO, model change/reindex, định dạng mới/OCR/AI summary |

Khảo sát `ingest/{worker,checkpoints,runtime,indexer,embedding,celery_app}.py`, `provider_capacity.py`, `provider_identity.py`, `models/index.py`, `search.py` (shared capacity/accounting), `common_index.py`, `auth_safety.py`, `recovery.py`, `db_roles.py`, derived fence/cleanup và các test private/common index trước sửa. Vẽ call/lock/outcome map ngắn trong handoff.

Các điểm nguồn hiện tại cần kiểm và xử lý:

- Extraction dispatcher/claim cố ý bỏ `awaiting_index`, `indexing`, retry `resume_state=indexing`; `process_job` trả `index_pending`. Phải thêm boundary index riêng và route dispatch rõ, không nới mọi state qua helper extraction.
- `indexer.py` còn import `_locked` từ worker, trong khi worker hiện không còn định nghĩa helper đó. Kiểm/tái hiện import/entry trên settings thử tường minh, rồi thay phụ thuộc legacy; không thêm lại helper owner hoặc giả module để test qua.
- Legacy indexer dùng Session/ORM xuyên I/O, `db.commit()` trực tiếp và `_provider_failure` sau lỗi; chưa có outcome/proof theo hợp đồng runtime. `provider_capacity.py` có connection riêng, bulk expiry, heartbeat theo call ID và `log.exception`; không coi chúng đã chuyển chỉ vì engine là runtime.
- `checkpoints.context/leased/_final` hiện dành extraction, từ chối active index và index states. Reuse phần khóa/giải phóng/proof thích hợp; tách predicate đúng phase. Không làm yếu gate extraction hoặc cho publish đi qua kiểm dành `metadata_only` sau khi chính transaction vừa đặt `indexed`.
- Kiểm actual runtime grants trước chọn migration. Chỉ thêm revision mới sau head thực tế nếu cần schema/fence/attempt binding; không sửa revision đã merge hoặc cấp owner/TEMP/protected auth DML. Nếu thêm bảng dẫn xuất, cập nhật cleanup/leftover/fence cùng lúc.

## 3. Dispatch, quyền và job lease

- Dùng runtime role đã provision và guard thực của PR #51, không fallback owner/migrator/recovery, không Principal/session Admin giả. Compose mặc định/API chưa cutover; giữ override cô lập và explicit env ngoài repo. Worker/beat không nhận operator/recovery credential.
- Index dispatcher chỉ nhận job đúng index states đến hạn, không finished/cancelled, không live lease, không active index cần reindex. Candidate SELECT không phải proof. Broker chỉ nhận UUID; release transaction trước gửi; consumer kiểm lại dưới khóa. Hai message/worker chỉ một claimant/attempt theo durable lease.
- Thiếu key hoặc `pricing_verified=false`: giữ job chờ, không provider/reservation/đốt attempt mới vì thiếu cấu hình; không reset index đang dở về extraction. Job extraction vẫn chạy tới `awaiting_index` bình thường. Nêu rõ trạng thái/mã pause của index job đã chạy dở.
- Private: đúng owner/scope/file/version, exact processing acknowledgement/upload intent và notice đã dùng; owner active được chứng minh bằng latest status head/receipt. Disable/held/head thiếu thì pause, không terminal fail hoặc tự enable. Logout/reset credential không tự hủy background job.
- Common: đúng bản `listed`, publication và rights còn hiệu lực đúng file/version/checksum/epoch, audience, `permits_fulltext_search` **và** `permits_external_processing`, không `scope_conditions`, không tombstone/cancel. Session/role Admin tạo job không là quyền sống của worker. Không suy quyền từ cùng checksum với file private.
- Gate integrity/cutover/hold, binding, cancel, rights/status và job token/lease kiểm mới ở mỗi checkpoint, trước content I/O/provider và publish. Clock sau SQL cuối/lock wait; đúng biên là hết hạn. Không gia hạn lease đã hết bằng deadline mới để hợp thức hóa attempt cũ; stale worker không đổi job hoặc công bố.

## 4. Transaction, I/O và provider admission

- Checkpoint content writer: owner User khi private → item → job → resource rows theo thứ tự ổn định → capacity/budget/call/generation/entry cần thiết → flush DML/FK/unique/audit → journal **exclusive trực tiếp** → scalar binding/rights/status/gate và clock mới → commit. Không lấy thêm row/capacity lock sau journal. Đối chiếu thứ tự với revoke, cleanup và shared query capacity; ghi rõ khác biệt maintenance ở mục 5.
- Không giữ Session/connection/transaction/row/journal/capacity lock trong lúc S3/parser/hash/provider hoặc gửi broker. Không để ORM expire/lazy-load checkout connection sau commit; DTO/proof/payload detached và bất biến. Session ngắn luôn thử rollback và close, invalidate khi release lỗi; release lỗi dừng đường content/provider tiếp theo.
- Trước **mỗi** provider call, đọc toàn bộ original và kiểm actual size + SHA-256 **ngoài DB**, rồi làm proof/admission mới dưới khóa, commit reservation xác định và release sạch trước gửi. Lần hash mở đầu có thể phục vụ call đầu nếu chưa qua call nào; HEAD chỉ từ chối sớm, không thay hash. So lại payload với chunk/section/locator/hash/pipeline đúng file, không bibliography hoặc reference edge.
- Ngay sau entry cuối, luôn hash toàn original một lần nữa ngoài DB trước transaction publish, **kể cả chỉ reuse call completed, không có call mới**. Giữa hash và call/commit chỉ có DB steps. Ghi thẳng khe overwrite sau lần hash cuối; không hứa phát hiện mọi thay đổi trong khe đó.
- Reservation và pre-send proof phải bind đúng job attempt/token, source tuple, chunk hash/pipeline, model, reservation attempt và deadline. Nếu dùng lại một call row sau rejected, call ID đơn lẻ không đủ làm bằng chứng cho heartbeat/outcome của attempt trước; dùng binding/nonce bền vững phù hợp và migration mới nếu cần. Claim job không tạo quyền gửi lại call uncertain.
- Kiểm model/provider allowlist qua adapter hiện hành, vector hữu hạn/dimensions thống nhất và giới hạn bytes/token/giá cấu hình. Không mở allowlist hoặc đổi model/pricing để làm smoke xanh. Document và query vẫn cùng active model; lưu requested/response model/provider, dimensions và pipeline. Số chiều live đã quan sát không thay kiểm từng response.

## 5. Reservation, heartbeat và đối soát chi phí

- Giữ budget thử hiện hành trong config: app $10/ngày, embedding bucket $3/ngày, private quota $1/User/ngày; tính document + query và các task khác vào app đúng như hợp đồng hiện có. Common `owner_user_id=NULL`, chỉ app/embedding bucket, không quota uploader/Admin. Đây là phương án orchestrator đã chọn, **không ghi chủ dự án đã trực tiếp phê duyệt mới**; không bật dữ liệu thật/price gate.
- Admission nguyên tử cho concurrency document + query, app/bucket/User committed + reserved + uncertain, warning thresholds và reservation ceiling. Giữ request ceiling bảo thủ; token usage/quote là ước tính chi phí, không xác nhận charge live hoặc credit key.
- Phân biệt **job lease**, **reservation lease** và **thread/provider còn chạy**. Reaper giải slot hết hạn nhưng giữ tiền dự phòng; không tự retry uncertain. Heartbeat không hồi sinh lease đã hết hoặc gia hạn reservation attempt khác. Heartbeat của call đang gửi chỉ duy trì capacity, không gia hạn quyền nội dung/job trái gate. Nếu DB/gate lỗi thì dừng gia hạn, giữ dự phòng; provider đang nhận không thể thu hồi.
- Reaper/heartbeat/late outcome phải có runtime + gate, tuple/attempt binding và short transaction của riêng chúng; không gọi helper mở connection thứ hai trong khi caller giữ item/job/call/capacity. Thứ tự capacity và row locks phải tránh cả reentrancy lẫn vòng khóa với query/revoke/cleanup; không dùng bulk expiry vô hạn giữ khóa qua I/O. Nếu phải đổi shared helper, giữ API tương thích, kiểm các consumer cũ và chỉ nhận chứng nhận cho đường đã chuyển.
- **Quyền ghi accounting khác quyền ghi content.** Sau revoke/cancel/disable/job lease mất hoặc cleanup, provider có thể đã tính phí. Khi gate hợp lệ, cho phép checkpoint **chỉ tài chính** xác minh call bền vững/attempt, cập nhật tokens/model/provider/cost/status một lần, không đòi live job lease để bỏ quên chi phí; không ghi vector/chunk/text/index, dựng lại content đã scrub hoặc mở item. Khi gate/DB không cho đối soát, giữ reservation/uncertain để xử lý sau, không giải tiền theo phỏng đoán.
- Response về muộn sau reaper chỉ settle một lần vào **reserved_day gốc**, không trừ reserved/ghi committed hai lần. 429/definite rejection hoặc chắc chắn chưa gửi có thể giải đúng khoản đã giữ; timeout/network/parser/model mismatch/outcome chưa rõ giữ ceiling. Không suy rằng mọi exception trước commit là provider chưa nhận.
- Tài liệu hóa bảng outcome ít nhất cho: known admission commit, release lỗi sau commit, provider chưa gửi, request đã gửi/timeout, response hợp lệ nhưng job mất quyền, commit settlement không rõ, late result sau lease expiry, cleanup trước result. Không dùng `_provider_failure` để che lỗi commit/release hoặc gửi lại request.

## 6. Công bố và uncertain commit

- Generation building/entries chỉ theo đúng job/item/version/file/model/pipeline/index version/rights epoch. Reuse completed theo đúng file/model/pipeline/chunk hash, vector/dimensions và binding còn hợp lệ; không dùng chéo owner/common/private hoặc resurrect scrubbed content. Retry idempotent không embed lại chunk không đổi.
- Chỉ publish khi đủ toàn bộ eligible body/abstract chunks, keyword entry và vector khớp model/dimensions/locator/checksum; coverage hiện hành hợp lệ, parser versions đúng (không `layout-v1`/Markdown legacy thay parser hiện hành), không bibliography evidence. Không đổi threshold/partial policy/reindex để vượt gate.
- Một transaction publish atomic: generation active + active pointers + item `indexed` + job `ready`/`finished_at` + kết thúc attempt/lease. Proof cuối kiểm **trạng thái hậu mutation đúng của transaction này**, không chỉ proof building cũ. Revoke thắng thì không active generation; publish thắng thì revoke sau đó vẫn gỡ index và content gates ngay theo hợp đồng.
- Outcome thuộc attempt, độc lập rollback/close/invalidate. Mọi commit exception là uncertain: không SELECT suy landed, tự replay/retry, chạy failure checkpoint như chưa commit, giải reservation hoặc xóa object. Known commit rồi release lỗi giữ mutation và dừng. Lượt dispatch/recovery độc lập sau đó mới dựa durable state/lease; không tự gửi lại provider chỉ vì worker không nhận commit response.
- Log/audit chỉ IDs/mã và usage cần thiết; không question/chunk/paper/vector/key/cookie/DSN/traceback chứa credential. Đặc biệt bỏ hoặc kiểm soát exception logging ở heartbeat background. Thread/connection/temp thuộc attempt rõ ràng, settle/close trên mọi đường, không để heartbeat cũ chạy sang attempt mới.

## 7. Kiểm thử bắt buộc theo rủi ro

PG/pgvector + MinIO + Redis/journal cô lập, runtime thật. Setup owner/recovery chỉ fixture gắn nhãn; settings trước import, explicit host ports/project/env file, không default dev port hoặc `.env` fallback. Fake HTTP transport đi qua adapter thật, outbound bị chặn; bật pricing **chỉ trên bản Settings trong test gắn fake**, không production switch. Pending HTTP/task giữ gate thật riêng, không mock integrity/role để tạo kết quả pass.

| Nhóm | Ca phải chứng minh |
| --- | --- |
| Entry/dispatch/claim | Import indexer và entry trực tiếp; owner/recovery/thiếu parts/grants bị chặn; ba index states, thiếu price/key; Redis down/up; hai worker/message trùng; chỉ UUID; extraction hồi quy không bị nới |
| ACL và consent | Private A/B/cùng bytes; owner disabled/held; intent/ack sai; common rights/file/version/checksum/epoch/conditions/tombstone sai; Admin session hết không hủy common job hợp lệ; bibliography không vào payload/index evidence |
| Clock và I/O | Lease đúng biên sau SQL/journal wait; stale token; gate/cancel/rights/status/binding đổi khi hash/provider; revoke commit được khi S3/provider đang bị barrier chặn; không DB connection ở I/O |
| Hash/reuse/publish | Object đổi cùng size và HEAD giả không đổi; trước call tiếp/cuối; all-completed reuse rồi overwrite lúc ghi entry; final hash vẫn chạy; chunk/locator/pipeline/vector/model/dim sai; publish atomic/revoke hai thứ tự |
| Accounting | Cap app/bucket/User đồng thời với query, cảnh báo đúng; common NULL owner; heartbeat/reaper hai thứ tự; ngày cũ; late settle hai lần; old heartbeat sau re-reserve; response sau revoke/cleanup chỉ tài chính; không vector resurrect |
| Fault/outcome | Reservation/entry/settlement/publish commit landed nhưng ném lỗi và rollback; teardown lỗi độc lập; provider timeout/429/malformed/identity mismatch; không call sau uncertain admission/release; không double call/charge hoặc xóa original |
| Tích hợp | Worker + beat Linux/Redis thật, role từ connection của chính tiến trình, fake provider qua adapter; private Markdown/PDF/common tới `ready`/`indexed`; restart/resume giữ completed calls; pending giữ job, không provider |

- Test double lỗi phải có assertion nghiệp vụ/no-I/O/no-write; lỗi setup/timeout/deadlock ngoài kỳ vọng không tính đối chứng đạt. Tạm bỏ từng guard trọng yếu rồi chạy test nhắm đúng lỗi; giữ lượt mutant yếu/thất bại riêng, khôi phục bytes/hash trước suite cuối, không sửa source hoặc chạy mutant song song suite.
- Kiểm migration/ORM/schema; hồi quy extraction, private/common index, upload consent, revoke/cleanup/recovery/HTTP index request và shared capacity/query accounting có tác động. Bốn baseline PR #50 giữ debt riêng; nếu test trực tiếp bị thay đổi thì tái hiện base/head, sửa fixture đúng quyền, không cấp runtime `session_replication_role` hoặc đổi kỳ vọng pending để né lỗi.
- Kiểm theo call graph/rủi ro, không mở rộng suite vô hạn. Collect/selection/JUnit exact, unique pass/fail/error/skip/not-run, source hashes trước/sau, code SHA và artifact manifest ngoài repo. Không cộng pass lịch sử hoặc đếm retry thành unique. Không web/Chrome nếu không đổi web; nếu đổi UI/proxy thì kiểm tương ứng qua browser thật.

## 8. Hoàn thành và bàn giao

1. Runtime job private/common từ `awaiting_index` tới `ready`/`indexed` được chứng minh bằng fake provider; missing price/key và gate thật vẫn đóng. Không còn worker entry nào nối legacy indexer/maintenance để đi vòng checkpoint.
2. Handoff mới `docs/c2d5d2b-document-index-runtime.md`: call/engine/lock map, provider admission, job/call binding/lease, content-vs-accounting, bảng outcome, reuse/final hash, cleanup/thread ownership, config thực và caller còn legacy. Inventory `docs/c2d5d2b-test-inventory.json` có evidence/selection/JUnit/source SHA, lỗi các lượt trước, not-run và giới hạn.
3. Cập nhật README/docs index/roadmap/decisions/testing/dev-setup/caller map; AGENTS trỏ handoff và chỉ thêm lệnh đã chạy. Phân biệt đề xuất scoped, chủ dự án xác nhận và kết quả đo; không ghi bộ cũ đã pass mới, default Compose đã cutover, charge đã xác minh hoặc SLO đã đạt.
4. Cleanup chỉ tài nguyên thử theo exact ID/label/sole volume consumer, giữ evidence đã quét secret; không in raw inspect Env. So snapshot tài nguyên có sẵn và báo drift trung thực. Kiểm Markdown links/anchors/control chars và `git diff --check`.
5. Giữ tác giả/committer hiện có, không AI author/`Co-authored-by`/trailer hay dòng quảng bá AI. Commit/push, mở PR tiếng Việt, không merge. Báo ngắn file/code/base SHA, kết quả thực chạy, lỗi/giới hạn và phần chưa hoàn thành. **Safety pending tới C4**; query/rerank/RAG provider, background writer khác và C3/C4 vẫn riêng.
