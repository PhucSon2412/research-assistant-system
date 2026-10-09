# C2d-5b: phê duyệt review và sao chép original qua runtime

**Review lại P2 PR #47 tại `78fbe1d`:** P2 đã xử lý, không phát hiện điểm chặn merge mới trong phạm vi approve/copy. Reviewer thực chạy **66/66 API** và đối chiếu riêng artifact **1.727** của agent; web/Chrome không chạy lại trong lượt này. [Report và giới hạn](reviews/pr47-c2d5b-review-approval-original-copy.md#review-lại-p2-tại-78fbe1d). PR đã merge `04434e3`; chưa rerun tại merge, safety pending tới C4.

**Review trước sửa tại `bac3739` (lịch sử):** [report/P2 và prompt sửa](reviews/pr47-c2d5b-review-approval-original-copy.md). Reviewer chạy selection 281/281, web 62/62; bốn regression teardown proof đầu fail đúng HTTP 500, hai control pass. Chưa merge; safety pending tới C4. Artifact agent vẫn giữ theo source cũ, không ghi thành pass sau sửa.

**Trạng thái:** đã merge PR #47 `04434e3` (2026-10-08), nhánh `feat/m6c2b2c2d5b-review-approval-original-copy`. Kết quả kiểm giữ theo source trước merge, không là pass mới tại merge. Base fetch/xác minh `8ff5b4ed5f643a4618f70b73550cdcce465181ab` (PR #46 MERGED, chứa head `7938153`/code `1666273`). Head Alembic source trước sửa `0040` (kiểm bằng ScriptDirectory/graph, không query DB dev); thêm `0041`, không sửa migration đã merge. **Safety pending tới C4**; publish/revoke/common-index, provider, C3/C4 riêng.

