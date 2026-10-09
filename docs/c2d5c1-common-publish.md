# C2d-5c1 — công bố metadata bản chung qua runtime

[Task](tasks/m6c2b2c2d5c1-common-publish.md) · [Inventory](c2d5c1-test-inventory.json) · [Caller map](c2d1-caller-inventory.json)

Phạm vi duy nhất là `POST /admin/common-copies/{item_id}/publish`. Base đã fetch và xác minh là PR #47 MERGED `04434e35e2b016601d3629a2d4afe3feb6042a6b`; tree bằng head PR `78fbe1d`. Nhánh `feat/m6c2b2c2d5c1-common-publish`. Head migration mã nguồn vẫn **0041**, không thêm migration/grants. Không kiểm revision DB dev. PR để review, không merge; **safety pending tới C4**.

Code chốt `520ac546abb4812dc1b2744e93437d4288dd5c57`; commit docs/inventory nằm sau code này. Đã đối chiếu selection với JUnit: **2.016/2.016 unique API pass**, 0 fail/error/skip, collect **3.923**, **1.907 not-run** ghi từng node trong inventory. Sáu nhóm không trùng/thiếu: publication 215 (191 ca publish mới, Chrome, legacy publication/foundation và schema), approval 295, review writers 220, JSON reads 325, original downloads 330, resources/citations/cleanup 631. **283 source hashes** trước/sau khớp; **7/7 mutants** fail assertion đúng mục tiêu, 0 setup error. Web **64/64**, typecheck/build đạt; Chrome proxy thật có trong selection. Đây là kết quả mới, không cộng preparation hay lịch sử PR #47.

## Hành vi và ranh giới

`api/common_publish.py` thay đường `publication.publish` cũ; helper publish đặc quyền được bỏ để fixture không dùng nhầm. Revoke/cleanup vẫn ở `publication.py`, chưa chuyển sang runtime C2d-5c2/C3. Route tiếp tục nhận principal từ `api/deps.py`, Origin/CSRF/session thật. API đòi boolean literal `true`, checksum SHA-256 lowercase đủ 64 ký tự; `false`, `null`, `1`, chuỗi hoặc thiếu xác nhận là 422.

Chỉ common copy từ approval đã kết thúc, `pending`, `metadata_only`, chưa active generation/tombstone, đúng một rights sống và đúng item/version/file/checksum/epoch. Không đọc private payload hoặc phục hồi grant duyệt. Frozen target gồm IDs, key, actual byte size, checksum, MIME, epoch, audience và các flags/conditions. Chỉ metadata trở thành `listed`: không job/index/chunk/provider call. Quyền tải original, full-text và RAG tiếp tục qua từng gate riêng; publish không đòi download/provider permission, cũng không bỏ conditions.

200 vẫn là `{"item": ...}`. DTO được đọc mới theo `review_copy_admin_clause` ở transaction response riêng. Sau known commit, revoke/cleanup có thể thắng trước response: DTO revoked/đã scrub là hợp lệ; không trả lại ORM hoặc DTO listed cũ. Không trả storage key, bytes, private text hay IDs thành công khi outcome uncertain.

## Phase, khóa và clock

| Pha | Thứ tự và ownership |
| --- | --- |
| Proof | Session ngắn: common item SHARE theo ACL → rights UUID → version UUID → file UUID SHARE → journal shared → exact pending tuple → current Admin/session/head/receipt/generation/CSRF và clock sau SQL cuối → rollback + close sạch. Missing/malformed ID hoặc denial muộn vẫn kiểm caller/gate hiện hành. |
| Hash | Sau khi trả connection, attempt lấy slot riêng và đọc original chung từng chunk qua storage executor. Kiểm actual bytes và SHA-256 của frozen target, đóng stream/socket. Không Session, connection, row/journal lock, spool, buffer toàn file hoặc put/delete object. |
| Acceptance | Session riêng: writer gate → Admin User NO KEY UPDATE → common item UPDATE → rights UUID → version UUID → file UUID UPDATE → kiểm frozen tuple/current state → item/publication/audit DML, flush tất cả FK/unique và `SET CONSTRAINTS ALL IMMEDIATE` → journal exclusive trực tiếp → scalar exact listed tuple/publication mới → actor/CSRF/clock hiện hành sau SQL cuối → stop check → commit. |
| Response | Session mới sau known commit: common item SHARE → resources SHARE → journal shared → Admin/caller hiện hành → current DTO/cleanup clock → actor/CSRF/TTL sau formatter SQL cuối → release sạch → 200. |

