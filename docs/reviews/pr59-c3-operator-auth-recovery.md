# Review PR #59 — operator auth recovery

## Review lại tại `035cef0` — không còn điểm chặn merge trong scope C3

Ngày **2026-10-12**, head `035cef02e38d87bd5e44e0bf5e887ed0ed574e09`, PR **OPEN, chưa merge**, source **0049**. Code sửa tại `5eda25e`, suite agent tại `d2d56f3`; head thêm docs. **P2-1 và P2-2, gồm nhánh Ctrl-C trong release, đã qua review lại; không phát hiện điểm chặn merge mới trong phạm vi C3.** Có thể merge PR #59 theo phạm vi này. Safety vẫn pending tới C4; kết luận không thay full inventory, xử lý debt hoặc nghiệm thu cutover.

### Cơ chế và kiểm độc lập

- `_release` hấp thụ `BaseException` của rollback/close và fallback invalidate/close, trả `clean`/`failed`/`interrupted`; lỗi của thân pha vẫn thắng. Mutation phân loại theo `Attempt`: known commit giữ credential/status/case/receipt, batch unconfirmed giữ exit 5, commit chưa rõ vẫn uncertain, append đã bắt đầu không báo nothing-written. Ngắt rollback nội bộ không còn lọt ra CLI như hủy TTY. Proof sau commit bị ngắt vẫn non-success.
- **106/106 repo pass**, 0 failure/error/skip, **344,57 s**: C3 core 93, CLI 10, migration 2, ORM/schema 1. Bao gồm ba lượt regression, ConPTY, pending HTTP không patch qua API/proxy và hai vòng dump/restore. Gate 503 không thay nghiệp vụ C4.
- **4/4 ca reviewer ngoài repo pass**, 0 failure/error/skip, 11,99 s: ca Ctrl-C từng fail tại `6465dc0` và ba ca one-shot `Connection.close` lỗi ở prepare/mutation/proof. Ca Ctrl-C nay có receipt mới và verifier mới usable nhưng CLI exit 4, không in `nothing was changed`. Prepare release lỗi dừng trước prompt/ghi; known commit giữ receipt, không báo success sau release lỗi. Các ca này dùng CLI in-process/fault injection, không là SIGINT/terminal thật; ConPTY riêng phủ terminal thật lúc nhập mật khẩu.
- **408 tracked source hash trước/sau trùng.** Không đổi app, repo tests, migration/grants trong review. Lượt ghép test repo với test ngoài repo ban đầu dừng ở collect vì plugin `tests.conftest` đăng ký trùng; không có test sản phẩm chạy, không cộng vào pass. Giữ log/XML riêng, tách hai nhóm và chạy lại như trên.

### Đối chiếu agent và giới hạn

Đọc đúng `rereview_fix_6465dc0.final` và artifact `%TEMP%\ras-c3fix2-20261012\final-1`: **106 unique pass**, selection/JUnit khớp chính xác; 5.118 collected, **5.012 not-run**, **358 source hash khớp head**. Ghi SHA-256 cho **28 artifact** final/countercheck; chín XML đối chứng cuối đều một assertion failure, không error/skip. Reviewer kiểm artifact, không chạy lại chín mutants; các lượt đối chứng yếu và lỗi chuẩn bị của agent giữ theo inventory.

Selection rộng **1.009** ở `0ee17f3` là kết quả lịch sử, **không** phải lượt chạy mới tại `d2d56f3`/`035cef0`; không dùng số top-level `final` trong inventory để gộp thành pass hiện hành. Không chạy lại full inventory, 281 debt/base, web suite riêng, provider/charge/routing/retention, C4 hoặc độ bền mất điện. Generic receipt `bootstrap`/`invitation_accept`, lịch sử bị mất, nơi lưu journal độc lập và inventory operator/người thật vẫn cần nghiệm thu gói B/C4, không được miễn bởi review C3.

### Evidence và cleanup lượt cuối

Evidence ngoài repo: `%TEMP%\ras-pr59-rereview2-20261012` gồm logs/JUnit, ca reviewer, hashes, đối chiếu artifact, baseline và cleanup. Quét 13 artifact log/XML/JSON trước cleanup: 0 hit credentials thử, mật khẩu fixture và mẫu provider key/PHC/URL DB chứa password; không là quét toàn máy.

Đã kiểm labels rồi gỡ đúng **3 container** cùng volume/network của project `rasc3b2bf7005`, xóa credentials/compose/empty-env. Chín container có sẵn giữ nguyên ID/image/mounts/labels, volume/image khớp baseline. Worker/beat đang restart chỉ đổi `StartedAt`/`RestartCount`, không suy nguyên nhân. Không đọc/sửa `.env`, query/migrate/restart dev, gọi provider/arXiv, đổi pricing hoặc mở safety. Docs reviewer giữ chưa commit cho PR kế tiếp; root không commit/push/comment GitHub/merge. Các report trước dưới đây là lịch sử, không là điểm chặn hiện hành.

