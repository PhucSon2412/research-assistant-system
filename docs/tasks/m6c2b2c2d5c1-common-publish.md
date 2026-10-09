# Prompt C2d-5c1: công bố metadata bản chung qua runtime

PR #47 đã **MERGED** tại `04434e35e2b016601d3629a2d4afe3feb6042a6b` ngày 2026-10-08, chứa code P2 `50393d1` và head `78fbe1d`. Fetch xác nhận `origin/main` tại merge này, tree giống head PR; head migration mã nguồn `0041`. Không kiểm revision DB dev. Đây là hợp đồng giao task; **đã triển khai trên nhánh review, chưa merge**. [Handoff](../c2d5c1-common-publish.md) và [inventory mới](../c2d5c1-test-inventory.json) ghi bằng chứng hiện hành; safety **pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-5c1-sau-merge-pr-47-2026-10-08) · [Handoff approve](../c2d5b-review-approval-original-copy.md) · [Review P2](../reviews/pr47-c2d5b-review-approval-original-copy.md#review-lại-p2-tại-78fbe1d) · [Caller map](../c2d1-caller-inventory.json)

Bạn là coding agent chính. Hoàn thành một PR **C2d-5c1**: khảo sát, triển khai, kiểm thử, docs, commit/push và mở PR **tiếng Việt** để review; **không merge**. Chuyển đúng **`POST /admin/common-copies/{item_id}/publish`** sang boundary runtime thật. Admin công bố metadata của bản chung đã được duyệt; kiểm original đúng file/version/checksum ngoài khóa và kiểm actor/quyền lại trước commit, rồi lấy response bằng proof mới.

Revoke được giao ở **C2d-5c2** vì có journal/mirror/retention riêng; common-index, cleanup/reconcile vận hành, search/RAG/provider, C3/operator/delivery và C4/cutover tiếp tục riêng. Không mở safety ready hoặc gọi provider trong PR này.

## 1. Base, tài liệu và môi trường

