# Review PR #54 — C2d-5e2 reconcile original mồ côi qua runtime

**Cập nhật sau review:** PR #54 đã merge tại `37edd5c9c28e60ba43425f0b1bd9f42946585e28`; merge tree khớp head `cf66f1c`, chưa rerun tại merge. Các trạng thái OPEN và kết quả dưới đây giữ đúng source/thời điểm review. Đã giao [C2d-5f1 citation reconcile runtime](../tasks/m6c2b2c2d5f1-citation-reconcile-runtime.md), mới là tài liệu; safety pending tới C4.

Ngày **2026-10-10**. PR **OPEN, chưa merge**; head `cf66f1ccaddccb81a1c40121edab7af231e6fac5`, base `b2e76edb028af2ff118d3936ed4cdf47cb88a99f` (merge PR #53). Production code `ebbc27ac88cac804fcc50a42cf3c5d28117451f4`; commit `9faf0d17e341b107129d2cc8e4d6d394c103fbf4` chỉ sửa barrier của một test, sau đó chỉ tài liệu. Migration nguồn **0045**; không truy vấn revision DB dev.

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi PR #54. Có thể merge luồng reconcile thủ công này.** Reviewer thực chạy **125/125 pass**, 0 failure/error/skip. Safety **pending tới C4**; kết luận không mở pricing, cutover hoặc chứng nhận các consumer chưa chuyển.

Ba thay đổi hành vi phù hợp task đã giao: chỉ login runtime được provision, CLI mặc định preview và cần `--apply` để xóa, chỉ nhận key `originals/<32 hex thường>`. Đây là lựa chọn scoped của orchestrator theo ủy quyền, không phải một xác nhận trực tiếp mới của chủ dự án hoặc quy tắc kiến trúc vĩnh viễn. Lệnh Compose owner cũ được đánh dấu superseded; key lạ được giữ lại.

## Kiểm code và hợp đồng

- [Boundary reconcile](../../api/app/ingest/orphans.py) và [CLI](../../api/app/cli/reconcile_originals.py) kiểm runtime thật, hai routine và gate trước listing; preview không HEAD/fence/cursor/audit/delete. Phân trang và số candidate được giới hạn; cursor chỉ là hint, pending fence được tiếp tục trước listing.
- [Migration 0045](../../api/migrations/versions/0045_orphan_object_fences.sql) tách orphan fence/completion khỏi fence revoked-common 0044. Routine kiểm **mọi** SourceFile cùng key, không lọc visibility/state/owner; hint không cấp quyền xóa. Routine và trigger INSERT cùng key advisory lock, bắt buộc READ COMMITTED; hai thứ tự race không làm lọt tham chiếu. Storage key vẫn bất biến khi UPDATE. Fence/completion không sửa/xóa/truncate, cả owner; grant runtime chỉ qua routine, recovery không có EXECUTE hai routine.
- Prepare flush và kiểm gate/binding/clock cuối, commit xác định và release sạch, rồi dùng proof mới trước xóa. LIST/HEAD/DELETE đều ngoài Session/connection/lock DB. Finalize riêng, completion và audit tối đa một hàng/fence; S3 DELETE có thể lặp.
- Commit không xác định, lỗi release/gate/storage/clock dừng pass, không SELECT đoán kết quả hoặc tự retry. Invocation độc lập tiếp tục từ fence kể cả object không còn trong listing. Phân biệt missing với 403/5xx/timeout; đổi metadata giữa listing/HEAD hoặc sau fence không bị xóa theo quan sát cũ.
- Common cleanup/operator/recover giữ đường riêng; không tạo tombstone giả, không mở quyền User/Admin hoặc sửa ledger. Chỉ những lượt đã qua pre-delete boundary mới có thể hoàn thành DELETE khi gate đóng sau admission; finalize và candidate kế tiếp bị chặn.

## Reviewer thực chạy

Conda `ras`: Python **3.12.14**, pip **26.2.1**. PostgreSQL/MinIO/Redis/journal và login runtime/recovery cô lập; không sửa production/test repo. Hash **321 file** `api/` + `scripts/` trước/sau khớp, đồng thời khớp source seal agent.

| Selection | Kết quả |
| --- | ---: |
| Toàn bộ `test_c2d5e2_orphan_reconcile.py` | 120/120 pass |
| Upgrade 0044 có dữ liệu/provision → 0045, grants/reprovision và downgrade/roundtrip | 2/2 pass |
| `test_orm_models_match_migrated_schema` | 1/1 pass |
| CLI Linux SIGKILL sau fence commit trước DELETE, resume và idempotence | 1/1 pass |
| Regression reviewer: key lock timeout giữ object, không fence/completion/audit; lần sau chạy được | 1/1 pass |
| **Tổng thực chạy** | **125/125 pass, 0 failure/error/skip** |

123 ca repo chạy **285,18 giây**, ba warning hiện hữu (Starlette và hai computed-default Alembic); hai ca ngoài repo chạy **12,87 giây**, một warning Starlette. Smoke dùng bản sao test ngoài repo, chỉ đổi tag image thử; mount source hiện hành vào image dependency có sẵn. Fixture chỉ scope cutover/clock trong thử nghiệp vụ; các ca pending/hold giữ gate thật. Không phải kiểm build image mới hoặc cutover production.

**4.567/4.690 node repo chưa rerun** trong lượt reviewer; hai ca ngoài repo không nằm trong mẫu số đó. Nhánh `skipped:busy` vốn chưa chạy trong báo cáo agent đã được reviewer kiểm riêng; SET ROLE qua membership vẫn chưa chạy. Không chạy lại web/Chrome, 81 ca cleanup runtime, toàn bộ debt, drill backup/restore hoặc các nhóm ngoài selection.

## Đối chiếu evidence agent, không phải rerun toàn suite

**179/179 artifact SHA-256** khớp. JUnit mười nhóm khớp exact selection và full collection, không thiếu/trùng/uncollected: **2.096 unique pass**, **4.690 collected**, **2.594 not-run**, exit 0. Source-before/source-after/current **321 file** khớp. Chín countercheck có assertion failure, không setup error; reviewer kiểm artifact và cơ chế bị phá, không chạy lại mutant trên source.

Đã kiểm ba bộ artifact process được lưu (inspect exit **137**, hai fence/completion/audit, ba login facts); seal cuối chỉ tính **một** smoke pass. Không cộng các lượt chuẩn bị hoặc `sealed-1` exit 1 vào pass mới. Helper đối chiếu ban đầu giả định chỉ có một thư mục process và dừng ở assertion vì có ba lượt lịch sử; sửa helper kiểm tất cả artifact process trong manifest, giữ nguyên bằng chứng.

**25 baseline debt** được deselect và nằm trong not-run; bốn debt PR #50 ngoài selection vẫn chưa được sửa/chứng nhận. Reviewer không rerun base hoặc gọi toàn inventory xanh.

## Giới hạn giữ nguyên

- Quiescence là điều kiện vận hành CLI **không tự chứng minh**: phải settle mọi upload/approval/SDK/DB thread và remote transaction trước chạy; grace không thay thế điều kiện này. Chưa có online GC/beat schedule, multipart cleanup hoặc lifecycle cho writer đang bay.
- HEAD/ETag/Last-Modified không phải conditional delete hay bằng chứng bytes bất biến. Ghi đè cùng key/size/giây có thể không bị phát hiện ngoài môi trường quiescent; không có exactly-once S3 hoặc rollback object cùng DB.
- Fence nằm trong DB; restore snapshot cũ hơn fence không được orphan journal bảo vệ toàn diện. Chưa diễn tập restore fence, chứng minh độ bền mất điện, benchmark/SLO hoặc đối soát provider charge.
- Discovery/citation, search/query/rerank, RAG, tổng hồi quy C2d và C3/C4 vẫn riêng. Chưa giao task mới trong lượt review này.

## Evidence và tài nguyên

Evidence ngoài repo: `%TEMP%\ras-pr54-review-20261010`, gồm `scope.{log,xml}`, `smoke.{log,xml}`, regression ngoài repo, `agent-evidence-verification.json`, hash source, public Docker snapshots và `review-results.json`.

Đã kiểm label/sole consumer và gỡ đúng project `rasc2d5e2cf43b3f2`: **3 container, 3 anonymous volume, 1 network**; gỡ một tag image reviewer, giữ image gốc. Chín container có sẵn giữ ID/name/image; volume và image tag về đúng baseline. Generated-secret scan UTF-8/UTF-16 chỉ trúng hai file cấu hình thử, đã xóa cùng `empty.env`.

`ras-beat-1` RestartCount **908 → 914**, `ras-worker-1` **909 → 915**; StartedAt cũng thay đổi trong khoảng review. Không thao tác chúng, chưa điều tra nguyên nhân, không khẳng định trạng thái dev bất biến.

Không đọc/sửa `.env`, query/migrate/restart dev, gọi provider/arXiv hoặc đổi pricing. Chỉ cập nhật tài liệu review; chưa commit/push/merge hoặc đăng comment PR.
