# Review PR #59 — operator auth recovery

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
