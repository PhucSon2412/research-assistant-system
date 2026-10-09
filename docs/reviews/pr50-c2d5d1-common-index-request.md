# Review PR #50 — C2d-5d1 common index request

**Cập nhật sau review (2026-10-09):** PR #50 đã merge tại `03b79855221f3d9279cbe5aa677d6a50db59b5ab`; head/tree review khớp merge. Không chạy lại test tại merge; nội dung/kết quả bên dưới giữ theo thời điểm và source review. [Task tiếp C2d-5d2a](../tasks/m6c2b2c2d5d2a-worker-runtime-extraction.md) chỉ là kế hoạch chưa triển khai; safety pending tới C4.

Ngày **2026-10-09**. PR **OPEN, chưa merge** tại lúc review. Base merge PR #49 `859dfe9793dd9549e3ca9b8f0102361f414d0340`; head `ff5fa825d96fe78ec1fbdbb3aeb5f84e0b58a4dc`. Code/test tại `e283bc6400efdefd4641838ce4d94f4ccee3baaa`; diff từ code tới head chỉ tài liệu. Source migration **0042**, không migration/grants mới, không suy revision DB dev.

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi C2d-5d1. Có thể merge PR #50 theo phạm vi một POST tạo/trả job index.** Safety vẫn **pending tới C4**; worker/ingest/index/provider dispatch và C3/C4 chưa được chứng nhận. Reviewer chỉ cập nhật docs, không sửa implementation, commit/push/merge hoặc đăng comment PR.

[Task](../tasks/m6c2b2c2d5d1-common-index-request.md) · [Handoff](../c2d5d1-common-index-request.md) · [Inventory agent](../c2d5d1-test-inventory.json).

## Đối chiếu code và hợp đồng

- Đúng `POST /admin/common-copies/{item_id}/index` qua [common_index_request](../../api/app/api/common_index_request.py). Xác nhận dùng StrictBool + true validator; `1`/`1.0` không còn tạo job. Origin/session/CSRF/current Admin và generation/head/receipt/status/TTL được kiểm, không tin dependency snapshot để commit/response.
- Acceptance khóa Admin User → item UPDATE → rights/publication/jobs/version/file SHARE, flush DML/FK/unique/audit trước journal exclusive trực tiếp. Eligibility kiểm listed/rights/publication/file/checksum/epoch/tombstone, audience, fulltext + external processing, conditions/MIME. Job ghim tuple; reuse không sửa actor/state/lease/cancel. Job sai binding hoặc đang hủy trả 409, không re-arm. Item trước job, không khóa tombstone sau item, không lấy row lock mới sau journal.
- Không storage/parser/broker/provider trong endpoint; job DB tồn tại khi Redis không dùng được, không yêu cầu key/pricing verified để tạo job. Không đổi extraction/coverage/reindex hoặc content gates. Helper `common_index.start` tự commit đã gỡ, gate đọc dùng chung giữ nguyên.
- Current actor/clock sau SQL cuối trước commit; commit exception luôn uncertain, không SELECT suy success/replay/retry/IDs. Outcome giữ facts qua rollback/close/invalidate. Known commit hậu kiểm lỗi giữ job; response dùng Session/proof mới theo Admin history + retained job, state hiện hành kể cả revoke/cleanup/scrub. Release lỗi có JSON 503 và trả connection.
- Business denial audit dùng transaction riêng có current Admin/writer proof, chỉ IDs/mã; lỗi audit không thay denial. Không áp ngoại lệ restriction-only của revoke cho việc tạo job.
- Form có ref/busy guard; checkbox ban đầu rỗng, success refresh trạng thái; response mất/504/uncertain không tự POST lại hoặc khẳng định chưa tạo job. Normal proxy 15 s giữ nguyên. Chrome dùng response barrier sau known commit để tái hiện 504, không phải lỗi mạng thật.

