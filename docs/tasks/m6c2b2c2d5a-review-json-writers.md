# Prompt C2d-5a: tạo, rút và từ chối yêu cầu review

**Đã triển khai trên nhánh review; [handoff](../c2d5a-review-json-writers.md) / [inventory](../c2d5a-test-inventory.json) ghi kết quả theo source, chưa merge.** PR #45 đã merge `070cb37aeb35da62c088293eaff9c55dd58f1929` ngày 2026-10-07 (Asia/Saigon), chứa head `b23ff82`/code `4c49919`. Head migration mã nguồn hiện `0039`; không suy revision DB dev. Safety **pending tới C4**.

[Mục lục](../README.md) · [Quyết định giao](../decisions.md#giao-c2d-5a-sau-merge-pr-45-2026-10-07) · [Roadmap](../roadmap.md) · [Review PR #45](../reviews/pr45-c2d4c-original-downloads.md) · [Caller inventory](../c2d1-caller-inventory.json)

Bạn là coding agent chính. Hoàn thành một PR **C2d-5a**: triển khai, kiểm thử, docs, commit/push và mở PR **tiếng Việt** để review; không merge. Chuyển đúng ba JSON writer:

| Endpoint | Hợp đồng |
| --- | --- |
| `POST /review-requests` | Current User gửi đúng một version/file private của mình; xác nhận tường minh + notice version; lời khai `verified=false` |
| `POST /review-requests/{request_id}/withdraw` | Chủ request rút yêu cầu còn mở; kết thúc grant review đúng request |
| `POST /admin/review-requests/{request_id}/reject` | Current Admin từ chối request còn mở theo workflow; chỉ trả thin history sau khi đóng |

Approval/copy S3, publish/revoke/common-index, search/RAG/provider, xóa private, membership journal và C3/C4 là task riêng. Task này giữ quyền đọc GET đã merge và bổ sung đường ghi closure journal bằng runtime thật.

## 1. Base, tài liệu và môi trường

- Fetch lại, xác minh PR #45 MERGED và main chứa head/code trên; tạo `feat/m6c2b2c2d5a-review-json-writers` từ main đã xác minh. Nếu main tiến, đối chiếu diff liên quan và ghi base/head thực tế.
- Giữ **mọi docs chưa commit** của orchestrator: review PR #45, trạng thái merge và prompt này. Đọc diff, đưa cập nhật hợp lệ vào PR; không reset/clean làm mất file. Nếu dùng worktree, chuyển đủ và đối chiếu.
- Đọc [AGENTS.md](../../AGENTS.md), [quyền và dữ liệu](../access-and-data.md), [C1/role boundary](../c1-auth-safety-foundation.md), [C2a](../c2a-login-session-predicates.md), [C2b](../c2b-reset-status.md), [C2d-2b GET](../c2d2b-review-publication-json-reads.md), [C2d-3 writer](../c2d3-library-collection-json-writes.md), [final read clock](../c2d-final-read-clock.md), [original downloads](../c2d4c-original-downloads.md), [testing](../testing.md). Không xin lại nền C1/Origin đã chốt theo ủy quyền.
- Python/cài thư viện dùng `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip, không tạo venv. Dùng PostgreSQL/journal và MinIO nếu fixture cần, tất cả cô lập với credentials thử tường minh trước import fixture (`RAS_TEST_DATABASE_ADMIN_URL`), không fallback `.env`.
- Không đọc/sửa `.env` hoặc key, gọi provider, đổi `pricing_verified`, query/migrate/restart dev hoặc mở safety readiness. Không dùng credentials migrator/recovery làm fallback của API. Scoped cutover chỉ trong fixture nghiệp vụ; HTTP pending riêng không patch.

## 2. Khảo sát và ranh giới thay đổi

Đối chiếu `api/routes/reviews.py`, `review_closing.py`, `recovery.py`, `auth_safety.py`, `db_roles.py`, `review_publication_reads.py`, SQL/trigger closure và fixtures trước sửa. Báo mâu thuẫn cùng hành vi quan sát; các điểm đã biết:

1. Ba writer đang dựa principal/`now` ở dependency, commit rồi trả ORM/DTO cũ; chưa có current actor/clock cuối và proof response mới.
2. `record_review_closure` hiện chèn/update mirror trực tiếp. Runtime bị chặn raw DML trên `revocation_journal_records`, nên fixture download legacy dùng quyền đặc biệt cho withdraw/reject. **Không giữ bypass đó cho ba endpoint vừa chuyển.**
3. `require_final` xử lý pending session/reset/invitation, chưa xử lý closure review do chính transaction đang ghi. Sau flush request closed, counter unjournaled review có thể đóng gate; cần xử lý hẹp, giữ kiểm mọi closure khác.
4. `commit_close` cũ có thể replay rồi trả thành công sau commit exception. C2d-5a phải giữ outcome chưa xác định và hậu kiểm mới; replay không chứng minh mutation/audit của actor đã commit.

Tạo boundary ghi riêng hoặc mở rộng helper phù hợp. Không gắn shared read guard vào formatter dùng chung POST, nâng shared journal lên exclusive hoặc chuyển các writer ngoài scope theo tiện tay.

## 3. Quyền và lock map

Entry kiểm integrity trước row locks. Giữ session, Origin và CSRF thật từ dependency; nhận exact session/User, raw CSRF và callable clock cho kiểm cuối. Actor scalar/predicate dùng latest confirmed head/receipt, generation, active/proven, TTL, CSRF và role hiện hành, không profile/role ORM cũ. Lấy clock sau mọi SQL/lock wait **và sau chính predicate SQL**; không touch idle ở helper cuối.

Nhánh missing/closed/duplicate hoặc lỗi resource muộn cũng kiểm current caller/gate/clock qua boundary phù hợp trước trả denial; không dùng principal đầu request để quyết định mã lỗi sau SQL chậm. Không commit audit từ chối bằng một đường bỏ guard, hoặc nâng shared journal thành exclusive khi bổ sung audit.

| Writer | Thứ tự khóa/DML trước journal exclusive |
| --- | --- |
| Submit | Caller User **NO KEY UPDATE** → private item **SHARE** qua owner + exact version/file → INSERT request + audit → flush DML/FK |
| Withdraw | Caller User NO KEY UPDATE → owned request **UPDATE** → private item SHARE qua owner nếu cần cho target gate → closure + audit → flush |
| Reject | Caller Admin User NO KEY UPDATE → request UPDATE qua Admin workflow → closure + audit → flush; không cần khóa/đọc private item cho thin workflow |

Submit dùng item SHARE vì chỉ thêm request; không khóa request cũ sau item. Duplicate do partial unique hiện có xử lý thành `409 review_request_open`; không tạo vòng với approve/read đang khóa request → item. Nhiều UUID cùng lớp khóa theo thứ tự ổn định.

- Submit đòi current role User, private owner, đúng item–version–file/checksum/epoch và acknowledgement/notice; không tạo rights/common/job/index hoặc gửi provider. Không mặc định User có quyền phân phối.
- Withdraw theo ownership hiện hành; không tự thêm điều kiện role User nếu owner đã đổi role mà contract vẫn cho rút. Request closed trả `409 review_request_closed`; foreign/missing/malformed giữ 404 như contract.
- Reject đòi current Admin và request open trong workflow; **grant đọc file mất không ngăn Admin đóng request**. Không dùng quyền thin history làm quyền đọc payload private, hoặc trả declaration/email/title/file/URL sau reject.
- Mọi FK/audit và row lock cần thiết phải xong trước journal. Sau journal không lấy User/session/request/item lock mới hoặc flush DML tạo chờ FK chưa được giữ. Recheck actor/resource/notice bằng scalar SQL dưới khóa, rồi clock cuối trước commit. Journal read/append/fsync theo protocol nằm trong transaction; task không có storage/parser/provider I/O nghiệp vụ.

## 4. Closure journal bằng runtime và gate

**Quyết định scoped của orchestrator theo ủy quyền:** cho phép migration mới sau head thực tế (dự kiến `0040`) để thêm đường mirror `review_closure` hẹp. Giữ nguyên mọi migration đã merge. Tên/helper cụ thể do agent chọn và ghi rationale.

- Runtime vẫn không raw INSERT/UPDATE/DELETE protected tables, không owner/DDL/TEMP/BYPASSRLS hoặc role fallback. Dùng routine allowlist tối thiểu theo domain review closure, NOLOGIN definer và fixed search_path như C1; provisioning mới **và upgrade DB đã provision roles** đều có đúng EXECUTE/owner/ACL. Không cấp broad write vào journal để sửa permission denied.
- Routine writer chỉ nhận record review closure hợp lệ, derive typed mirror từ chính line, `journal_confirmed=false`. Kiểm request/body ID, exact item/version/file, status/reason/time, shape/type/NULL/missing/extra fields theo contract; SQL kiểm độc lập với Python. Writer nhắm request thực đang đóng; recovery vẫn nhận record request vắng theo hợp đồng snapshot cũ. Không thêm FK làm hỏng ca replay request absent.
- Giữ append-only và false → true sau append + fsync thành công. Force các binding/closure/FK constraints cần thiết **trước byte journal đầu**; input sai phải không đổi file/mirror/request. Ghi thất bại không đánh dấu confirmed, kể cả line đủ mà fsync lỗi. Routines không tự append file hay suy fsync từ flag caller/GUC.
- Gate writer lấy journal exclusive trực tiếp sau rows/flush. Nếu phải xét closure review đang chờ mirror, chỉ xét **exact closure do transaction này khóa từ open rồi stage**, bound với tuple/quyết định/thời điểm; không trừ toàn counter, bỏ integrity check hoặc đưa UUID tùy ý thành bypass. Unjournaled closure khác, unconfirmed mirror khác, hold, file ahead/out-of-sync và pending cutover vẫn đóng gate. Actor/caller mới không có quyền dùng trạng thái pending này để đọc grant.
- Sau actor/resource kiểm hợp lệ, mirror false + kiểm binding → append/fsync → confirm → actor/resource/clock kiểm lại → commit. Lỗi journal có thể vẫn commit closure fail closed với mirror false và trả lỗi; đừng dùng gate thường để vô tình bỏ mất chính closure fail closed này. Lỗi khác không được che bằng nhánh journal-write-failed.
- Giữ replay/backfill/hold tương thích, `closed_by_recovery` không phải approved và không tạo rights/common/User/receipt. Session/reset/invitation/auth transition và projection không được nới guard. Nếu sửa helper chung, hồi quy caller cũ, đặc biệt approve còn riêng; không tuyên bố approve đã chuyển runtime.

## 5. Commit, durable intent và response

Outcome phải do operation sở hữu, độc lập exception rollback/close: staged record/append outcome/commit_started/committed giữ được dù lỗi xảy ra sau append hoặc khi teardown. Không suy commit từ SELECT, error string, Session còn sống hoặc replay; không tự retry POST.

| Cửa sổ | Hành vi bắt buộc |
| --- | --- |
| Known precommit fail, chưa có durable intent | Rollback mutation/audit; denial/lỗi có kiểm soát, không success DTO |
| Append none/torn/full-fsync-error, closure commit xác định | Grant kết thúc; mirror false, gate đóng; `503 review_journal_unavailable` hoặc mã hiện hành tương ứng |
| Durable intent rồi rollback, actor hết TTL sau fsync, confirm/commit lỗi hoặc outcome uncertain | Không success DTO. Journal có thể khiến grant kết thúc qua recovery; không hứa request vẫn open/chưa lưu. Gate file ahead/unconfirmed đóng tới đối soát |
| Commit exception, kể cả remote commit đã xảy ra | Trả lỗi controlled outcome chưa xác định; không báo thành công sau replay, không phát DTO cũ hoặc retry mutation |
| Known commit rồi hậu kiểm actor/resource/DB thất bại | Giữ mutation/audit đã có; 401/403/409/503 phù hợp, không DTO cũ; lỗi response không chứng minh mutation chưa lưu |
| Known commit và proof response mới đạt | DTO từ transaction/quan sát mới; submit 201, withdraw/reject 200 theo contract |

**Chọn cho task:** replay nếu có đường hạn chế hợp lệ chỉ dùng đóng grant, không chứng nhận business success. API runtime không dùng credentials recovery/migrator; khi không replay được hợp lệ, trả pending-recovery controlled và giữ gate đóng để CLI `recover` đối soát. Teardown thử rollback/close/invalidate trên mọi đường, không hạ uncertain thành precommit hoặc che mã lỗi chính.

Hậu kiểm sau known commit dùng transaction mới: request SHARE → item SHARE chỉ theo owner/exact read grant → journal shared → current caller/role/CSRF/gate → DTO scalar đọc lại → clock sau SQL cuối. Có thể tái dùng boundary C2d-2b phù hợp, không dùng ORM/proof trước commit. Owner đọc lịch sử cả closed; Admin reject chỉ thin history. Submit có thể đã bị reject/withdraw trước response: trả trạng thái hiện hành nếu được đọc, không ép request phải open để báo đã tạo. Note/UI không nói request đã đóng vẫn đang chờ duyệt.

## 6. Kiểm chứng cần có

Fixture auth dùng bootstrap/invitation/login/runtime/head/receipt/journal thật. Resource setup/corruption riêng có đặc quyền trong DB thử; **ba POST này dùng runtime thật**, không override principal/predicate/grants/quarantine/held. Gỡ withdraw/reject khỏi privileged setup trong `original_fixtures.py` và fixture tương tự; approve/publish/revoke legacy còn đặc quyền phải ghi đúng giới hạn. HTTP pending riêng không patch: health sống, ready/ba POST 503, không business/journal changes.

| Nhóm | Ca bắt buộc |
| --- | --- |
| ACL/contract | A/B/Admin, cùng bytes/item khác; submit exact tuple/notice/ack; owner withdraw; current Admin reject; closed/inactive-grant workflow; foreign/malformed; raw request/URL/field lạ; audit IDs/mã, không text/secret |
| Actor/clock | Revoke/disable/held/quarantine, binding sai, generation/head/receipt/CSRF/role đổi; dependency → row wait → final resource SQL/predicate → append/fsync → commit → fresh response. Idle/absolute đúng biên và sát biên; không touch idle |
| Routines/gate | Runtime hợp lệ pass; raw protected writes/PUBLIC/DDL/GUC/role fallback fail; NULL/sai UUID/missing/type/tuple/status/reason/time fail trước append; confirm-only; pending closure đúng một transaction không che closure/mirror/hold khác |
| Race/locks | Hai submit cùng file chỉ một open; submit đua withdraw/reject; withdraw đua reject/approve; auth writer đua closure; GET history/original đua đóng. Barrier tất định cho cả hai thứ tự, quan sát lock wait/no deadlock; không khóa hàng mới sau journal |
| Journal/outcome | None/torn/prefix/full-fsync-error, lỗi sau append/confirm, trước commit, rollback/landed-uncertain/known commit + rollback/close lỗi; clock qua hạn trong fsync; gate/rows/file/audit/mã/no stale DTO/no retry/connection release |
| Restore | Snapshot request open → runtime withdraw/reject → restore/migrate/recover bằng journal độc lập hiện hành; grant không sống lại, URL review cũ 404; replay idempotent/present/absent, không dựng common/receipt. Bảo vệ session/reset/invitation giữ nguyên |
| Response/proxy | Proof transaction mới sau known commit; owner history và thin Admin output; clear-cookie/no cached private DTO; JSON proxy thật cho submit/withdraw/reject; Chrome form với checkbox ban đầu rỗng nếu công cụ có. Nếu chỉ handler/HTTP, ghi rõ |
| Migration | Upgrade DB head `0039` có dữ liệu/roles/closures, ORM/schema xanh, roles sau provisioning lặp giữ privileges hẹp; snapshot cũ/hold-before-migrate và chính sách downgrade populated theo nền C1 |

Chạy đủ test mới và hồi quy theo risk: review requests/closure journal; C2d-2b GET và C2d-4c download khi fixture/shared helper đổi; actor/clock/role/provisioning cùng auth journal bị ảnh hưởng; submit/approve cũ khi dùng chung formatter/closure helper; ORM/schema. Chọn và giải thích selection, không mở scope production writer khác để qua test. Web đổi thì test/typecheck/build; JSON proxy/browser smoke có evidence riêng. Không provider thật.

Đối chứng ngoài suite cuối: bỏ current role/clock cuối, exact owner/tuple, pending counter guard/binding SQL, proof response mới hoặc outcome độc lập teardown phải làm test fail có ý nghĩa. Khôi phục byte/hash trước suite; phân biệt failure assertion với setup error và ca chỉ đổi deny code. Không sửa code trong lúc suite chạy.

## 7. Docs, giao PR và hoàn thành

- Thêm `docs/c2d5a-review-json-writers.md` và inventory JSON: route/actor/lock/DML/mirror/append/commit/response, privilege/pending semantics, mã lỗi và cửa sổ journal. Ghi tên helper/routine, bàn giao approval/revoke task sau và caller còn mở.
- Đồng bộ README, docs README, decisions, testing, data-model/access, roadmap, dev-setup, AGENTS và caller inventory. PR #45 đã merge; giữ số review/agent theo source riêng, không coi đã rerun tại merge. Các quyết định scoped trong task do orchestrator chọn theo ủy quyền, không phải quyết định trực tiếp mới của chủ dự án hay luật kiến trúc vĩnh viễn.
- Inventory current collect/selection/not-run + JUnit unique/no missing/duplicates; code SHA và hash trước/sau suite. Ghi riêng mọi preparation/fault/mutant/fail/error/skip/not-run; số lịch sử 991/350/57 không là pass mới. Chỉ thêm lệnh thực chạy vào AGENTS/dev-setup.
- Giữ artifact/journal/paper/credentials ngoài repo. Cleanup đúng tài nguyên thử sau exact ID/name/label/image/port/mount/sole-consumer checks; đối chiếu tài nguyên có sẵn, không đụng dev. Báo giới hạn crash/restore/storage độc lập; không tuyên bố production durability hoặc SLO.
- Commit giữ tác giả/committer hiện có, không AI co-author/trailer. Đã được giao commit/push/mở PR tiếng Việt cho người review, không xin lại quyền và không merge. Báo base/head/code, file, migration, API/web/mutant/proxy/drill, cleanup, lỗi đã gặp và phần chưa chạy.

**Hoàn thành:** ba writer chạy bằng runtime thật, actor/resource/clock bảo vệ mutation; closure journal hẹp và fail closed đúng cửa sổ lỗi; known commit có proof response mới, uncertain không success DTO/retry; quyền review không sống lại sau restore có journal hiện hành. Approval và writer/provider khác còn riêng, safety pending tới C4.