Kiểm docs: **99 Markdown, 1.953 liên kết nội bộ**, 0 lỗi liên kết/control character; `git diff --check` sạch. Năm file docs reviewer modified được giữ cho agent đưa vào PR kế tiếp.

## Review lại tại `6465dc0` — còn nhánh P2-2 khi Ctrl-C trong release

Ngày **2026-10-12**, head `6465dc0789ffedf414b84611215571b721dfa11e`, PR **OPEN, chưa merge**, source **0049**. Production sửa ở `54a2d02`; suite agent tại `0ee17f3`, head chỉ thêm docs. Hai cơ chế đã báo ở lượt đầu được sửa đúng cho lỗi `Exception`: P2-1 đã qua kiểm lại, P2-2 rollback/close lỗi thông thường đã qua kiểm lại. **Còn một nhánh P2-2 cần sửa trước merge:** interruption lúc release sau known commit không được phân loại theo outcome.

### P2-2 còn lại — Ctrl-C sau commit bị báo “nothing was changed”

`app/operator_recovery.py:117–133` chỉ bắt `Exception` trong `_release`; `KeyboardInterrupt` thoát helper trước khi hoàn tất cleanup. `mutate:367` chỉ phân loại `ConnectionReleaseFailed`, nên interruption này thoát ra CLI. `app/cli/auth_recovery.py:145–147` nhận nó như hủy nhập mật khẩu, trả **exit 2** với `error: input cancelled; nothing was changed`.

Reviewer tái hiện với PostgreSQL/journal/login operator thật, gọi CLI in-process (TTY/getpass tổng hợp, **không** tính là kiểm ConPTY cho ca này). Thay `Connection.rollback` bằng fault một lần **chỉ khi release mutation** để ném `KeyboardInterrupt`. Kết quả sau CLI:

- DB tăng đúng một receipt; credential `usable`, verifier khớp mật khẩu mới.
- CLI exit 2 và khẳng định chưa thay đổi gì; kỳ vọng outcome không thành công **sau known commit** (exit 4), giữ quyết định/case, không submit lại.
- Ca fail đúng assertion nghiệp vụ ở lượt đầu và lượt xác nhận có kiểm thêm verifier; không setup error/skip. Đây là fault injection trong Python, không phải SIGINT/ConPTY hay lỗi thiết bị thật.

