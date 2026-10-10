# Review PR #51 — C2d-5d2a worker runtime/extraction

**Cập nhật sau review (2026-10-10):** PR #51 đã merge tại `9fb8890568af85b7beea4503f93f12e2896e6c89`; head review là ancestor và tree khớp merge. Không chạy lại test tại merge; số và kết luận bên dưới giữ theo source/thời điểm review. [Prompt tiếp C2d-5d2b](../tasks/m6c2b2c2d5d2b-document-index-runtime.md) mới là kế hoạch, chưa triển khai; safety pending tới C4.

Ngày **2026-10-10**, tiếp tục lượt review bắt đầu ngày 2026-10-09. PR **OPEN, chưa merge** tại lúc review. Base merge PR #50 `03b79855221f3d9279cbe5aa677d6a50db59b5ab`; head `b569d0cbb128b85ccd9854aff2d8e2ac7106412c`. Production code `5b505d3f33ca8180cbb0bf4b3a58f08b0cf539a4`, code/test sealed `dfca84ac04a68bbce29abe0e9d1b87ac51da28ac`; diff từ sealed commit tới head chỉ docs. Source migration **0042**, không migration/grants mới; không truy vấn revision DB dev.

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi C2d-5d2a. Có thể merge PR #51 theo phạm vi worker/beat runtime và extraction tới `awaiting_index`/`metadata_only`.** Safety vẫn **pending tới C4**. Document embedding/index/provider dispatch, các task nền còn legacy, mặc định Compose/API và C3/C4 chưa được chứng nhận.

Reviewer chỉ cập nhật tài liệu, không sửa production/test, commit/push/merge hoặc đăng comment PR. [Task](../tasks/m6c2b2c2d5d2a-worker-runtime-extraction.md) · [Handoff](../c2d5d2a-worker-runtime-extraction.md) · [Inventory agent](../c2d5d2a-test-inventory.json).

## Đối chiếu code và hợp đồng

- [Runtime](../../api/app/ingest/runtime.py) kiểm danh tính và quyền DB thực, từ chối owner/superuser/recovery/membership/DDL/TEMP/BYPASSRLS/raw protected writes hoặc thiếu routine/grant. Không fallback credential. Worker child dispose pool kế thừa; task entry và checkpoint trực tiếp đều có guard.
- [Checkpoint](../../api/app/ingest/checkpoints.py) dùng owner User khi cần → item → job/resources → flush FK/citation/audit → journal exclusive trực tiếp → gate/binding/lease/clock mới → commit. Không thêm row lock sau journal. Proof trả khỏi transaction là dữ liệu detached, không ORM lazy load. Commit outcome độc lập teardown; commit không rõ kết quả dừng attempt, không suy success bằng SELECT hoặc chạy stage/failure writer tiếp.
- Private job kiểm owner/status theo latest head/receipt, không dùng session của người upload làm tuổi thọ job. Disabled/held thì pause. Common job kiểm đúng version/file/checksum/rights/epoch/publication/tombstone và phạm vi; không phụ thuộc session Admin đã tạo job. Binding, cancel, lease và clock được kiểm lại sau I/O, trước checkpoint ghi.
- [Worker](../../api/app/ingest/worker.py) release connection trước S3/parser/broker; kiểm kích thước và SHA-256, giữ coverage, locator và cách ly bibliography. Claim/retry/reuse dữ liệu không tự công bố hoặc gửi lại provider. Extraction thành công ghi `extraction_finished_at`, giữ `finished_at=NULL`, nhả lease ở `awaiting_index`; item vẫn `metadata_only`.
- [Celery](../../api/app/ingest/celery_app.py) chỉ chuyển UUID job, kiểm dispatchability mới trước broker và chịu Redis ngắt mà job vẫn nằm trong PostgreSQL. Consumer vẫn phải kiểm lại vì quyền có thể đổi sau kiểm dispatch. Không dispatch `awaiting_index`/`indexing`/retry index tới indexer legacy.
- [Override runtime](../../scripts/compose.worker-runtime.yml) chỉ phục vụ tích hợp cô lập với credential tường minh. Compose gốc không đổi; worker/beat mặc định còn owner sẽ fail closed. Đây là trạng thái maintenance đã ghi trong task, không phải cấu hình cutover hoàn chỉnh. Discovery/citation/cleanup nền mới có entry compatibility guard, chưa được chứng nhận toàn bộ writer nghiệp vụ.