Không shared-to-exclusive upgrade, không User lock hay resource lock mới sau journal, không tombstone lock sau item. Unique checksum được DB phân xử trước journal. Khi unique index từ chối, rollback rồi lấy caller/gate proof ngắn mới trước 409; không SELECT để suy commit thành công. Deadline lock dùng config recovery hiện có. Clock không dùng `get_now` lúc request bắt đầu; proof/final binding/response đều kiểm lại thời gian sau SQL cuối.

Storage failure/deadline phải lấy proof ngắn độc lập sau khi SDK thread settle, trước denial/audit: caller/gate/resource hiện hành có thể thay verdict storage cũ bằng 401/403/503 hoặc `common_copy_revoked`. Audit best-effort không phải proof cấp quyền cho response. Denial audit là writer độc lập: User lock → current actor → ID/mã audit flush → exclusive journal → current actor/clock → commit. Không dùng `_refuse` cũ; audit không chứa checksum/title/evidence/private text. Lỗi audit không thay auth/business denial, không tạo publication và không retry mutation.

## Outcome, release và cancellation

`Outcome` do attempt sở hữu, không nằm trong Session: `commit_started`, `committed`, `rollback_confirmed`, `teardown_failed`. Đặt `commit_started` trước commit; chỉ đặt `committed` khi commit trả về. Rollback/close/invalidate không xóa hai fact đầu. Acceptance không savepoint và không tự retry.

| Kết quả | HTTP/hành vi |
| --- | --- |
| Caller/input/resource denial | 401/403/404/409/422 tương ứng. 401 xóa cookie; không DTO. Proof chưa release sạch thì không slot/storage. |
| Object missing/size/hash sai | 409 `common_file_integrity_failed`; không publication, không xóa original. |
| Storage lỗi | 503 `publication_storage_unavailable`; exception text/key/URL không vào response. Close lỗi trước release được thử close lần nữa, nhưng verification vẫn thất bại. |
| Slot hết/deadline | 503 `publication_busy` / `publication_verify_timeout`; deadline chỉ quan sát, không giết thread. |
| Lỗi trước commit | Lỗi auth/business giữ nguyên; lỗi bất ngờ 503 `publication_unavailable`, không success/retry. |
| Commit ném exception, DB landed hoặc rollback | 503 `publication_outcome_uncertain`; không response read, SELECT inference, ID, replay success hoặc retry. Original không bị put/delete. |
| Known commit + teardown/response lỗi | Giữ publication/original; 401/403/409 hiện hành hoặc 503 `publication_response_unavailable`. Không hứa mutation chưa lưu. |
| Known commit + proof response/release sạch | 200 DTO hiện hành. |

Session ngắn thử cả rollback và close kể cả khi một bước lỗi, rồi invalidate khi cần. Lỗi release giữ controlled denial nhưng không cho tiếp tục I/O hay trả success. Các regression gồm lỗi trước release thật, lỗi sau release và lỗi known commit; không chỉ ném exception sau khi đã release.

Attempt sở hữu slot/executor đến khi thread settle. `_thread` shield executor; cancellation/deadline không trả slot sớm. `receive` giữ task riêng; ASGI cancellation đặt stop và để task settle. Dừng trước acceptance hoặc sau binding trước commit rollback, không publish. Sau khi commit đã bắt đầu, client mất response không chứng minh thao tác chưa lưu. Proxy/UI không tự POST lại; form bỏ tick, nút bị disable khi đang gửi, unknown outcome hướng Admin tải lại trạng thái.

## Config và proxy

Giá trị scoped theo ủy quyền task, **đề xuất cần đo, không SLO/luật kiến trúc vĩnh viễn**:

- `files.publication_verify_max_concurrent=2` mỗi process, integer 1–64.
- `files.publication_verify_deadline_seconds=90`, >0 đến 600 giây; SDK/thread có thể tiếp tục đến khi I/O settle.
- `WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS=120`, integer 1–300, không nhỏ hơn normal timeout 15 giây. Chỉ exact POST `/admin/common-copies/<một segment>/publish`; approve/provider/revoke/route khác giữ budget riêng.

Compose source forward hai backend fields và proxy field. Không chạy API Compose image trong phép thử Chrome: socket Uvicorn chạy working tree, Next `start` chạy production build, PostgreSQL/MinIO là container cô lập. Đây không phải xác nhận wiring Compose/API dev hay triển khai production.

## Fixture và mâu thuẫn với code cũ

`tests.resource_fixtures.common_copy` dùng runtime approval và runtime publish với Admin/session/CSRF/clock thật. Source helper nhận dữ liệu tổng hợp lúc INSERT; không sửa file bất biến để dựng file 30 MiB. Original fixture chỉ override owner cho **legacy revoke**, publish dùng runtime. Adversary publish trong C2d-2b và C2d-5b chuyển sang `runtime_publish`. Test foundation direct tạo identity/bootstrap/login runtime thật trước approval/publish; revoke/cleanup giữ nhãn owner legacy.

`test_publication` ở base thiếu fixture `installation` trong namespace; đã tái hiện setup error trước assertion. Sau nhập fixture runtime, dùng `setup_engine` riêng cho corruption/cleanup/schema test; HTTP publish/reads/auth tiếp tục runtime. Test restore phải dispose cả harness runtime pool trước `pg_terminate_backend`; snapshot API dùng runtime credentials, không fallback owner. `recover` có hai replay passes: prefix identity/approval đã có trong snapshot góp vào `present`, nên kiểm exact count theo journal hiện hành; gate của scoped C1 là `ready` (expectation `open` trước C1 đã lỗi), đồng thời kiểm reconciliation hoàn tất và `safety_ready=false`. Giữ đầy đủ revoke/cleanup/ACL/private assertions. Nhãn metadata note cũ không còn đúng với gate full-text đã merge C2d-2b, nên expectation kiểm note hiện hành và các gate riêng; không bỏ invariant sản phẩm.

Test common cleanup dùng original fixture runtime cho auth/approval/publish/GET, owner riêng cho clone/corruption/revoke/cleanup adversary. Observer lock-wait dùng owner test vì runtime không được đọc `pg_stat_activity` fields của User khác; không nới grants để giữ expectation race. Không chứng nhận revoke/index/cleanup là runtime-ready. Các file resource có fixture publish bị đổi được đưa vào selection; inventory ghi union unique, từng command/node/JUnit, source hashes và not-run.

## Bằng chứng và giới hạn

Các lượt chuẩn bị có lỗi được giữ riêng trong inventory: fixture rights-count vướng unique origin review; fixture indexed thiếu active version; recovery closure cần actor NULL đúng CHECK; file 30 MiB không được UPDATE SourceFile bất biến; snapshot dùng storage placeholder dẫn đến cleanup incomplete; Node import/typecheck, Chrome port và runtime pool bị snapshot terminate. Chúng đã được sửa ở setup/caller tương ứng, không bỏ kiểm quyền hay expectation nghiệp vụ. Một lượt full publication trước chốt có 200 pass/1 fail ở expectation gate `open` cũ; sau sửa về `ready` của scoped C1 vẫn kiểm `safety_ready=false`. Lượt candidate tiếp theo bị dừng để bổ sung proof denial sau storage, không tính partial progress là pass cuối.

Lượt hồi quy JSON writer đầu trên source chốt có **219 pass/1 setup error**: bootstrap bị integrity gate 503 trước assertion concurrent-submit. Chưa ghi được reason và chưa xác định nguyên nhân. Ca riêng trên DB mới pass; toàn nhóm chạy lại trên source không đổi cũng pass. Observer ngoài repo chỉ ghi GateStatus đóng, không đổi verdict hay bypass gate. Hai lượt này được lưu riêng; kết quả rerun không xóa setup error trước đó. Legacy cleanup ban đầu dùng runtime để quan sát `pg_stat_activity` của User khác nên không thấy lock wait; đã đổi riêng observer sang owner test và giữ assertion lock-wait thật, không nới runtime grants.

