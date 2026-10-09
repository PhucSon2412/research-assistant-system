# Review PR #47 — C2d-5b approval/copy

**Sau review:** PR #47 đã merge `04434e3` ngày 2026-10-08, chứa `50393d1`/`78fbe1d`; merge tree giống head PR. Chưa chạy lại suite tại merge. Kết luận/evidence dưới đây giữ source và thời điểm review, safety pending tới C4; [C2d-5c1](../tasks/m6c2b2c2d5c1-common-publish.md) được giao tiếp theo.

## Review lại P2 tại 78fbe1d

Ngày **2026-10-08**. GitHub CLI xác nhận PR **OPEN, chưa merge**; code sửa `50393d1`, head `78fbe1dfa6251538f52ed8d142f8372384eea865`. Working tree sạch trước kiểm thử. Reviewer chỉ bổ sung tài liệu sau kiểm chứng, không sửa ứng dụng/migration, commit/push/merge hoặc đăng comment PR.

**Kết luận: P2 đã được xử lý; không phát hiện điểm chặn merge mới trong phạm vi C2d-5b.** Safety vẫn **pending tới C4**; kết luận này chỉ áp cho POST approve/copy, không chứng nhận publication/index/provider, C3/C4 hoặc toàn bộ ứng dụng.

### Đối chiếu bản sửa

`_short` sở hữu Session của proof đầu, proof giữa pha, caller-denial khi ID sai, response và audit từ chối. Helper thử cả rollback và close; nếu một thao tác lỗi thì invalidate connection và dừng bằng lỗi có kiểm soát. Proof chỉ được dùng sau release sạch. `attempt` bảo vệ cả nhánh ID sai và proof đầu; lỗi bất ngờ thành JSON `503 approval_unavailable`. Các ApiError auth/nghiệp vụ vẫn giữ mã gốc.

Sau known commit, lỗi release response trả `503 approval_response_unavailable`, giữ hàng DB và object; không trả success hoặc tự retry. Acceptance outcome, `0041`, grants, SQL binding và thứ tự khóa không đổi. Reviewer đối chiếu diff và source hash, không chỉ dựa vào mô tả của agent.

### Kiểm chứng mới của reviewer

| Thực chạy trên source sau sửa | Kết quả |
| --- | --- |
| Selection proof/teardown/cancel/outcome/clock/denial/response/pending HTTP | **60/60 pass**, 0 fail/error/skip; 225,39 s, 1 warning |
| Sáu ca tái hiện độc lập từ lần review trước, giữ nguyên script ngoài repo | **6/6 pass**, 0 fail/error/skip; 18,46 s, 1 warning |

Tổng **66 unique**. Selection 60 chứa đủ 18 regression mới, cả lỗi rollback/close trước khi release và fallback invalidate, cùng các ca outcome/cancellation/clock liên quan. Sáu ca ngoài repo gồm bốn lỗi từng trả 500 và hai control 200/404; giờ bốn lỗi trả JSON 503 đúng hợp đồng. Kiểm no copy/mutation/journal/slot sau proof đầu lỗi và giữ object/rows sau known commit đều đạt.

**279 source hash** API Python/SQL và web trước/sau hai lượt khớp nhau và source tại head review. Không sửa code khi suite chạy. Reviewer chạy 60 node trong inventory API 3.731, **3.671 node chưa chạy lại**; sáu ca ngoài repo không nằm trong inventory đó. Không chạy lại web/typecheck/build, Chrome, auth/auth-restore, toàn bộ suite hoặc mutants trong lượt này. Kết quả reviewer 281 API/62 web/Chrome trước sửa bên dưới giữ nguyên theo source `b7ba608`, không tính thành pass mới.

### Artifact agent đối chiếu riêng

Đã kiểm selection/JUnit của năm group sau sửa: approval 299, writers 220, reads-json 325, reads-originals 330, resources 553. Tổng **1.727 unique pass**, không thiếu/trùng/fail/error/skip; collect **3.731**, **2.004 not-run**. 278 source hash trước/sau khớp current; sau code `50393d1` chỉ đổi docs. Web source không đổi so với lần kiểm lịch sử.