## Reviewer thực chạy

Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe` 3.12.14, pip 26.2.1; không tạo venv. PostgreSQL/MinIO/Redis/journal và role runtime cô lập; settings trước import, không fallback `.env`, key provider trống và egress ngoài bị chặn. Business fixture chỉ patch cutover trong test; các bài pending giữ gate production thật.

| Kiểm chứng độc lập | Kết quả |
| --- | ---: |
| Hai file test worker runtime/common extraction mới | 74/74 pass |
| Toàn `test_migrations.py`, gồm ORM/schema | 28/28 pass |
| Worker + beat Linux thực, Redis stop/start, ba job | 1/1 pass |
| **Tổng API unique** | **103/103 pass**, 0 fail/error/skip |
| SHA-256 source/config/test/script trước/sau | 293 file khớp |

Core chạy 266,27 s; process smoke 23,67 s. Đây là thời gian chạy test, không phải latency/SLO. JUnit 103 node không trùng; theo collect 4.343 của agent, **4.240 node không được reviewer chạy lại**. Không chạy web/Chrome vì PR không đổi web; không chạy full business/cutover, provider thật hoặc benchmark.

Process smoke dùng image dependency có sẵn `rasc2d5d2a-worker:20261009`, ID `sha256:2ebafdf61a3fdcff8f712bce8309355a870182bca59995b9e78c3014d6b4e674`, mount source hiện hành read-only. Không coi đây là clean build image phát hành. Connection của chính worker/beat chứng minh `c1_runtime`, không raw User write/TEMP hay credential operator/recovery. Ba job private Markdown, private PDF và common promotion đều `awaiting_index`, attempt 1, có extraction time, chưa finished; không generation/index entry/embedding call. Sitecustomize chỉ dùng trong test để giới hạn schedule/egress và patch cutover; không có bypass production mới.

## Artifact agent, kiểm riêng

- **218 file artifact** khớp SHA-256/kích thước manifest. Bảy nhóm selection/JUnit khớp **549 unique pass**, 0 failure/error/skip: core 75, extraction 110, request 171, revoke 170, upload 9, recovery 11, schema 3. Không gọi 549 ca này là reviewer chạy lại.
- **293 source hashes** trước/sau agent khớp nhau và disk; toàn bộ tracked source khớp commit `dfca84a` sau chuẩn hóa LF/CRLF.
- Tám countercheck được tính có failure đúng guard, không setup error: runtime, final gate, lease clock, post-I/O cancel/binding, SHA-256, uncertain commit và item/job order. Lease-clock thất bại vì guard không còn ném `Halt`. Item/job countercheck mạnh thất bại ở assertion claim phải SKIP thay vì lock-error outcome; lượt yếu trước đó giữ riêng, không cộng.
- XML chuẩn bị `prep-worker-2.xml` đã che sáu lần credential sinh riêng. Hash bản gốc được giữ nhưng bytes gốc không còn; reviewer chỉ kiểm được artifact đã che và record redaction, không xác minh lại bản chưa che. Lần kết nối nhầm port 5432 trong chuẩn bị của agent bị từ chối xác thực trước SQL, theo evidence/handoff; không lặp lại trong lượt reviewer.

## Việc còn lại

1. **C2d-5d2b:** chuyển reservation/heartbeat/provider outcome/publish sang runtime checkpoint; hash toàn object trước mỗi provider call và ngay trước publish kể cả reuse; giữ budget/quota/uncertain accounting và không gọi lại chunk không đổi. Chưa nối lại indexer legacy trong extraction.
2. Bốn lỗi baseline PR #50 vẫn là debt tổng hồi quy trước C4: hai corruption fixture common-search thiếu quyền `session_replication_role`, readiness kỳ vọng 200 trong pending, và common-original fixture revoke 409. Reviewer PR #51 không chạy lại hay coi chúng đã sửa. Không nới gate/grants để né fixture.
3. Các background writer khác, search/RAG provider, private deletion/membership journal, operator/delivery/C3 và Compose/cutover/C4 còn riêng. Chưa đo quy mô 1.000 paper; `due_job_ids` và các gate/lock cần baseline trước chốt SLO.
4. Provider charge/routing, journal storage độc lập và độ bền mất điện vẫn chưa được xác minh. Không dùng kết luận này bật pricing hoặc safety.

## Evidence, gián đoạn và cleanup

Evidence reviewer giữ ngoài repo tại `%TEMP%\ras-pr51-review-20261009`: `reviewer-core.xml`, `reviewer-process-rerun.xml`, logs, source hashes, `reviewer-verification.json`, `agent-verification.json`, `resume.json`, `cleanup.json` và process facts. Evidence agent giữ nguyên tại `%TEMP%\ras-c2d5d2a-20261009-run`.

Lượt process đầu sau gián đoạn dừng **trước pytest**, vì ba service thử đã exited; log/result giữ riêng, không tính test fail/pass. Đã xác minh exact ID/label/image/mount rồi khởi động lại đúng ba service thử, không thao tác dev. Chín container dev hiện hành có ID khác snapshot trước gián đoạn; nguyên nhân ngoài lượt review chưa xác định. **Không tuyên bố dev giữ nguyên suốt toàn lượt.** Từ lúc tiếp tục tới cleanup, chín container không đổi ID/image; ngay trước/sau cleanup không đổi StartedAt.

Đã gỡ đúng **3 container/3 anonymous volume/network** reviewer sau kiểm ownership và sole volume consumer; worker/beat smoke được chính test gỡ trước đó. Xóa hai file chứa credential thử; `runtime.env` của smoke đã xóa. Quét các evidence còn giữ với credential sinh riêng: **0 hit**. Không đọc/sửa `.env`, query/migrate/restart dev, gọi provider/arXiv hoặc đổi pricing/gate production. Fault injection và process smoke không chứng minh độ bền khi mất điện.