Không chỉ đổi câu chữ: interruption trong release phải được cleanup và phân loại bằng `Attempt` độc lập, gồm known commit/unconfirmed/uncertain. Ctrl-C tại TTY **trước mutation** vẫn được hủy an toàn theo hợp đồng hiện có. Không tuyên bố connection leak từ ca tái hiện này. [Prompt sửa hẹp cùng PR](../tasks/fix-pr59-c3-final-authority-and-release.md#lượt-review-lại-tại-6465dc0--interruption-trong-release).

### Kiểm độc lập và đối chiếu agent ở lượt này

- **95/95 repo pass**, 0 failure/error/skip, 336,02 s: C3 core 82, CLI 10, migration 2, ORM/schema 1. Bao gồm 12 regression agent mới, ConPTY, pending HTTP qua API/proxy và hai vòng dump/restore.
- **11/11 ca ngoài repo pass:** 5 ca từng tái hiện ở lượt đầu, 3 ca kiểm trực tiếp `os.fsync`, 3 ca one-shot `Connection.close` lỗi tại prepare/mutation/proof. Lỗi close ở prepare dừng trước prompt và không thay DB/journal; lỗi close sau commit giữ receipt, không báo success; các ca kiểm pool trả hết connection.
- **1 ca ngoài repo fail** là interruption nêu trên; chạy xác nhận lại vẫn fail, không đếm lượt lặp thành ca mới.
- Artifact agent: JUnit **1.009 unique pass**, 0 fail/error/skip; selection khớp chính xác JUnit, nằm trong 5.107 collected, 4.098 not-run. **358 source hash khớp head**, nội dung chuẩn hóa LF/CRLF khớp seal `0ee17f3`. **26 artifact hash** lưu riêng; tám XML countercheck mỗi file có một assertion failure, không error/skip. `before-fix.xml` đúng 9 fail/3 pass. Reviewer không chạy lại full 1.009, 281 debt/base hoặc tám mutants.
- **408 tracked source hash trước/sau trùng.** Không đổi app/test repo, migration/grants, `.env`, pricing hay safety; không query/migrate/restart dev, không gọi provider/arXiv. Evidence mới: `%TEMP%\ras-pr59-rereview-20261012`; các chứng cứ lượt đầu bên dưới giữ nguyên lịch sử.
- Cleanup đã kiểm label rồi gỡ đúng **3 container** và volume/network thử, xóa credentials/compose/empty-env của context. Chín container có sẵn giữ nguyên ID/image/mounts/labels; volume/image khớp baseline. Worker/beat vẫn restart, chỉ `StartedAt`/`RestartCount` đổi; không suy nguyên nhân. Quét 18 artifact log/XML/JSON: 0 hit mật khẩu fixture, mẫu provider key/PHC/URL DB chứa password (không là quét mọi secret trên máy). Kiểm **99 Markdown, 1.944 liên kết nội bộ**, 0 lỗi; `git diff --check` sạch. Năm docs reviewer modified giữ chưa commit cho cùng PR, không comment/push/merge.

## Lượt review đầu tại `8fa504f` — lịch sử trước sửa

Ngày **2026-10-12**, PR **OPEN, chưa merge**. Head `8fa504f49504d4a1c3e36830f02cfca4c730c33c`, base merge PR #58 `58d059ab7a383d5f97e1b04e909e9628a11bf78a`, source **0049**. [Handoff](../c3-operator-auth-recovery.md) · [Inventory agent](../c3-test-inventory.json) · [Task gốc](../tasks/m6c2b2c3-operator-auth-recovery.md) · [Prompt sửa cùng PR](../tasks/fix-pr59-c3-final-authority-and-release.md).

## Kết luận

**Có hai P2 chặn merge. Chưa merge PR #59; sửa trên cùng PR rồi review lại.** Safety vẫn pending tới C4. Các ca C3 hiện có đạt không bao phủ hai nhánh dưới đây; không dùng kết quả đó để chứng nhận full recovery/cutover hoặc readiness.

### P2-1 — Append lỗi bỏ qua final authority/expiry, recover giữ quyết định đã mất quyền

`api/app/operator_recovery.py:379–385`: `_recheck` chỉ chạy khi `attempt.written=True`. File ghi đủ nhưng `os.fsync` ném `OSError` làm `written=False`, rồi code vẫn COMMIT mutation/receipt và cả batch false. Case hết hạn hoặc operator bị revoke trong lúc fsync không được kiểm lại.

Reviewer tái hiện trên PostgreSQL/journal/operator login thật, thay **chính `journal.os.fsync`** để chạy tới đúng cửa sổ rồi ném lỗi:

| Tình huống | Quan sát tại head |
| --- | --- |
| Credential case hết hạn trong fsync | `auth_journal_unavailable`, `committed=True`, thêm 1 receipt; sau recover credential usable với mật khẩu mới |
| Initial status case hết hạn trong fsync | `committed=True`, thêm 1 receipt; sau recover status proven, không còn held |
| Operator bị revoke trong fsync | `committed=True`, thêm 1 credential receipt; sau recover credential usable |

Đây không chỉ là thông báo không thành công: receipt đã commit khiến recover giữ quyết định thay vì chỉ replay hạn chế. Kỳ vọng phải kiểm authority/clock cả khi append lỗi, rollback mutation/receipt nếu đã mất quyền/hạn; journal có thể giữ intent/case, recover không dựng credential/active/receipt. Giữ nhánh commit batch false khi append lỗi **và authority/case vẫn hợp lệ**. Fault là mô phỏng tại hàm fsync, không phải lỗi thiết bị/mất điện thật.

### P2-2 — Release lỗi bị nuốt, prepare/proof vẫn dẫn tới success

`api/app/operator_recovery.py:110–123`: helper `connection()` bắt lỗi rollback/close, invalidate rồi close nhưng không báo lại lỗi nếu cleanup cuối thành công. Reviewer làm rollback ném lỗi một lần ở transaction prepare, rồi riêng ở proof sau known commit; **cả hai vẫn trả `OperatorResult` thành công**.

Hai ca này không rò connection (`checkedout=0`); lỗi là workflow tiếp tục/báo success sau release không sạch, trái hợp đồng task. Prepare lỗi phải dừng trước nhập/hash/mutation; proof lỗi sau known commit phải trả outcome lỗi, giữ dữ liệu đã commit và không tự retry. Sửa helper cùng phân loại `Attempt` độc lập để lỗi cleanup không làm mất known/uncertain commit hoặc che lỗi có ý nghĩa. Regression cần mở thêm rollback/close/invalidate ở các pha trong prompt sửa.

## Kiểm độc lập đã chạy

Conda ras Python 3.12.14/pip 26.2.1; project `rasc3b0e09d00`, PostgreSQL/MinIO/Redis/journal và credentials tổng hợp cô lập. Network HTTP ngoài loopback bị chặn bởi runner; không đọc/sửa `.env`, query/migrate/restart dev, gọi arXiv/OpenRouter, đổi pricing hoặc mở safety. Không sửa app, migration, grants hay repo tests.

- **83/83 repo pass**, 0 failure/error/skip, 302,795 s: toàn C3 core (70), CLI (10), migration (2), ORM/schema (1). Có ConPTY thật, pending HTTP không patch qua API/proxy ở head và hai vòng pg_dump/pg_restore. Đây là thời gian suite, không là latency sản phẩm.
- **5 ca ngoài repo fail đúng assertion nghiệp vụ:** 3 authority/expiry + 2 release. Lượt đầu chạy `-x` dừng ở ca credential expiry; lượt đủ 5 được giữ riêng, không đếm lượt lặp thành ca độc lập.
- Kiểm lại 3 ca authority/expiry bằng **actual `os.fsync` injection**, cả 3 fail: `committed=True`, `new_receipts=1`, `restricted_after_recover=False`. Không lỗi setup/error/skip trong các lượt tái hiện.
- **408 tracked source hash** (`api/app`, migrations, tests, scripts, web) trước/sau review trùng. Chỉ docs review/task/header thay đổi trong workspace.
- Chưa chạy lại full inventory, 279 baseline debt, provider thật/charge/routing/retention, C4 hoặc độ bền mất điện. Không chạy web suite riêng vì không sửa web; pending proxy nằm trong nhóm C3 đã chạy. HTTP nghiệp vụ có patch riêng cutover trong fixture theo hợp đồng C1–C3 không là chứng nhận C4.

## Đối chiếu chứng cứ agent

Đọc artifact `final-3`: JUnit **997 unique pass**, 0 fail/error/skip; all collection 5.095, selection 997, not-run 4.098. Đối chiếu manifest **358 source hash**: 357 khớp head; khác duy nhất `scripts/c3_pending_smoke.py`, sửa sau seal `cb9518b` ở commit `8a622a8`. Nội dung source seal/head sau chuẩn hóa LF/CRLF khác đúng file đó; không nhầm Git blob LF với raw checkout hash CRLF. Pending smoke ở head đã được reviewer chạy lại trong nhóm 83.

Đã lập SHA-256 cho **22 artifact** final/countercheck; sáu XML countercheck cuối mỗi file có 1 assertion failure, 0 setup error/skip. Đây là kiểm artifact, reviewer không chạy lại sáu mutant hoặc toàn suite 997. Các lượt countercheck yếu/runner WinError 206 của agent giữ theo inventory, không suy thành pass mới.

## Debt và giới hạn không mở thêm scope review

- Generic receipt status `bootstrap`/`invitation_accept` là lỗ ràng buộc đã có trong routine trước C3, không phải hai lỗi Python mới ở trên. Chuyển thành mục bắt buộc gói B/C4; không miễn nghiệm thu, không dùng generic initial reason để chứng minh đường operator đã đóng toàn bộ recovery.
- Inventory/ủy quyền người thật chưa được cung cấp; case/operator tổng hợp không thay bằng chứng danh tính ngoài app. Windows case ACL chưa kiểm; PHC của target case được trả vào bộ nhớ operator CLI để so mật khẩu cũ là trust boundary đã công bố.
- Các baseline/debt và test restore gắn cứng môi trường cũ vẫn cần xử lý tại gói B/C4. Không chứng nhận full writer/auth recovery bằng 83 hoặc 997 pass.

## Evidence và cleanup

Evidence ngoài repo: `%TEMP%\ras-pr59-review-20261012` gồm source hashes, Docker baseline/after, logs/JUnit scope và regression, `test_extra.py`, `test_actual_fsync.py`, kết quả đối chiếu artifact agent và cleanup. Quét 16 log/XML/JSON review: 0 hit credentials thử, provider key hoặc PHC.

Đã kiểm project/container labels rồi gỡ đúng **3 container** và volume/network của project thử, xóa `credentials.json`, `compose.json`, `empty.env`. Chín container có sẵn giữ nguyên ID/image/mounts/labels; danh sách volume/image khớp baseline. Chỉ `StartedAt`/`RestartCount` của worker/beat đang restart có thay đổi, không suy nguyên nhân. Không gọi stop/up/down project dev.

Kiểm tài liệu: **99 Markdown, 1.927 liên kết nội bộ**, 0 lỗi liên kết/control character; `git diff --check` sạch. Docs review/task được giữ **chưa commit** cho agent sửa PR; root không commit/push/comment GitHub/merge.