## Reviewer thực chạy

Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe` **3.12.14**, pip **26.2.1**; không tạo venv. PG/MinIO/journal thử riêng, runtime credentials thật, provider key trống và HTTP ngoài bị chặn. Business fixture chỉ patch cutover trong phạm vi test; HTTP pending giữ production gate thật.

| Kiểm chứng | Kết quả |
| --- | --- |
| Toàn `test_c2d5d1_common_index_request.py` | 171/171 pass |
| Toàn `test_migrations.py`, gồm ORM/schema | 28/28 pass |
| Chrome qua Next production proxy → socket Uvicorn API | 1/1 pass |
| **Tổng API unique** | **200/200**, 0 fail/error/skip |
| Web tests | 70/70 pass |
| Web typecheck/build | exit 0/0 |
| Reviewer source hashes trước/sau | 281 file khớp |

Nhóm core 199 ca hoàn tất trong 529,34 s, ba warning thư viện/autogenerate hiện có; Chrome 24,24 s, một Starlette warning lặp lại. Đây là thời gian chạy test, không phải số đo latency/SLO. Selection 200 node và JUnit khớp exact core selection của agent, không thiếu/trùng. Theo collect 4.268 của agent, **4.068 node không được reviewer chạy lại**; không cộng kết quả lịch sử thành pass mới.

Chrome kiểm unticked không POST, hai submit đồng bộ một POST, create/reuse đúng job, refresh, known commit giữ response quá 15 s → 504/unknown/no repost; proxy từ chối số `1`, User, session invalid và Admin mất role. Không Compose API image, full cutover, provider live hoặc benchmark.

## Artifact của agent, kiểm riêng với lượt reviewer

- Đã kiểm manifest **135 file**; sáu JUnit/selection khớp **923 unique pass**, 0 failure/error/skip: core 200, revoke 171, publish 192, reads 260, content 89, recovery 11. Không gọi đó là reviewer chạy lại 923 ca.
- **299 source hashes** trước/sau agent khớp nhau và worktree; tracked source khớp `e283bc6` sau chuẩn hóa CRLF/LF (102 file cần chuẩn hóa khi so Git blob). `web/next-env.d.ts` là generated/ignored, không nằm trong commit; hash của nó chỉ chứng minh ổn định trên disk.
- Chín XML mutant có failure, không setup error; đã đối chiếu log/message. Mutant bỏ item lock chỉ chứng minh assertion quan sát lock wait thất bại, **không** chứng minh một lỗi nghiệp vụ/duplicate đã xảy ra. Giữ giới hạn này, không nâng mức chứng cứ.
- Bốn ca lỗi riêng có cùng assertion trên base/head trong log/XML: hai `test_common_search` thiếu quyền `session_replication_role`, `test_health::test_readyz_ok_after_migration` đòi 200 khi safety pending, và `test_common_original::test_common_original_refused_unless_live_rights_open_that_exact_file` nhận revoke 409. Runner base trỏ `git archive` tree riêng; sáu file liên quan đã đối chiếu với `859dfe9`. Reviewer **không chạy lại** bốn ca này hoặc chứng minh nguyên nhân sâu hơn.

## Việc cần theo dõi, không chặn scope PR này

1. Đưa bốn ca lỗi baseline vào task tổng hồi quy C2d trước C4. Tách owner setup corruption khỏi runtime (`session_replication_role`); giữ pending readiness đúng hợp đồng; điều tra revoke 409 của fixture download. Không nới auth/resource gate hoặc đổi expectation nghiệp vụ chỉ để có số pass.
2. C2d-5d2 cần khảo sát worker/beat/claim/checkpoints/indexer/cleanup và role. Compose worker còn dùng `POSTGRES_USER`; kiểm tương thích hiện tại không chứng minh worker đã chuyển runtime. Không mang session giả hoặc buộc tuổi thọ job vào cookie Admin đã gửi POST.
3. Production journal storage/độ bền, pricing/charge/provider routing, C3/operator/delivery và cutover C4 còn theo các quyết định mở. Không dùng kết luận review này mở safety hoặc provider thật.

## Evidence và cleanup

Evidence reviewer: `%TEMP%\ras-pr50-review-20261009` — `review-results.json`, selection, hai JUnit/log, web logs, source hashes, artifact verifier, mutation messages và baseline/cleanup. Evidence agent giữ ở `%TEMP%\ras-c2d5d1-20261009`; không đổi/xóa các artifact đó.

Đã gỡ đúng **2 container/2 anonymous volume** reviewer sau kiểm exact ID/label và sole consumer; xóa ba file credentials, giữ logs/XML/hash. **Chín container có sẵn giữ nguyên ID/name/config image/image ID/StartedAt** so snapshot đầu và trước/sau cleanup. Quét credentials sinh cho review trên evidence: 0 hit. Không đọc/sửa `.env`, query/migrate/restart dev, gọi provider, đổi pricing hoặc mở gate.

Lỗi công cụ review, không phải test ứng dụng: lần đối chiếu Git blob đầu chưa xử lý CRLF và generated `next-env.d.ts`, đã sửa verifier; guard cleanup lần đầu so ID ngắn với ID đầy đủ nên dừng **trước mọi xóa**, đã dùng `--no-trunc` và chạy lại thành công. Không có source change trong suite; diff/links/control chars kiểm riêng sau khi ghi docs.