Đối chiếu focused 22 pass và XML trước sửa có đúng 10 failure; 12 XML mutant mạnh đều fail assertion đúng mục tiêu, không setup error, restored hashes khớp. XML mutant yếu bỏ một lớp bắt lỗi vẫn pass được giữ riêng, không tính là phát hiện lỗi. Đây là **đối chiếu evidence của agent**, không phải reviewer chạy lại 1.727 ca, Chrome hoặc 12 mutant. Không cộng số 2.188 của lần triển khai trước.

### Evidence, cleanup và giới hạn

Evidence reviewer ở `%TEMP%\ras-pr47-p2-review-20261008`: `run-review.py`, commands/logs, hai JUnit, source hashes, `verify-review.py`, `review-results.json`, `verify-agent.py`, `agent-artifact-check.json`, cleanup và quality JSON. Evidence agent ở `%TEMP%\ras-c2d5b-p2-20261008`, liên kết trong [inventory `p2_fix`](../c2d5b-test-inventory.json). Hai lượt chuẩn bị checker của reviewer cần sửa cách đọc key inventory/đếm nhóm regression; không sửa ứng dụng và không tính thành failure/pass API.

Conda `C:\Users\ADMIN\.conda\envs\ras`, PostgreSQL/MinIO/journal cô lập, HTTP ngoài bị chặn, **0 provider call**. Đã kiểm ID/name/label/image/port/mount/sole-consumer trước khi dọn **2 container/2 volume của reviewer** và ba file credentials; **24 container có sẵn giữ nguyên ID/image/StartedAt**. Bốn credential sinh cho test không xuất hiện trong evidence. Không đọc/sửa `.env`, tác động stack/DB dev hoặc đổi pricing/gate.

Fault injection không chứng minh lỗi thiết bị/mất điện hoặc journal production bền vững. Không có benchmark, provider live hoặc full cutover. PR vẫn mở để chủ dự án quyết định merge; reviewer chưa thực hiện thao tác ra ngoài.

## Review ban đầu trước sửa (lịch sử)

Phần dưới ghi nguyên phát hiện và kết quả tại `bac3739`/`b7ba608`. P2 này đã đóng theo review lại phía trên; prompt sửa cuối tài liệu được giữ làm lịch sử.

Ngày 2026-10-07. PR **OPEN, chưa merge** tại lúc review. Base `8ff5b4e`; code `b7ba6082dfd49316cea065fd6d03762810bc2244`; head `bac3739420f838c75064a7c9175126dc6bf5963f`. Đúng một POST approve; **safety pending tới C4**. Reviewer chỉ bổ sung docs, không sửa ứng dụng/migration, commit/push/merge hoặc đăng comment PR.

## Kết luận: cần sửa một P2 trước merge

### P2 — lỗi teardown của proof đầu thoát thành HTTP 500

Trong [review_approval.py](../../api/app/api/review_approval.py), `attempt()` gọi proof đầu ở dòng 607 **trước** khối `try` chuyển ngoại lệ thành lỗi API (dòng 612). Nhánh ID sai cũng gọi `caller_denial()` trước khối đó. Hai helper có `except Exception` bên trong `with Session(...)`, nhưng `db.rollback()` trong `finally` (dòng 257/297) và `Session.__exit__ → close()` nằm ngoài phạm vi bắt lỗi. Lỗi teardown vì thế thoát qua `_thread`/`receive` thành HTTP 500 với body `Internal Server Error`, thay vì JSON 503 có kiểm soát.

Reviewer tái hiện bằng tài khoản/session/receipt/grant và runtime PostgreSQL thật, chỉ fault-inject `rollback` hoặc `close` của Session riêng trong module này. Gọi HTTP bằng `TestClient(..., raise_server_exceptions=False)`:

| Ca | Thực tế | Kỳ vọng |
| --- | --- | --- |
| ID hợp lệ, rollback proof lỗi | 500, text `Internal Server Error` | 503, JSON `approval_unavailable` |
| ID sai, rollback caller-denial lỗi | 500, text `Internal Server Error` | 503, JSON `approval_unavailable` |
| ID hợp lệ, close proof lỗi | 500, text `Internal Server Error` | 503, JSON `approval_unavailable` |
| ID sai, close caller-denial lỗi | 500, text `Internal Server Error` | 503, JSON `approval_unavailable` |
| ID hợp lệ, không gây lỗi | 200 | 200 |
| ID sai, không gây lỗi | 404 | 404 |

**4 failure đúng lỗi / 2 control pass**. Không phải lỗi setup: mỗi ca fault đi hết assertion request vẫn open, chưa common/rights/copy mới, journal không đổi, gate integrity mở, slot bằng 0, rồi mới fail ở status/body. Fault dùng rollback/close thật trước khi ném ngoại lệ; chưa mô phỏng mất mạng thật hoặc chứng minh mọi đường release khi cả hai thao tác đều lỗi. Chưa thấy mất dữ liệu hoặc lộ nội dung ở các ca này, nhưng vi phạm hợp đồng lỗi DB có kiểm soát trong [task](../tasks/m6c2b2c2d5b-review-approval-original-copy.md#6-outcome-object-cleanup-và-response-lỗi).

**Hướng sửa:** bảo vệ toàn bộ phase đầu, gồm malformed-ID và proof/teardown; khi proof không hoàn tất sạch phải dừng trước storage/mutation và trả ApiError có kiểm soát. Giải phóng/invalidate connection phù hợp khi rollback/close lỗi; không nuốt lỗi rồi tiếp tục copy, không làm mất `CancelledError` hoặc đổi ownership của thread. Giữ nguyên actor/CSRF/clock/grant, lock order, outcome acceptance độc lập, retention khi commit unknown/known và proof response mới. Không cần đổi `0041` hay quyền DB chỉ để sửa điểm này.

### Dọn docs đã làm trong lượt review

Hai README còn `{N_API}`/`{N_WEB}`; reviewer thay bằng **2.188/2.188 API và 62/62 web của agent**, sau khi đối chiếu artifact. Đây không phải số test reviewer chạy hoặc bằng chứng pass toàn inventory. Thêm link report/P2, giữ trạng thái PR chưa merge và safety pending.

## Kiểm độc lập

| Reviewer thực chạy | Kết quả |
| --- | --- |
| Nhóm approval mới, migration, Chrome proxy, legacy approval/review requests/common originals, head/noop/pristine roundtrip/ORM | **281/281 pass**, 0 fail/error/skip; 904,90 s, 6 warning trong log |
| Regression ngoài repo cho proof đầu | **4 fail đúng P2 / 2 control pass**, 0 setup error/skip |
| Web test | **62/62 pass**, 0 fail/skip |
| Web typecheck/build | Đạt / đạt |

Selection chính trùng 281 node nhóm `approval` trong inventory agent. Bao gồm quyền/binding/NULL/UUID/type/time, routine/runtime privilege, deferred constraint trước append, idle/absolute sau SQL/lock wait/fsync, hash/source/copy/spool, 30 MiB synthetic streaming, slot/cancel/deadline/thread ownership, commit rollback/landed-uncertain/teardown, race hai thứ tự, snapshot replay open/absent/approved và HTTP pending thật không patch. Chỉ fixture business dùng cutover scoped; auth/bootstrap/invitation/login/predicate/runtime thật. Publish/revoke legacy chỉ làm setup/đối thủ trong test, không chứng nhận các writer đó đã chuyển runtime.

Chrome cài sẵn được chạy lại qua Uvicorn socket và Next proxy: form trống/unticked, approve 200 thin, copy 16 s qua timeout riêng, proxy 504 hiển thị unknown outcome và không tự POST lại. Các phép đo fixture/copy không là benchmark hoặc SLO.

**279 hash reviewer** của API Python/SQL và web trước/sau mỗi lượt không đổi: phủ đủ 278 file trong inventory agent và thêm helper `api/m4d3_same_seed.py`. Không sửa source trong suite; 281 là tập con của 3.713 node, **3.432 node inventory API không chạy lại trong lượt reviewer**. Sáu regression ngoài repo không nằm trong collect API đó. Không chạy lại 2.188 node như agent hoặc các suite publication/common-index/common-cleanup/provider/C3/C4.

## Artifact agent đối chiếu riêng

Đã đọc JUnit và node selection của bảy group: approval 281, writers 220, reads-json 325, reads-originals 330, resources 553, auth 478, auth-restore 1. Tổng **2.188 unique**, không thiếu/trùng/failure/error/skip; **1.525 not-run** trong collect 3.713. Hai alias `uuid1()` được kiểm đúng test/domain/version, không gộp test khác. **278 hash** trong inventory trước/sau khớp source hiện tại; sau code `b7ba608` chỉ đổi docs.

Đối chiếu 10 XML mutant: mỗi bản mạnh có một assertion failure, không setup error; restored source hash khớp. Bản response-proof yếu ban đầu vẫn giữ XML pass riêng, không được tính là mutant bắt lỗi. Reviewer **không chạy lại 10 mutation**; bốn regression P2/control là bằng chứng mới độc lập. Không cộng số lịch sử PR #46.

## Evidence và tài nguyên

Reviewer: `%TEMP%\ras-pr47-review-20261007` chứa command, log, JUnit/source hashes, regression script, agent-artifact-check, review-results và cleanup JSON; không commit artifact/credentials/paper/journal. API/XML SHA-256 ghi trong `review-results.json`, source trong `review-source.json` và `first-proof-source.json`. Agent: `%TEMP%\ras-c2d5b-20261007`, [inventory](../c2d5b-test-inventory.json), [handoff](../c2d5b-review-approval-original-copy.md).

Chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`, PostgreSQL/MinIO mới, HTTP ngoài bị chặn và **0 provider call**. Đã kiểm exact ID/name/label/image/port/mount/sole consumer rồi dọn **2 container/2 volume của reviewer**, xóa ba file credentials sinh cho test. **24 container có sẵn giữ nguyên ID/image/StartedAt**. Không đọc/sửa `.env`, tác động DB/stack dev hoặc đổi pricing/readiness production. Evidence không có bốn credential sinh cho reviewer. Fault injection/snapshot cùng máy không chứng minh crash/mất điện, journal production độc lập hoặc cutover C4.

## Prompt sửa cho coding agent

Sửa P2 trên PR #47, giữ PR mở. Đọc report này và task C2d-5b; giữ docs review chưa commit. Chuyển lỗi phase đầu/proof/Session teardown thành lỗi API có kiểm soát và trả connection trước I/O; không tiếp tục copy nếu proof release lỗi. Thêm regression cho normal/malformed ID × rollback/close, cả lỗi trước khi release nếu cần kiểm fallback invalidate; kiểm no storage/DML/journal/slot/connection leak và giữ 200/404/401/403 khi không fault. Giữ cancellation/executor ownership và outcome commit/retention của acceptance.

Không đổi migration/grants/SQL binding nếu không có lý do mới; không mở safety pending hoặc chuyển publication/provider/C3/C4. Chạy regression mới cùng nhóm proof/cancel/outcome/clock/SQL và selection approval 281 (gồm Chrome) trên tài nguyên cô lập; chọn thêm hồi quy nếu sửa helper dùng chung. Đối chiếu collect/selection/JUnit/hash, ghi failed/control/not-run riêng và cập nhật handoff/inventory/docs. Fault reproducer độc lập ở thư mục evidence reviewer; không ghi kết quả cũ thành pass mới. Commit/push bản sửa vào PR #47 với mô tả tiếng Việt, không AI trailer; không merge.
