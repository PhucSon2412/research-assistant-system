# Sửa PR #62 — deadlock cleanup và xóa lặp

**Trạng thái 2026-10-18:** P2 đã sửa ở code `a5eb8bb`, review lại head `15f1dfa` không phát hiện điểm chặn merge mới trong scope M6d; [report cập nhật](../reviews/pr62-m6d-private-item-delete-restore.md). PR vẫn OPEN/chưa merge. Nội dung dưới giữ yêu cầu và lỗi tại source trước sửa, không giao thêm một lượt coding.

Sửa **trên cùng PR #62**, không mở task/PR khác và không mở rộng M6d. Đọc [review](../reviews/pr62-m6d-private-item-delete-restore.md), [handoff](../m6d-private-item-delete-restore.md) và task M6d gốc. Head được review `7494f8c`, source `0052`; source application `a7b57b0` và test `3f0695b` theo inventory. Giữ toàn bộ docs reviewer/orchestrator modified/untracked, đưa vào PR.

## Lỗi đã xác nhận

`private_delete.accept()` khóa review requests (kể cả đã đóng) → item, rồi mới kiểm đã deleted. `private_cleanup.finalize()` khóa tombstone → item → scrub review requests. Với item đã deleted có review history, repeat POST và cleanup tạo cycle review ↔ item.

Reviewer giữ cleanup ngay trước scrub khi đã giữ item, cho repeat POST tới lúc PostgreSQL báo đang chờ transactionid, rồi thả cleanup. SQLSTATE **40P01 DeadlockDetected** xuất hiện ở cả boundary trực tiếp và HTTP thật: cleanup purged, repeat nhận **503 deletion_unavailable** thay vì receipt idempotent. Control không review pass. Không đổi lock timeout/clock/source hoặc auth/gate. Đây không phải một failure chưa rõ nguyên nhân như hai failure lịch sử trong inventory.

Test/evidence tham khảo ngoài repo:

- `C:\Users\ADMIN\AppData\Local\Temp\ras-pr62-review-20261017\test_review_lock_order_direct.py`
- `C:\Users\ADMIN\AppData\Local\Temp\ras-pr62-review-20261017\test_review_lock_order.py` (HTTP)
- Hai XML/log và `lock-result-True.json` / `http-lock-result-True.json` cùng thư mục.

Tài nguyên/credentials reviewer đã dọn. Tạo context cô lập mới, không dùng cổng/project lịch sử hoặc dev.

## Cách sửa được phép

Chọn thay đổi nhỏ nhất loại cycle và giải thích lock graph sau sửa:

- Sắp cleanup theo thứ tự tương thích với review → item của acceptance/approval; **hoặc** xử lý deletion đã commit bằng receipt path không giữ review lock trong lúc chờ item của cleanup. Nếu dùng precheck, chứng minh binding/caller/gate/clock và các interleaving liên quan, không tin một hint stale thay proof.
- Giữ locks/binding cần thiết cho lần xóa đầu, manifest mọi file/version, atomic closure/deletion và approve-vs-delete. Không chuyển acceptance sang item → review một cách cơ học, tạo cycle khác với approval.
- Giữ final proof mới sau known commit, outcome độc lập teardown, và fail closed sau append/fsync/commit uncertain. Không tự retry mutation hoặc COMMIT uncertain để che deadlock; không chỉ bắt 40P01 rồi đổi mã thành busy/503.
- Giữ S3 ngoài DB/key/journal locks, fence/shared reference, scrub đầy đủ, late writer/settlement và restore sweep. Không bỏ scrub request, skip vĩnh viễn cleanup hoặc báo completed khi còn private text.
- Dự kiến chỉ cần Python/test/docs; không sửa migration `0052` đã có thể được áp vào DB lâu dài. Nếu thật sự cần schema mới, thêm revision sau head thực tế và chứng minh lý do, không tác động dev.

## Kiểm bắt buộc

1. Đưa regression tất định vào repo: item có ít nhất một review đã đóng do delete; pause cleanup sau item lock/trước scrub; repeat deletion tới điểm chờ; release. Cả cleanup và repeat phải hoàn tất đúng hợp đồng, không PostgreSQL deadlock, không success giả. Giữ control không review. Observer ghi SQLSTATE chỉ đọc, barrier luôn được thả ở `finally` khi test fail.
2. Thử thứ tự ngược, nhiều review request theo UUID, repeat sau purge, hai deletion đồng thời. Nếu chọn fast receipt, thêm ca liên quan tới precheck/proof để không bỏ kiểm resource/caller.
3. Hồi quy lần xóa đầu với review open, hai thứ tự approve/delete, receipt sau cleanup, expiry/CSRF/current caller, journal prefix/fsync/uncertain/release và shared key/late write. Giữ C4 readiness thật, grants/role và clock/timeout hiện hành.
4. Tái hiện test mới trên head review để chứng minh fail đúng lỗi, rồi chạy trên source sửa. Một countercheck đưa lại thứ tự cũ phải bắt `40P01` hoặc failure nghiệp vụ đúng; khôi phục bytes trước suite cuối.
5. Chạy các file M6d cùng nhóm approval/review/cleanup/C4/ORM liên quan. Không bắt buộc full 5.250 node hoặc mọi drill/browser lại cho một sửa Python lock hẹp; ghi đúng not-run và giữ kết quả full theo source cũ. Nếu thay parser, SQL/grants hoặc shared gate, mở rộng hồi quy theo rủi ro thật.

Không đọc/sửa `.env`, query/migrate/restart dev, dừng Dify, gọi provider/arXiv hoặc bật pricing thật. Dùng Conda ras, PostgreSQL/runtime/journal/ports/project riêng, có thể dùng tmpfs như runner repo. Không sửa guard/timeout hoặc giả clock để làm race pass. Không chạy mutant cùng suite hoặc suy pass từ rerun riêng.

## Bàn giao

Commit/push vào PR #62, cập nhật mô tả PR bằng tiếng Việt, **chưa merge**. Handoff/caller/lock map và inventory tách source/lượt cũ với sửa mới; ghi root cause, test trước/sau, outcome và giới hạn. Hai failure lịch sử chưa rõ nguyên nhân phải giữ nguyên trạng, không ghi sửa lock này đã giải thích chúng. F07 vẫn ngoài scope sửa hẹp.

Giữ report/prompt reviewer và mọi docs review/task trong PR. Không AI authorship/trailer/promo; cleanup chỉ tài nguyên thử có ownership rõ. Báo lại code/head, kết quả, not-run và đường dẫn chứng cứ để review lại.
