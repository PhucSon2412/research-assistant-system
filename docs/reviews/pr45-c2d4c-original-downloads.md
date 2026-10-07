# Review PR #45 — C2d-4c original downloads

Review ngày 2026-10-07 trên head `b23ff8221e78dc4465504ccf713b54eb832b918f`; code được kiểm `4c49919203db085dacd0778b3ce62e6787fd3823`, base `dd8bdc689ef7b495618116add4832628d7d24327`. PR #45 còn **OPEN tại lúc review**, sau đó chủ dự án merge `070cb37` ngày 2026-10-07; không rerun tại merge. [Task](../tasks/m6c2b2c2d4c-original-downloads.md) · [Bàn giao](../c2d4c-original-downloads.md) · [Inventory agent](../c2d4c-test-inventory.json).

## Kết luận

**Không phát hiện điểm chặn merge trong phạm vi C2d-4c; có thể merge PR #45.** Đúng hai GET giữ ba gate private/common/review riêng biệt; private có thêm kiểm size/SHA-256, common/review có proof response mới sau audit known commit. **Safety vẫn pending tới C4.** Kết luận này không mở readiness hoặc chứng nhận các writer/provider còn lại.

## Source đã rà soát

- [Boundary download](../../api/app/api/original_downloads.py): target frozen chỉ chứa scalar; proof ngắn khóa resource trước journal, exact caller/gate/target và clock sau SQL cuối. Session/connection trả trước storage/temp/hash. Review dùng request SHARE rồi private item SHARE qua exact grant; ordinary route không nhận review grant.
- Common/review audit riêng: User NO KEY UPDATE → resources → audit/FK flush → journal exclusive trực tiếp; actor GET không thêm CSRF/Origin của POST. Commit exception giữ outcome uncertain độc lập rollback/close. Sau known commit, transaction response mới kiểm lại quyền/clock; lỗi không phát file, retry audit hay xóa original.
- [Spool/response ownership](../../api/app/common_original.py): ba nhánh dùng chung pool mỗi process; băm cả object và actual spool trước byte đầu. Cancel giữ owner tới executor/read thread settled; response close qua executor, finalizer chỉ fallback. Không giữ DB lock khi stream.
- [Proxy streaming](../../web/lib/stream.ts): giữ Cookie và mọi Set-Cookie, nối abort/cancel tới upstream, không buffer/retry hoặc chèn JSON vào partial file. Headers từ target cuối, no-store/nosniff.
- AST của notice/intent/PUT và parser trong route uploads không đổi so base. Authz, auth safety, identity, models, migrations và hai helper upload không đổi. Head source vẫn `0039`; không suy revision DB dev.

## Kiểm độc lập

| Nhóm reviewer thực chạy | Kết quả |
| --- | --- |
| Toàn `test_c2d4c_original_downloads.py` | **330/330 pass** |
| Legacy `test_uploads.py` | **9/9 pass** |
| Đủ 10 regression P2 acceptance outcome/teardown PR #44 | **10/10 pass** |
| ORM/schema | **1/1 pass** |
| Tổng API selection | **350/350**, 0 fail/error/skip, JUnit 820,648 s; 3 warning hiện có |
| Web `npm test --prefix web` | **57/57 pass**, 0 fail/skip |
| Web typecheck | Đạt |

Đã chạy ACL A/B/Admin/cùng bytes, exact review/common rights, pending HTTP không patch, size/hash/temp/storage lỗi, auth/head/receipt/binding/role thay đổi, clock idle/absolute sau gate và predicate, audit uncertain + teardown, race closure/revoke, cancellation và ASGI 2.3/2.4. Fixture teardown kiểm attempt/temp/slot/connection; I/O bị chặn không giữ DB connection/locks.

350 JUnit node unique khớp nhóm trên và là tập con selection agent. 267 API Python/SQL/web source hash trước/sau reviewer không đổi; hai package files được đối chiếu thêm với seal agent 269 file, tất cả khớp source hiện tại. Không sửa source trong suite. JUnit reviewer SHA-256: `136722c433264a90567f1f25174608609613b447447186727799840c17818bdb`.