Kết quả cuối và hashes nằm trong [inventory](c2d5c1-test-inventory.json); log/JUnit/collect/commands/credentials/journal chỉ ở thư mục temp ngoài repo. Bảy mutants có mục tiêu bỏ final actor/clock, frozen tuple, SHA-256, response proof, uncertain outcome, giữ connection trong I/O và bỏ proof denial sau storage đều phải fail đúng assertion, không setup error; source được khôi phục trước suite cuối. Không cộng lịch sử PR #47 hoặc lượt chuẩn bị vào pass mới.

Runtime tests dùng PostgreSQL pgvector 16, MinIO pinned, real runtime/recovery roles, genuine bootstrap/invitation/login/session/receipt/journal. Scoped cutover chỉ fixture business; test HTTP pending riêng giữ production gate thật và trả 503, health sống/readiness đóng, không hash/DML. Real 30 MiB MinIO hash đo peak Python allocation <12 MiB; đó là kiểm regression buffer, không benchmark process RSS/SLO.

Chrome cài trên host chạy headless qua Next proxy: Admin form unticked/checksum, publish metadata success, ordinary user ACL, invalid session Set-Cookie forwarding, hash 16 giây vượt normal 15 giây, test publish proxy 20 giây. Delay response 25 giây **sau known commit** gây 504: UI unknown, unticked, không re-POST và backend giữ listed. `route.abort` mô phỏng lỗi network trước delivery, không chứng minh commit-loss ở ca đó; 504 known-commit là ca riêng. Old approval/JSON writer Chrome được chọn hồi quy riêng.

Storage không immutable/versioned: bytes có thể đổi trong khe sau hash cuối trước commit. Regression ghi rõ publish có thể thành công trong khe đó, rồi download riêng từ chối SHA-256 sai. Không có lời hứa phát hiện mọi overwrite; index/download vẫn xác minh lại riêng. Session revoke/role/hold tới **sau boundary response cuối** có thể xảy ra sau proof hợp lệ như hợp đồng C2d hiện hành.

Không đọc/sửa `.env`, không query/migrate/restart dev, không provider hay đổi `pricing_verified`, không secret/journal/paper/backup/artifact trong repo. Không full business HTTP cutover, production immutable storage, deployment, C3/operator/delivery hoặc C4. Snapshot tests là PostgreSQL TEMPLATE cô lập + journal hiện hành, không diễn tập Compose backup/restore mới. Safety vẫn pending.

Sau khi tiếp tục task bị usage limit, phép inspect metadata thấy 24 container ngoài task có ID khác baseline ban đầu (24 thêm/24 vắng, cùng tên); chưa xác định nguyên nhân, không chứng minh nguyên trạng xuyên khoảng gián đoạn. Cleanup đầu đã dừng trước mọi delete. Hai container thử vẫn đúng exact ID/label/image/port/mount và sole volume consumer. Cleanup chỉ xóa hai ID/volume đó; snapshot **24 container hiện tại** khớp chính xác ID/image/StartedAt trước/sau cleanup. Inventory giữ drift, baseline cũ và mốc cleanup mới riêng; không suy nguyên nhân từ trạng thái hoặc query DB dev. Credentials thử được xóa, evidence ngoài repo được giữ để review.

## Bàn giao C2d-5c2

Revoke riêng cần runtime boundary: Admin User → item → rights/publication/jobs → flush toàn DML/FK → journal exclusive; mirror/confirm hẹp, tombstone/rights_epoch/cancel/retention cùng known commit, outcome độc lập và response proof mới. Cleanup/recovery phải giữ journal/tombstone/metadata contract. Race C2d-5c1 dùng **legacy journaled owner revoke** có nhãn; không gọi đó là triển khai hay pass runtime C2d-5c2.