[Task](tasks/m6c2b2c2d5b-review-approval-original-copy.md) · [Quyết định](decisions.md#triển-khai-boundary-c2d-5b-2026-10-07) · [Inventory](c2d5b-test-inventory.json) · [Caller map](c2d1-caller-inventory.json) · [C2d-5a](c2d5a-review-json-writers.md)

Nhánh giữ nguyên toàn bộ docs orchestrator chưa commit lúc bắt đầu (report PR #46, task C2d-5b, trạng thái merge trong README/decisions/roadmap/testing/AGENTS/caller map) và cập nhật tiếp trên đó.

## Sửa P2 review PR #47

[Report](reviews/pr47-c2d5b-review-approval-original-copy.md) tái hiện: proof đầu và nhánh ID sai nằm ngoài khối chuyển lỗi của attempt; `rollback()`/`close()` của Session proof nằm ngoài `except`, nên lỗi teardown thoát thành HTTP 500 không phải JSON.

Sửa trong `api/review_approval.py` (không đổi `0041`, grants, SQL binding, lock order hay outcome acceptance):

- Helper `_short(function, code)` sở hữu Session ngắn của proof đầu, proof giữa pha, caller-denial (ID sai), response sau known commit và audit từ chối. Luôn thử cả rollback và close; nếu một trong hai lỗi thì `invalidate()` để trả/bỏ connection và dừng bằng `ApiError` có kiểm soát — proof chỉ được tính khi Session đã release sạch, nên không có slot/storage/mutation sau proof lỗi. Denial do chính proof ném (401/403/404/409/422) được giữ; BaseException không phải Exception được ném lại nguyên vẹn (cancellation/ownership thread không đổi).
- `attempt` bọc nhánh ID sai và proof đầu: lỗi không phải `ApiError` → `503 approval_unavailable`.
- Mã lỗi: proof/caller-denial `503 approval_unavailable`; response sau known commit `503 approval_response_unavailable` (rows/object giữ).

| Kiểm chứng P2 | Kết quả |
| --- | --- |
| 18 regression mới trên code `b7ba608` (ID hợp lệ/sai × rollback/close/cả hai/lỗi trước release; proof giữa pha; response) | **10 fail đúng lỗi** (8 HTTP 500 không JSON, 2 rò connection `checkedout=1`), các control/ca khác pass |
| Cùng 22 node `-k teardown_fault` sau sửa | **22/22 pass** |
| Selection sau sửa | **1.727/1.727 pass**: approval 299 (281 cũ + 18 mới, gồm Chrome/Next proxy), writers 220, reads-json 325, reads-originals 330, resources 553; collect 3.731, not-run 2.004; JUnit unique/không thiếu/trùng; 278 hash nguồn trước/sau trùng |
| Đối chứng guard | **12/12 fail đúng** (10 cũ chạy lại với anchor `respond` mới + `short_proof_teardown` bỏ cả hai lớp → 500, `short_teardown_invalidate` → rò connection). Lượt đầu mutant chỉ bỏ guard trong `_short` sống sót vì wrapper ở `attempt` vẫn bắt lỗi (phòng thủ hai lớp); giữ làm preparation |

Không chạy lại trong lượt sửa: auth/auth-restore (không đổi code auth/helper dùng chung) và web (không đổi web); số trước giữ theo source `b7ba608`. Evidence ngoài repo `%TEMP%/ras-c2d5b-p2-20261008`; PostgreSQL 58475/MinIO 59475/restore 58442 nhãn `c2d5b-p2-20261008` đã dọn đúng 3 container, 24 container có sẵn giữ nguyên. Safety pending tới C4.

## Phạm vi

Chỉ `POST /admin/review-requests/{request_id}/approve` chuyển sang `api/review_approval.py`. Admin hiện hành kiểm đúng request/version/file/checksum/epoch, tự ghi nguồn/bằng chứng/ngày kiểm/scope; hệ thống tạo **bản common mới `pending`** với original ở key riêng và `AccessRights` riêng. Không công bố, không job/index/chunk, không gọi provider. Bản riêng, version, file, collection của User không đổi; grant review kết thúc từ commit.

Không chuyển writer khác. `promotion.verified_read` giữ nguyên hợp đồng/export cho C2d-4c download và publication; `promotion.approve` legacy (cùng `_refuse`, `_cleanup_after_failed_commit`, replay ngay trong HTTP) đã bỏ.

## Mâu thuẫn quan sát trước sửa

| Điểm trong task | Hành vi quan sát ở `8ff5b4e` | Xử lý |
| --- | --- | --- |
| Principal/`now` dependency | Approve dùng `require_admin` + `get_now`; sau copy không kiểm latest caller/role/CSRF/TTL; `_refuse` tự audit/commit bằng principal cũ | Proof/acceptance/response đều đọc scalar session/User/receipt/generation/CSRF/role, clock sau SQL cuối; audit từ chối qua writer riêng (User → audit flush → journal exclusive → actor), lỗi audit bị nuốt |
| Mirror protected | `recovery.record_review_closure` INSERT thẳng `revocation_journal_records`; runtime không có quyền, routine `0040` chỉ nhận withdraw/reject | `0041`: hai routine approval riêng, `0040` giữ nguyên |
| Commit lỗi | Replay ngay bằng bind, rồi SELECT để suy không commit và xóa key | Outcome do operation sở hữu; commit exception luôn uncertain, giữ object, không replay/retry |
| Copy ownership | Ba lượt storage trong request thread, không slot/cancel; có Session dependency | Attempt sở hữu key/spool/slot/executor/outcome tới khi mọi thread settled |
| Proxy | JSON proxy 15 s cho approve | Timeout riêng đúng POST approve, mặc định 120 s |

Ngoài task: test HTTP legacy `test_review_approval.py` không import fixture `installation` nên lỗi setup ở base (not-run trong C2d-5a); nay import fixture runtime và chuyển các hook `promotion._audit` sang hook runtime cùng bất biến (xem Kiểm chứng).

## Boundary, khóa và proof

| Pha | Khóa/kiểm tra | Connection |
| --- | --- | --- |
| Proof trước copy | request SHARE → private item SHARE qua exact grant → journal shared → current Admin/CSRF/TTL → input (evidence/scope/ngày kiểm theo clock mới) → status/target scalar → caller lại sau SQL cuối | Session riêng trong thread, trả trước I/O |
| Đọc nguồn | Stream private original theo chunk vào temp, giới hạn size, SHA-256; hash lại chính spool | Không DB |
| Proof giữa pha | Như proof đầu, target phải bằng target đã ghim | Session ngắn, trả trước put |
| Put + readback | Put spool vào `originals/<uuid>` do attempt tạo; đọc lại size + SHA-256 | Không DB |
| Acceptance | Admin User NO KEY UPDATE → request `FOR UPDATE` → private item SHARE qua grant → target ghim + checksum xác nhận + ngày kiểm theo `now` → INSERT common item/version/file/rights, request `approved`, audit → flush → journal exclusive trực tiếp (pending đúng approval) → binding/actor → mirror → append/fsync → confirm → gate thường → binding/actor → commit | Session riêng trong thread; không savepoint |
| Response sau known commit | request SHARE → common item SHARE qua `review_copy_admin_clause` → journal shared → current Admin/CSRF → thin DTO → caller lại sau SQL | Transaction mới; không private item/grant/User lock |

Denial muộn (input/404/409) luôn kiểm current caller trước khi trả; mất phiên/role thắng lỗi tài nguyên. Malformed ID cũng qua caller/gate (không khóa tài nguyên). Từ chối `request_closed`/`grant_inactive`/`checksum_confirmation_mismatch` và lỗi copy (`source_unavailable`, `checksum_mismatch`, `copy_failed`, `copy_mismatch`, `copy_timeout`) được audit best-effort chỉ ID/mã lý do.

Response là **thin history**: `id/status/submitted_at/closed_at/close_reason_code/review_available=false/publication=false/pending_note` và `pending_publication_id` chỉ khi item chung còn qua gate common admin hiện hành (có thể đã published/revoked bởi writer khác). Không title/email/declaration/file/URL/evidence/rights.

## Copy, ownership và cấu hình

`CopySlots` riêng mỗi process (khác upload/download). Attempt: proof → giữ slot → spool → đọc nguồn → proof giữa → put/readback → acceptance → response; `finally` chỉ chạy sau khi executor settled (dùng `_thread` của C2d-4b: shield future, chờ thread thật kết thúc kể cả cancel-all). Xóa key chỉ khi: chưa acceptance, hoặc acceptance có `rollback_confirmed` và chưa `commit_started`. Không SELECT để quyết định xóa. Spool đóng trong thread, lỗi đóng không thay outcome; slot trả cuối cùng.

Deadline pha copy dùng `asyncio.timeout` (monotonic), clock auth là callable UTC riêng. Deadline/cancel chỉ đặt `stopped`; thread đang chặn trong SDK vẫn giữ slot/spool/key tới khi settle — SDK connect/read/retry dùng config storage hiện có. Stop biết trước acceptance hoặc trước commit thì không ghi tiếp; nếu append đã durable, recovery có thể đóng grant (`closed_by_recovery`).

| Config | Mặc định | Kiểm | Ghi chú |
| --- | --- | --- | --- |
| `RAS_FILES__APPROVAL_COPY_MAX_CONCURRENT` | 2 | 1–64 | Pool copy approval mỗi process |
| `RAS_FILES__APPROVAL_COPY_DEADLINE_SECONDS` | 90 | >0, ≤600 | Dừng quan sát pha copy, không giết thread |
| `WEB_PROXY_REVIEW_APPROVAL_TIMEOUT_SECONDS` | 120 | 1–300, ≥ `WEB_PROXY_TIMEOUT_SECONDS` | Chỉ đúng `POST admin/review-requests/<id>/approve` |

Các giá trị là **khởi đầu scoped theo ủy quyền, cần đo**; không phải SLO/benchmark hay luật kiến trúc. Route khác giữ timeout cũ.

## Migration `0041` và routine

- `ras_auth_review_approval_mirror(text)`: SECURITY DEFINER NOLOGIN (`c1_definer`), `search_path=pg_catalog,public,pg_temp`. Kiểm độc lập envelope/body đúng 7 trường, kiểu, UUID, `status=approved`, `reason_code=approved_by_admin`, thời gian ISO hữu hạn; request đúng tuple vừa chuyển open → approved trong **chính transaction** (`closure_transaction_id = txid_current()`), actor là Admin; private item/version/file/checksum/epoch/owner; `AccessRights` của đúng request với reviewer = closed_by, `recorded_at = closed_at`, epoch 0; common `pending`/owner NULL/`metadata_only`/epoch 0/key khác private/`file_source=review_approval`/job NULL/size+MIME+checksum khớp, đúng một version và một file. Item/version/file/rights phải do **chính transaction tạo**: so `xmin` với xid hiện tại (PostgreSQL mint, caller không truyền được GUC/boolean/UUID). Insert mirror false rồi `SET CONSTRAINTS ALL IMMEDIATE` trước byte đầu journal.
- `ras_auth_review_approval_confirm(text)`: chỉ false → true cùng line/request/transaction, request `approved`.
- Python `Approval.coupled` kiểm cùng ràng buộc bằng SQL riêng (tham số từ operation, không đọc lại line); pending gate (`review_writes._integrity`) chỉ trừ đúng approval chưa mirror này; closure/mirror khác, unconfirmed, hold, file ahead, cutover pending vẫn đóng.
- Provision mới và upgrade role đã provision: EXECUTE chỉ cho runtime, không PUBLIC/recovery; definer thêm SELECT `library_items/paper_versions/source_files/access_rights`. Không raw journal DML/DDL/TEMP/fallback.
- Downgrade populated bị chặn như C1; pristine roundtrip được.

Giới hạn binding: acceptance không dùng savepoint; row tạo trong subtransaction có `xmin` khác và bị từ chối (fail closed). `xmin` là binding live trong transaction, không phải receipt xuyên restore.

## Outcome và mã lỗi

| Cửa sổ | Kết quả | Object |
| --- | --- | --- |
| Input/denial trước copy | 401/403/404/409/422 sau caller hiện hành | Chưa put |
| Copy/integrity lỗi | `409 review_file_integrity_failed`, `503 approval_storage_unavailable`, `503 approval_copy_timeout`, `503 approval_busy` | Xóa key riêng sau khi thread settle |
| Known precommit rollback | 401/403/404/409 hoặc `503 approval_unavailable` | Xóa nếu rollback xác nhận; không xác nhận được thì giữ cho reconcile |
| Durable intent rồi actor/confirm/DB lỗi | 401/403 hoặc `503 review_closure_pending_recovery`; không DTO, không hứa còn mở | Như dòng trên; recovery có thể `closed_by_recovery`, không dựng common |
| Append/fsync lỗi, actor/binding còn hợp lệ | Commit approval + common/rights + mirror false; `503 review_journal_unavailable`; gate đóng tới `recover` | Giữ |
| Commit exception (landed hoặc rollback) | `503 approval_outcome_uncertain`, không DTO/ID/retry/replay | **Giữ** dù SELECT chưa thấy SourceFile |
| Known commit, proof/teardown lỗi | 401/403/`503 service_recovering`/`503 approval_response_unavailable` | Giữ |
| Known commit + proof mới đạt | 200 thin | Giữ |

Runtime không gọi `recovery.replay_record` và không dùng credentials recovery/migrator; CLI `recover` đối soát khi gate đóng.

## Web và proxy

`lib/proxy.ts` thêm `approvalTimeoutMs` cho đúng 4 segment `admin/review-requests/<id>/approve` + POST; giữ allowlist, Origin forwarding, pass-through status/Set-Cookie/no-store, không retry. Trang review Admin: form vẫn trống/không tick mỗi lần; mất phản hồi/504/502/đứt body hiển thị "Không xác định được kết quả phê duyệt… không tự gửi lại", bỏ tick xác nhận, không POST lại. Thêm thông báo cho `approval_busy/approval_copy_timeout/approval_response_unavailable`.

## Kiểm chứng

Source kiểm thử là working tree đúng bằng commit PR (hash 278 file nguồn trước/sau suite **trùng nhau**, ghi trong [inventory](c2d5b-test-inventory.json)). PostgreSQL/MinIO/journal cô lập, Conda `ras`, HTTP ngoài bị chặn, **0 provider**. Collect **3.713**; suite cuối **2.188/2.188 pass**, 0 fail/error/skip, JUnit unique/không thiếu/không trùng, **1.525 not-run**.

| Nhóm | Pass | Phạm vi |
| --- | ---: | --- |
| approval | 281 | 242 ca mới (contract/ACL, caller/clock, storage/attempt, SQL/pending gate, journal/outcome, race, restore, pending HTTP), 2 migration, 1 Chrome/Next proxy; legacy `test_review_approval` (MinIO, hook runtime), `test_review_requests`, `test_common_original`, migration C2d-5a và head/noop/downgrade/ORM |
| writers | 220 | C2d-5a submit/withdraw/reject + Chrome proxy (race với approval đã chuyển runtime) |
| reads-json | 325 | C2d-2b 9 GET + final read clock |
| reads-originals | 330 | C2d-4c download (approved ending qua runtime) |
| resources | 553 | C2d-2a/C2d-3 (fixture `common_copy` tạo bản chung qua approve runtime) |
| auth | 478 | Foundation, C2a, C2b, C2c2 invitation/migration |
| auth-restore | 1 | C2c2 pg_dump/pg_restore, container riêng nhãn bắt buộc |

Web **62/62** (`npm test`), typecheck và build đạt. Chrome 154 headless qua **Next start → socket Uvicorn**: form trống/không tick, approve 200 thin, URL review cũ 404; copy 16 s vẫn trả 200 qua budget approve; proxy 504 hiển thị "không xác định", bỏ tick, **không POST lại**, backend vẫn lưu; owner 403, phiên lỗi 401 xóa cookie/no-store.

**10/10 đối chứng guard** fail đúng assertion, 0 setup error, byte/hash khôi phục: role, clock trước journal, binding xmin SQL, binding xmin Python, coupling actor SQL, readback hash, pending gate, proof response mới, giữ object khi uncertain, ownership executor. Lượt đầu mutant proof response chỉ bỏ một trong hai kiểm tra nên sống sót (đúng hành vi), được giữ làm preparation và làm mạnh lại.

Preparation (không JUnit, không cộng pass) tìm ra **hai lỗi nguồn đã sửa**: Python binding thiếu kiểm "đúng một version/file" như SQL; ngoại lệ không phải `ApiError` ở pha response lọt thành 500. Còn lại là sửa test (cookie CSRF client phụ, locator Chrome, biến thể DB đã cấm, so ACL sau reprovision) và chuyển hook legacy. Danh sách đầy đủ trong inventory.

Chuyển test legacy: `common_copy`/`runtime_approve`, `original_fixtures` (bỏ approve đặc quyền), race C2d-2b/C2d-4c/C2d-5a và `test_review_approval` dùng approve runtime cùng bất biến; một kỳ vọng đổi có lý do: DB mất trước commit với rollback không xác nhận trả `approval_unavailable` (outcome biết chưa commit) thay vì `approval_outcome_uncertain`, object vẫn giữ cho reconcile. `test_publication`/`test_common_index`/`test_common_cleanup` (publish/revoke/index legacy, `test_publication` lỗi setup thiếu fixture từ base) giữ not-run.

Cleanup chỉ 3 container thử (DB 58475, MinIO 59475, restore 58442) theo exact ID/tên/nhãn/image/cổng/mount/sole consumer; anonymous volume và credentials generated đã xóa; **24 container có sẵn giữ nguyên ID/image/StartedAt**. Evidence (log/JUnit/node/script) ngoài repo `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5b-20261007`, đã quét không chứa credential. Không `.env`, dev DB/stack, provider, pricing hay readiness thay đổi.

## Contract cho task publication sau

[Task C2d-5c1](tasks/m6c2b2c2d5c1-common-publish.md) đã giao sau merge PR #47: chỉ publish. Revoke C2d-5c2 và index/provider riêng, safety pending tới C4. Lượt giao chỉ khảo sát nguồn và viết docs, không kiểm API/web/DB.

- Bản `pending` tạo ở đây có đúng một version/file, rights epoch 0, `file_source=review_approval`, key riêng; publish/revoke/index hiện vẫn legacy đặc quyền trong fixture và chưa runtime.
- Response approve có thể thấy bản đã published/revoked; không giả định `pending` vĩnh viễn.
- Object uncertain/known-commit không bị approval xóa; orphan chỉ `reconcile_originals` dọn sau grace.

## Giới hạn

- Scoped business cutover chỉ trong fixture; HTTP pending thật trả 503, không copy/DML/journal.
- Thread SDK bị treo vẫn giữ slot tới khi settle; không bảo đảm wall-clock tuyệt đối hay abort multipart an toàn. Registry/reaper attempt quiescent là follow-up riêng.
- Fault injection Python/driver, `fsync` và snapshot cùng máy không chứng minh mất điện, storage journal production độc lập hay cutover.
- Publish/revoke/common-index/cleanup vận hành, search/RAG/provider, C3 operator/delivery, C4 cutover giữ riêng. Safety pending tới C4.
- Các lựa chọn khóa/routine/xmin/config là quyết định scoped theo ủy quyền của orchestrator cho PR này, không phải quyết định trực tiếp mới của chủ dự án hay luật kiến trúc vĩnh viễn.