## Đối chiếu evidence agent

Reviewer xác minh **991 unique node**, 0 fail/error/skip, đúng selection 991/collect 3.246/not-run 2.255; 62 artifact SHA-256 và 269 hash source khớp. Commit sau code chốt chỉ đổi docs. Đây là đối chiếu artifact, **không phải reviewer chạy lại 991**. JUnit agent SHA-256: `0dbadadafe5149d9dab8a9f68f4244052f687b49d6e85ab872a796247bc0865f`.

Tám mutant XML đều có một assertion failure, không setup error/skip; bốn file được khôi phục đúng hash trước suite cuối. Hai mutant exact review gate/current role chỉ đổi mã deny; chúng không chứng minh bytes trái quyền đã phát. Sáu mutant còn lại bắt thiếu checksum/clock/proof sau audit/ownership. Không chạy lại mutant trong lượt reviewer.

Browser/30 MiB cancellation và web build thuộc evidence agent: Chrome qua Next/API/MinIO, 7 tải đúng SHA và 3 denial; cancel HTTP qua Next rồi dùng lại slot. Reviewer không chạy lại browser/build; không cộng các kết quả đó vào 350. Khoảng 0,500 s agent ghi là tổng một kịch bản, không p95 hoặc latency riêng byte đầu.

## Giới hạn và phần còn lại

- Reviewer chưa chạy **641 node trong selection agent**, **2.896 node trong collection hiện hành**; provider/search/RAG/worker và tổng cutover còn riêng. Không suy full inventory pass từ selection.
- POST review/publication dùng engine đặc quyền để dựng resource fixture vì các writer đó chưa đủ runtime journal privileges. Auth/GET vẫn dùng runtime và head/receipt/journal thật; không đổi grant hoặc chứng nhận POST runtime-ready. Pending HTTP riêng giữ gate thật.
- Boundary response cuối thắng thì stream có thể tiếp tục sau closure/revoke; request mới bị chặn. Bytes đã phát không thu hồi được, không kiểm quyền mỗi chunk.
- Pool 4 mỗi process là cấu hình ban đầu cần đo. SDK timeout không phải deadline tổng; không hứa kill thread treo. Fault injection không chứng minh crash/mất điện hay độ bền production. Không gọi provider hoặc đổi pricing/readiness.

## Môi trường, lỗi tooling và cleanup

Python/pip được kiểm ở Conda `ras`. PostgreSQL/MinIO riêng: `ras-pr45-review-db-20261006` port 58458 và `ras-pr45-review-minio-20261006` port 59458, label `ras.task=pr45-review-20261006`. Credentials tường minh trước import fixture, không fallback `.env`; chặn HTTP ngoài local và bỏ key provider khỏi process env.

Lượt đối chiếu hash reviewer đầu chưa chuẩn hóa dấu phân cách Windows/Posix nên helper báo KeyError; đã sửa helper ngoài repo, không đổi source/test hoặc chạy lại suite để che lỗi. Sau chuẩn hóa, 267 hash và hai package files khớp. API lượt reviewer không fail/error/skip.

Evidence giữ ngoài repo ở `%TEMP%/ras-pr45-review-20261006`: `review.xml`, `review-report.json`, `review-source.json`, `agent-artifact-check.json`, scope/quality checks, runner/setup và cleanup. Đã kiểm exact ID/name/label/image/port/mount/sole consumer rồi dọn đúng **2 container/2 volume thử**, xóa đúng ba file credentials/env. **24 container có sẵn giữ nguyên ID/image/StartedAt**. Không query/migrate/restart dev, đọc/sửa `.env` hoặc gọi provider. Journal/evidence thử giữ ngoài repo.

Lượt này chỉ cập nhật docs review và liên kết; không sửa code ứng dụng, commit/push, comment PR hoặc merge. Coding agent cần giữ các docs review chưa commit trong PR/lượt tiếp theo.
