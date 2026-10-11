# Review PR #55 — C2d-5f1 citation reconcile qua runtime

**Cập nhật sau review:** PR #55 đã merge `e6b5991dd7ea9df6b8a5d9307f100ab0b87c7faa` lúc `2026-10-10T18:07:01Z` (ngày 2026-10-11 giờ Việt Nam); ancestor/tree khớp head review `bca5d90`. Không rerun tại merge; phần dưới giữ nguyên chứng cứ/thời điểm review. [Kế hoạch MVP](../mvp-delivery-plan.md) và [task C2d-5f2](../tasks/m6c2b2c2d5f2-discovery-runtime.md) mới giao, chưa triển khai/kiểm. Safety pending tới C4.

Ngày **2026-10-11**. PR **OPEN, chưa merge**; head `bca5d90f955f8e1731b3236e2e925cea8bff6976`, base `37edd5c9c28e60ba43425f0b1bd9f42946585e28` (merge PR #54). Production code `8fb9671`; `8e2a500835403cd59ea36e4004339a83eee3d5f2` chỉ sửa teardown test, head sau đó chỉ tài liệu. Source migration **0045**, không migration/grant mới; không truy vấn revision DB dev.

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi PR #55. Có thể merge citation reconcile runtime.** Reviewer thực chạy **121/121 pass**, 0 failure/error/skip. Safety **pending tới C4**; discovery harvesting, các consumer còn lại và cutover chưa được chứng nhận.

CLI owner cũ bị từ chối, `now` không còn là tham số direct entry, counts chỉ cộng sau known commit/release sạch, và timeout citation lock trả `busy:citation_lock` là các lựa chọn phù hợp [task đã giao](../tasks/m6c2b2c2d5f1-citation-reconcile-runtime.md). Không phải xác nhận trực tiếp mới của chủ dự án hoặc luật kiến trúc vĩnh viễn. [Handoff](../c2d5f1-citation-reconcile-runtime.md) và [inventory agent](../c2d5f1-test-inventory.json) giữ giới hạn chi tiết.

## Kiểm code và hợp đồng

- [Citation boundary](../../api/app/citations.py): mỗi batch kiểm login runtime thực, từng quyền cần thiết và writer gate trong transaction riêng, release trước hint/reference read. `enabled=false` không SQL. Không owner/recovery fallback, không thêm quyền hoặc bỏ pending gate.
- Batch lấy common item `FOR KEY SHARE SKIP LOCKED` theo UUID, kiểm revoked/tombstone dưới khóa, rồi reference `FOR SHARE SKIP LOCKED` theo ID. Reload raw citation/tuple trước upsert. Citation shared lock giữ serialization với discovery; DML/FK flush trước journal exclusive. Scalar binding và clock mới chạy sau journal wait/SQL cuối. Keyset trong pass đi qua candidate bận/bị xóa, số batch hữu hạn.
- Upsert chỉ đổi hàng khác giá trị và đếm mutation qua `RETURNING`. Commit exception dừng với uncertain, không SELECT suy thành công, không retry batch. Known commit nhưng release lỗi giữ dữ liệu, báo `committed_unreleased` và dừng. `runtime.short` giữ outcome độc lập teardown; entry/release fault không tiếp tục private read/write.
- Discovery hook khóa common item trước citation exclusive; item xuất hiện muộn dùng `NOWAIT`, bận thì rollback page qua `RetargetBusy`, không advance cursor. Thứ tự này loại vòng item → citation / citation → item với extraction. Conditional Core UPDATE xử lý link biến mất giữa lúc chọn và cập nhật; không commit trong hook. Extraction giữ transaction của worker và final boundary hiện có.
- Common revoked **hoặc** có tombstone bị loại; hai thứ tự với revoke và cleanup không làm viết lại link sau thu hồi. Trigger 0024 giữ nguyên. Private cùng bytes không bị ảnh hưởng. Matcher `arxiv-exact-v2`, owner ACL, freshness/OAI tombstone và ngữ nghĩa bibliography không đổi; không đưa citation link vào index/evidence/provider payload.
- [CLI](../../api/app/cli/reconcile_citations.py) và [Celery task](../../api/app/ingest/celery_app.py) dùng cùng boundary; wrapper gate của task không thay cho kiểm từng batch. Không network/storage/provider I/O trong backfill. `run_sync` chưa chuyển runtime boundary; sửa hook không chứng nhận toàn harvesting.

## Reviewer thực chạy

Conda `ras`: Python **3.12.14**, pip **26.2.1**. PostgreSQL/MinIO/Redis/journal và role runtime/recovery cô lập. Không sửa production/test repo. **327 file** `api/` + `scripts/` khớp hash trước/sau, đồng thời khớp source seal agent.

| Selection | Kết quả |
| --- | ---: |
| `test_c2d5f1_citation_runtime.py` + `test_c2d5f1_worker_hooks.py` | 45/45 pass |
| Toàn bộ `test_citations.py` | 73/73 pass |
| `test_orm_models_match_migrated_schema` | 1/1 pass |
| CLI Linux SIGKILL giữa batch, resume và idempotence | 1/1 pass |
| Celery worker/beat qua Redis, SIGKILL worker, resume và idempotence | 1/1 pass |
| **Tổng thực chạy** | **121/121 pass, 0 failure/error/skip** |

119 ca repo chạy **176,11 giây**, ba warning hiện hữu (Starlette và hai computed-default Alembic). Hai smoke ngoài repo chạy **29,81 giây**, một warning Starlette. Selection/JUnit 119 ca khớp exact node trong full collection agent. **4.618/4.737 node repo chưa rerun**; hai smoke dùng bản sao test ngoài repo nên không cộng vào mẫu số repo.

Smoke chỉ đổi tag image trong bản sao test ngoài repo, mount source hiện hành vào image dependency có sẵn; **không kiểm build image mới**. Fixture/sitecustomize scope cutover cho nghiệp vụ, chặn egress và đặt điểm dừng ở batch hai; ca pending/hold giữ gate thật. CLI bị kill trả **137**, batch một đã commit, chạy lại đạt năm link và lượt ba không đổi count/timestamp. Celery dùng worker/beat/Redis thật, gửi SIGKILL, sau restart đạt năm link và không đổi qua các tick tiếp theo. Không phải bằng chứng mất điện, exactly-once hoặc cutover production.

Các ca thực chạy gồm login/grant/gate trước private read; gate đóng giữa batch hoặc trước final; commit landed/not-landed bị mất phản hồi; release, binding, clock; timeout citation lock; candidate bận; concurrent backfill; revoke hai thứ tự; discovery/extraction lock wait; arrival hai thứ tự; late item NOWAIT/page rollback; cleanup không tái tạo link. Không chạy lại web/Chrome, toàn bộ debt, drill backup/restore hoặc các nhóm ngoài selection.

## Đối chiếu evidence agent, không phải rerun toàn suite

**313/313 artifact SHA-256** khớp. JUnit tám nhóm khớp exact selection và full collection, không thiếu/trùng: **1.203 unique pass**, collect **4.737**, **3.534 not-run**. Source-before/after **327/327** khớp source hiện hành. Tổng nhóm: smoke 2, citation-runtime 45, revoke 170, citations 486, schema 244, cleanup 104, extraction 138, recovery 14.

Mười countercheck trong `counterchecks-2` đều có đúng một testcase thất bại bằng **assertion**, không setup/teardown error. Reviewer kiểm artifact, không tự chạy lại mutation code. Lượt `counterchecks-1` có teardown error ở lock-order giữ làm lịch sử, không tính thay lượt hợp lệ. Hai process record thuộc seal có identity runtime/grants đúng và năm link; CLI có log exit 137, Celery có log lệnh SIGKILL. Các record chuẩn bị khác không cộng thêm pass seal.

**27 debt node** vẫn not-run trong seal: 25 mang từ PR #54 và hai ca discovery mới được agent ghi nhận fail ở base do bootstrap legacy trước citation. Reviewer không rerun base/debt trong lượt này; không coi chúng đã sửa. Bốn debt được ghi từ PR #50 cũng chưa chứng nhận lại. Số pass agent không được gọi là toàn API suite hoặc kết quả reviewer chạy mới.

## Giới hạn và bàn giao

- C2d-5f2 phải chuyển `run_sync`/CLI/task harvesting: runtime/gate, không giữ DB connection qua HTTP/sleep, pacing/lease/cursor/backoff/outcome và fan-out retarget. HTTP 406 chưa giải quyết, không thử live arXiv.
- Backfill chưa có cursor/lease bền qua invocation; idempotence và DB batch không bảo đảm task chạy đúng một lần. Batch size/timeout chưa đo hiệu năng.
- Discovery retarget vẫn fan-out không giới hạn theo page; không được cắt link rồi advance cursor mà mất phần còn lại. Hợp đồng item → citation → journal cần giữ khi chuyển harvesting.
- Không kiểm provider/charge, production journal storage, backup/restore drill, durability mất điện hoặc C3/C4. Không mở `pricing_verified`, safety hoặc quyền nội dung mới.

## Dọn dẹp và phạm vi thao tác

Evidence reviewer: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr55-review-20261011` (JUnit/log, source manifest, artifact verification, Docker baseline và kết quả cleanup). Đã gỡ ba container hạ tầng thử, sáu volume của đúng project (ba volume smoke có label project và không consumer), một network và một tag image thử; worker/beat/CLI thử được smoke tự gỡ. Credentials sinh mới chỉ xuất hiện trong hai file cấu hình thử đã xóa; quét không thấy trong artifact còn lại hoặc file tracked đã kiểm.

Chín container có sẵn giữ cùng ID/name/image; inventory volume và image tag trở lại baseline. **Không ghi rằng trạng thái dev hoàn toàn không đổi:** `/ras-worker-1` và `/ras-beat-1` đã Restarting ở baseline, RestartCount cùng tăng **1107 → 1111**, `StartedAt` đổi. Reviewer không gửi lệnh mutate đến hai container này, không xác định nguyên nhân.

Không đọc/sửa `.env`, query/migrate/restart dev, gọi provider/arXiv, đổi pricing hoặc cutover. Chỉ cập nhật tài liệu review; chưa commit/push/merge hoặc đăng comment PR.