- Fetch lại, xác minh PR #47 MERGED và main chứa các SHA trên; tạo `feat/m6c2b2c2d5c1-common-publish` từ main đã xác minh. Nếu main tiến, ghi base/head thực tế và kiểm diff liên quan.
- Giữ **mọi docs chưa commit của orchestrator**, gồm review lại PR #47, cập nhật trạng thái merge, caller map và prompt này. Không reset/clean làm mất chúng; chuyển đầy đủ sang nhánh/worktree mới và đưa vào PR.
- Đọc [AGENTS](../../AGENTS.md), [quyền](../access-and-data.md), [C1](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2d-2b](../c2d2b-review-publication-json-reads.md), [C2d-3](../c2d3-library-collection-json-writes.md), [clock](../c2d-final-read-clock.md), [download](../c2d4c-original-downloads.md), [approve/P2](../c2d5b-review-approval-original-copy.md) và [testing](../testing.md). Nền C1/Origin đã chốt theo ủy quyền, không xin lại.
- Python/cài thư viện chỉ `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip, không venv. Cấp explicit `RAS_TEST_DATABASE_ADMIN_URL`, runtime/recovery/storage/journal thử **trước import fixture**; không fallback đọc `.env`.
- PostgreSQL/MinIO/journal, port, credentials, container và volume thử phải cô lập. Không đọc/sửa `.env` hoặc key, query/migrate/restart dev, đổi `pricing_verified`, gọi provider hoặc dùng API fallback migrator/recovery. Scoped cutover chỉ fixture business; HTTP pending riêng dùng gate thật, không patch production.

## 2. Khảo sát và các mâu thuẫn đã thấy

Đọc `api/routes/publications.py`, `app/publication.py`, `api/review_approval.py`, `api/review_publication_reads.py`, `api/original_downloads.py`, `api/resource_writes.py`, storage/config/proxy, `authz.py`, các trigger publication/revocation, `db_roles.py`, fixtures và test publication.

1. `publication.publish` khóa item và rights rồi gọi `_check_common_object`/`verified_read`: storage/hash hiện nằm **trong transaction giữ khóa**. Revoke/auth writer không được phải chờ thao tác đọc object của publish.
2. Route dùng principal và `get_now` từ đầu request; chưa final-check latest session/head/receipt/generation, role, CSRF và TTL bằng clock mới sau I/O/chờ khóa/SQL cuối.
3. `_refuse` rollback rồi audit/commit bằng principal cũ. Không dùng helper này làm đường bỏ qua actor/gate mới.
4. Commit lỗi chưa có outcome độc lập; response dùng `_copy_out` trên Session sau commit mà chưa có resource/caller proof mới.
5. PR #47 ghi `test_publication` not-run và có lỗi setup tại base. Lượt giao này chưa tái hiện nguyên nhân; khảo sát rồi chuyển fixture theo hợp đồng runtime, không suy lỗi chỉ từ import/helper hoặc đổi expectation nghiệp vụ để làm xanh test.

Ghi hành vi quan sát và cách xử lý vào decisions/handoff. Chỉ refactor helper dùng chung nếu có lý do cụ thể và hồi quy caller liên quan; giữ revoke/cleanup exports cần cho fixture/recovery, không tiện tay chuyển writer khác.

## 3. Hành vi sản phẩm giữ nguyên

- Chỉ Admin **hiện hành**, session usable/latest confirmed head/receipt/generation, Origin và CSRF hợp lệ. Confirm checkbox mặc định không tick; API nhận boolean `confirm_publish=true` tường minh, không coi `1`, chuỗi, null hoặc giá trị thiếu là xác nhận.
- Chỉ bản common được duyệt, `pending`, `metadata_only`, chưa active generation, chưa tombstone/thu hồi. Rights sống, đúng item/version/file/checksum/epoch; checksum User/Admin xác nhận phải đúng file đang kiểm. Không đọc private payload hoặc mở lại review grant đã kết thúc để publish.
- Giữ các mã nghiệp vụ: `404 not_found`, `409 already_published`, `409 common_copy_revoked`, `409 rights_not_publishable`, `409 checksum_confirmation_mismatch`, `409 duplicate_published_checksum`. Revoke thắng trước final lock phải cho `common_copy_revoked`, không lặp lỗi snapshot cũ bị trộn với rights mới.
- Một live publication cho cùng checksum SHA-256 theo unique index hiện hành. Hai bản cùng bytes không tự hợp nhất; bản private/collection/reference của User không đổi, không rò private duplicate sang User/Admin thường.
- Publish tạo `CommonPublication`, chuyển `pending → listed`, vẫn `metadata_only`; không tạo job/chunk/index hoặc gọi provider. Quyền tải original vẫn riêng; `scope_conditions` không tự bị bỏ, search/RAG vẫn cần index và `permits_*` tương ứng. Không đặt điều kiện download/provider làm điều kiện bắt buộc để công bố metadata.
- Giữ HTTP 200 dạng `{"item": ...}` với DTO Admin hiện hành tương thích UI; không trả ORM cũ, object key, bytes hoặc nội dung private. DTO phải lấy qua quyền history/common-copy hiện hành ở transaction response mới; không giả định trạng thái vẫn `listed` nếu revoke/cleanup thắng sau commit.

## 4. Boundary, I/O và khóa

Chọn cho task một attempt sở hữu frozen scalar target, slot, storage stream/executor và outcome. Hash trực tiếp theo chunk; không cần spool nếu chỉ xác minh checksum. Nếu tái dùng helper có spool, attempt giữ ownership tới khi thread settled. Không buffer cả file vào RAM, copy object mới, put hoặc delete original trong publish.

| Pha | Hợp đồng |
| --- | --- |
| Proof trước I/O | Session ngắn: common item SHARE qua `review_copy_admin_clause` → rights/resource locks cần thiết theo cùng thứ tự → journal shared → exact common tuple/pending/checksum/gate và current Admin/CSRF/clock sau SQL cuối. Thiếu/sai ID hoặc denial muộn cũng kiểm caller phù hợp. Trả connection sạch **trước cấp slot/đọc storage** |
| Hash ngoài DB | Đọc original chung theo chunk, giới hạn actual bytes, so kích thước và SHA-256 với frozen tuple; đóng stream/trả socket. HEAD/ETag chỉ được từ chối sớm, không thay SHA-256. Không Session/connection/row lock hoặc journal lock trong storage |
| Acceptance | Admin User NO KEY UPDATE → common item UPDATE → rights UPDATE (thứ tự cố định nếu nhiều hàng) → version/file locks cần thiết → kiểm lại frozen IDs/key/size/checksum/epoch và điều kiện publish → publication/item/audit DML + mọi FK flush → journal exclusive **trực tiếp** → scalar actor/role/CSRF/resource/gate và clock sau SQL cuối → commit |
| Response | Transaction mới: common item SHARE qua quyền Admin phù hợp → resource locks cần thiết → journal shared → re-read DTO/current caller/CSRF/clock sau SQL cuối; release sạch trước trả success. Không khóa User hay resource mới sau journal |

- Không mang dependency Session sống qua storage, không upgrade shared journal sang exclusive. Acceptance không dùng proof/caller/time cũ để cấp quyền; final binding kiểm sau DML phải mong đợi trạng thái `listed` và publication mới, không kiểm nhầm `pending`.
- Flush FK/audit/unique-index trước journal exclusive để không chờ User/item khác khi đang giữ journal. Giữ item → rights → publication/job order tương thích revoke; không khóa tombstone sau item. Race phải kiểm cả hai thứ tự với barrier/lock-wait thật.
- Proof, denial, response và teardown thử cả rollback/close, invalidate khi cần; lỗi release không cho phép tiếp tục I/O hoặc trả DTO. Giữ lỗi auth/nghiệp vụ có kiểm soát, cancellation/thread ownership và outcome. Có regression riêng cho lỗi trước release, không chỉ ném lỗi sau rollback/close thật.
- Hash ngay trước acceptance, không thêm lời hứa phát hiện mọi overwrite: khe giữa hash cuối và commit chỉ còn DB. Bytes bị thay trong khe đó có thể chưa bị phát hiện tại publish; các route download/index vẫn xác minh riêng. Ghi giới hạn này; immutable object/versioned storage production không được coi là đã có.
- Denial audit nếu cần là best-effort writer mới với current actor, IDs/mã, không secret/title/checksum/evidence; lỗi audit không thay denial hoặc tạo mutation publish. Không gọi `_refuse` cũ để commit bằng caller cũ.

## 5. Cấu hình, outcome và proxy

Giá trị khởi đầu scoped theo ủy quyền, **đề xuất cần đo, không SLO**: pool xác minh publish riêng **2 mỗi process**, deadline quan sát I/O **90 giây**, exact POST publish proxy **120 giây** (integer 1–300, không nhỏ hơn timeout thường). Backend/proxy dùng config có test; đề xuất tên `files.publication_verify_max_concurrent`, `files.publication_verify_deadline_seconds`, `WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS`. Route approve/khác giữ cấu hình riêng. Không thêm timeout cho cả `/admin/*`.

Deadline/cancel không giết Python thread hoặc chứng minh storage dừng. Attempt giữ slot/stream đến khi thread settle; cancellation thắng trước acceptance thì không publish, không auto retry. Không còn request theo dõi không phải là bằng chứng backend chưa commit; UI phải diễn đạt đúng.

| Outcome | Yêu cầu |
| --- | --- |
| Proof/input/gate denial | 401/403/404/409/422 phù hợp; không publication/DML publish, không đọc storage nếu proof đầu không sạch |
| Object thiếu/sai bytes/size | `409 common_file_integrity_failed`, không publication; không xóa original |
| Storage/slot/deadline lỗi | JSON 503 có mã riêng ổn định; resource được trả sau thread settled, không exception payload/internal URL |
| Known precommit rollback | Lỗi có kiểm soát, không success/retry; release tài nguyên, giữ original |
| Commit đã bắt đầu rồi ném lỗi, dù DB landed hay rollback | `503 publication_outcome_uncertain`, không success/DTO/ID/retry. Outcome giữ độc lập rollback/close/invalidate; không SELECT/replay để suy commit |
| Known commit, response/teardown lỗi | 401/403/409/503 thích hợp (`publication_response_unavailable` cho lỗi bất ngờ); giữ publication/metadata/original, không hứa chưa công bố hoặc tự đảo trạng thái |
| Known commit + proof response mới/release sạch | 200 DTO hiện hành |

Publish không tạo auth/revocation event mới; journal lock dùng kiểm gate. Runtime đã có quyền DML bảng business không protected: **ưu tiên không migration/grants mới**. Nếu khảo sát chứng minh thiếu guard/quyền thiết yếu, dùng revision mới sau head thực tế (dự kiến `0042`), không sửa `0041` hoặc các revision đã merge, không mở raw protected writes/SECURITY DEFINER generic. Ghi lý do và kiểm ORM/privileges/upgrade phù hợp.

UI publish phải bỏ tick sau lỗi/mất response và không tự POST lại. Unknown outcome, proxy 504/502/đứt body cần thông báo kết quả chưa xác định, hướng Admin tải lại trạng thái; không khẳng định thao tác thất bại hoặc đã thành công. Không đổi approve/revoke/index form ngoài phần chung thật sự cần và đã hồi quy.

## 6. Kiểm chứng bắt buộc

Test PostgreSQL/MinIO/journal và role runtime thật; auth/bootstrap/invitation/login/receipt/predicate thật. Business scoped cutover chỉ fixture; test HTTP pending riêng không patch gate. Resource corruption/revoke/cleanup legacy chỉ setup/đối thủ đặc quyền có nhãn, không được tính là runtime writer đã chuyển.

| Nhóm | Ca cần có |
| --- | --- |
| Sản phẩm/ACL | Admin success; A/B/anonymous/role đổi; đúng/sai ID và private item; false/null/1/chuỗi confirm; checksum sai, rights count/file/version/epoch/tombstone, listed/revoked; flags/conditions không bị nới, chỉ metadata, không job/index/provider |
| Clock/auth | Idle/absolute ở sát biên và đúng biên, clock sau SQL cuối/chờ lock/hash; logout/reset/disable/role/CSRF change/hold trước acceptance và trước response. Không cookie/DTO nếu proof cuối thất bại |
| Storage/I/O | File 30 MiB tổng hợp, missing/truncated/too large/bytes khác cùng size; stream/read/close lỗi, cap/deadline/cancel. Chặn hash bằng event: revoke và auth writer vẫn commit khi đang đọc, DB pool không giữ connection; không put/delete |
| Race | Publish cùng item, hai Admin publish hai bản cùng checksum, publish đua revoke cả hai thứ tự, epoch/hold/auth change; unique winner và lỗi nghiệp vụ đúng, không deadlock; GET/common original và response đua revoke/cleanup |
| Commit/release | Commit landed/rollback/uncertain; lỗi rollback/close trước release ở proof đầu/denial/response/acceptance, lỗi teardown known commit. No 500/no leak; outcome không bị teardown xóa; known commit giữ row/object; không retry hoặc report success từ SELECT |
| Recovery | Backup/snapshot hợp lệ → publish → revoke legacy journaled → restore với journal mới → recover: thu hồi không mất, không lộ original/metadata common, private cùng bytes nguyên. Không replay publish positive bị thiếu trong snapshot; ghi rõ phạm vi legacy revoke |
| Pending | HTTP thật khi safety pending: 503, health còn sống/readiness đóng, không hash/storage/DML publish; không dùng fixture override để chứng minh gate production |
| Proxy/browser | Chrome qua Next proxy với role/session thật, form unticked, publish success/metadata và checksum đúng, ACL denial. Hash 16 s vẫn thành công qua timeout mới; proxy mất phản hồi/504 hiển thị unknown và không POST lại. Ghi source API/socket/Compose và mọi scoped fixture, không chỉ gọi thẳng 8000 |

Chuyển **fixture publish** sang helper runtime có principal/session/CSRF/clock thật, đặc biệt `tests.resource_fixtures.common_copy`, `original_fixtures` và legacy publication tests; không monkeypatch publish thành success hoặc dùng privileged publication làm setup cho success mới. Giữ helper approve runtime từ PR #47. Revoke/index legacy giữ riêng và ghi not-run/giới hạn.

Chạy nhóm publish mới, các ca publication/duplicate/race tương ứng, hồi quy approve/P2, C2d-2b reads, C2d-4c originals và các resource suites có fixture publish bị đổi. Nếu sửa code dùng chung, mở selection theo caller bị tác động cụ thể. ORM/schema và head/noop migration phải xanh; nếu có revision mới kiểm upgrade có dữ liệu/grants tương ứng. Không bỏ expectation để tránh lỗi fixture, không gọi test chưa chạy là pass. Web tests/typecheck/build và Chrome khi đổi proxy/UI.

Đối chứng tối thiểu có mục tiêu: bỏ actor/clock cuối, frozen binding, SHA-256, response proof, giữ outcome khi uncertain và I/O ngoài khóa. Mỗi mutant phải fail assertion đúng mục tiêu, không setup error; mutant yếu sống sót ghi riêng và làm mạnh nếu cần. Khôi phục source **trước suite cuối**. Giữ source hashes trước/sau, collect/selection/JUnit unique, pass/fail/error/skip/not-run; được chia suite khi đối chiếu union không thiếu/trùng. Không cộng test lịch sử PR #47 vào kết quả mới hoặc test lại chỉ để tăng số lượng.

## 7. Hoàn thành và bàn giao

- Tạo handoff `docs/c2d5c1-common-publish.md` và inventory test mới; cập nhật caller map, decisions, testing, roadmap, README/docs README, AGENTS/lệnh đã chạy nếu cần. Quyết định scoped theo ủy quyền không biến thành bất biến repo.
- Giữ report review lại PR #47 và toàn bộ docs orchestrator; số reviewer 66/agent 1.727/web 62 lịch sử không thành pass tại merge/task này.
- Không ghi journal/artifact/backup/paper/credentials vào repo. Kiểm doc links/control chars/diff; quét secret bằng fingerprint, không in key/password/token. Chỉ dọn container/volume thử đã kiểm exact ID/label/mount/sole consumer; chứng minh tài nguyên có sẵn không đổi.
- Commit/push và mở PR tiếng Việt, không thêm AI author/committer/Co-authored-by hay dòng quảng bá AI vào mô tả PR. PR để review, không merge.
- Báo base/code/head, migration thực tế, routes/fixture đã chuyển, test/evidence/not-run và lỗi các lượt chuẩn bị, cleanup và giới hạn. **Safety vẫn pending tới C4.** Bàn giao C2d-5c2 revoke: Admin User → item → rights/publication/jobs → flush → journal; runtime mirror/confirm hẹp, tombstone/epoch/cancel/retention và cleanup/recovery riêng. Không nói phần đó đã triển khai trong C2d-5c1.
