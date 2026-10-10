# Review PR #52 — C2d-5d2b document embedding/index runtime

**Cập nhật sau review (2026-10-10):** chủ dự án đã merge; GitHub/Git xác minh merge `ba20fda402840243f13bd4bb27e98c4c0cb17562`, head `97bc6e7` là ancestor và tree khớp merge. Chưa rerun test tại merge; kết quả dưới đây giữ đúng source/lúc review. [Task tiếp theo: cleanup common revoked runtime](../tasks/m6c2b2c2d5e1-revoked-common-cleanup-runtime.md), safety pending tới C4.

Ngày **2026-10-10**. PR **OPEN, chưa merge** tại lúc review. Base `9fb8890568af85b7beea4503f93f12e2896e6c89` (merge PR #51); head `97bc6e7d179e2293755f57728e35c2a64212da80`; code/test sealed `43a656488d3a15a7d27d9c5bda5eb732f854fd3b`. Diff từ sealed tới head chỉ docs. Source migration **0043** sau 0042, không grants mới; không truy vấn revision DB dev.

## Kết luận

**Không phát hiện điểm chặn merge mới trong phạm vi C2d-5d2b. Có thể merge PR #52 theo phạm vi private/common document embedding/index runtime.** Reviewer thực chạy **254/254 API unique**, 0 fail/error/skip. **Safety pending tới C4**; kết luận không mở pricing, cutover hoặc chứng nhận các consumer/background task còn lại.

Reviewer chỉ cập nhật tài liệu; không sửa production/test, commit/push/merge hoặc đăng comment PR. [Task](../tasks/m6c2b2c2d5d2b-document-index-runtime.md) · [Handoff](../c2d5d2b-document-index-runtime.md) · [Inventory agent](../c2d5d2b-test-inventory.json).

## Đối chiếu code và hợp đồng

- [Document boundary](../../api/app/ingest/document_index.py) tách predicate index khỏi extraction, kiểm actual runtime, private owner active/latest receipt và consumed consent đúng file, hoặc common publication/rights/version/checksum/epoch/scope. Claim/lease, cancel và clock được kiểm lại ở checkpoint; logout của Admin tạo job không hủy quyền index common.
- Content khóa owner User khi private → item → job/resources → capacity/generation/entries/budget/call cần thiết → flush/constraints → journal exclusive trực tiếp → binding/trạng thái/clock mới. Checkpoint publish kiểm hậu mutation `ready/indexed/active`, không dùng predicate extraction. Session được release trước storage/hash/provider/broker; proof và payload detached, gồm JSON/vector bất biến.
- [Reservation/accounting](../../api/app/ingest/document_accounting.py) bind nonce + job/token/file/model/pipeline/day/ceiling. Rejected call được dùng lại với nonce mới; heartbeat/settlement cũ không sửa lần mới. Maintenance có transaction runtime/gate riêng, capacity → call → budget ngày gốc → journal; không lấy item/job/User lock hoặc ghi vector/content, tránh vòng khóa với revoke/cleanup.
- Slot tính document và query reservation còn lease sống; reservation hết hạn hoặc thiếu deadline không chiếm slot document, nhưng ceiling tiếp tục tính vào budget/quota. Reaper batch hữu hạn; heartbeat không hồi sinh deadline đã hết. Compatibility bridge nhả transaction capacity cũ trước chuyển document expiry sang runtime. Query/rerank/RAG boundary chưa được chứng nhận đã chuyển.
- Settlement tài chính diễn ra trước content proof. Response đã gửi có thể settle một lần sau revoke/cancel/disable/lease mất/cleanup, vào ngày gốc; không resurrect hash/vector/chunk/index. Mất phản hồi giữa settlement và vector commit có thể để completed call không vector; retry dừng `embedding_content_unavailable`, không tự embed lại để đoán chi phí.
- Hash toàn original ngoài DB trước mỗi provider call và sau entry cuối trước publish, kể cả reuse completed. HEAD chỉ từ chối sớm. Publish kiểm đủ keyword text/hash và vector/model/provider/dimensions; generation/pointers/item/job/attempt cùng transaction. Khe overwrite sau hash tới provider/commit vẫn tồn tại như handoff nêu.
- Commit exception dừng attempt với outcome uncertain; không SELECT suy landed, chạy failure writer tiếp, giải tiền, retry provider hoặc xóa original. Known commit rồi release lỗi vẫn giữ mutation và dừng. Heartbeat thread thuộc attempt tới lúc join; lỗi background không in traceback/DSN/content.
- [Migration 0043](../../api/migrations/versions/0043_document_reservation_attempt.py) thêm ba cột nonce/job binding, CHECK toàn NULL hoặc toàn non-NULL, FK job `RESTRICT`. Historical call không nonce chỉ được expiry, không adopt/renew/settle. Downgrade bị chặn khi có nonce; ORM/schema được chạy lại.

### Sửa nhận định trong prompt của orchestrator

Tại base `9fb8890`, `worker._locked` **vẫn tồn tại dưới dạng alias `checkpoints.leased`**. Nhận định trước đây rằng helper đã bị gỡ và import sẽ lỗi là sai. Reviewer kiểm trực tiếp Git blob; agent cũng giữ base import reproducer trong inventory. Vấn đề thật là predicate extraction của alias không nhận index state; PR bỏ phụ thuộc này và tạo document boundary riêng. Task và decisions được sửa mô tả, giữ rõ nguồn đính chính; không coi đây là bug import do PR đã sửa.

## Reviewer thực chạy

Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe` 3.12.14, pip 26.2.1. PostgreSQL/MinIO/Redis/journal và credentials riêng ngoài repo; settings trước import, không `.env` fallback hoặc provider key. Egress ngoài bị chặn; business fixture patch cutover trong test, các bài pending/hold giữ gate thật.

| Kiểm chứng độc lập | Kết quả |
| --- | ---: |
| Document runtime/common/schema mới và toàn `test_migrations.py` | 161/161 pass |
| Worker + beat Linux thực, Redis outage/retry/restart | 1/1 pass |
| Private/common index, search/shared capacity, adapter | 92/92 pass |
| **Tổng API unique** | **254/254 pass**, 0 fail/error/skip |
| SHA-256 source/config/test/script trước/sau | 302 file khớp |

Core chạy 500,56 s; shared index/search 513,33 s; process smoke 41,08 s. JUnit 254 node không trùng và thuộc collect của agent; theo collect 4.477, **4.223 node không được reviewer chạy lại**. Đây là thời gian chạy kiểm thử, không latency/SLO. Không chạy web/Chrome vì PR không đổi web; không full business/cutover/provider live/benchmark. Shared tests và process smoke dùng DB/bucket/journal riêng, chạy đồng thời sau core; không sửa source/mutant trong các lượt chạy.

Process smoke dùng dependency image có sẵn `sha256:2ebafdf61a3fdcff8f712bce8309355a870182bca59995b9e78c3014d6b4e674`, tag thử riêng và source mount read-only. Không clean-build image phát hành hoặc restart image dev. Chính connection worker/beat chứng minh `c1_runtime`, không raw User write/TEMP. Ba job private Markdown, private PDF và common promotion tới ready/indexed; **7 fake provider calls**, gồm một 429 sau completed chunk, restart giữ completed nonce và không tăng call/committed cost sau restart cuối. Redis stop/start thật, broker payload UUID-only. Sitecustomize chỉ thuộc harness test để giới hạn schedule/egress và scoped cutover/fake transport; không thêm bypass production.

## Artifact agent, kiểm riêng

- **403 artifact** khớp SHA-256 manifest. Chín nhóm selection/JUnit khớp **703 API unique pass**, 0 failure/error/skip; collect 4.477, **3.774 not-run**. Không gọi đây là reviewer chạy lại 703 ca.
- **302 source hashes** trước/sau agent khớp nhau và disk; toàn bộ khớp commit `43a6564` sau chuẩn hóa LF/CRLF.
- **8 countercheck cuối** đúng code SHA: role, consent, owner status, nonce, final hash, clock, uncertain commit và query lease. Mỗi XML có assertion failure, không setup error. 14 lượt chuẩn bị và một lượt yếu giữ riêng; không tính 23 raw thành 23 cơ chế.
- Seal thứ hai dừng ở cleanup lỗi expiry lịch sử; seal thứ ba xanh 699 nhưng chưa bao phủ query expired. Inventory giữ hai lượt superseded, bốn ca tái hiện query lease và seal cuối 703. Không cộng kết quả các lượt này.

## Phạm vi chưa được chứng nhận

1. Query embedding/rerank/search và RAG/generation runtime, background discovery/citation/cleanup/reconcile còn lại, C3/operator/delivery và C4/default Compose/API cutover có task riêng. Kiểm tương thích consumer cũ không chứng nhận đã chuyển chúng.
2. Bốn baseline debt PR #50 vẫn riêng: hai corruption fixture common-search dùng quyền `session_replication_role`, readiness kỳ vọng 200 trong pending, và common-original fixture revoke 409. Reviewer không chạy lại các node đó hoặc ghi đã sửa; phải giải quyết trước tổng hồi quy/C4.
3. Settlement → vector gap và hash → provider/commit gap nêu trên vẫn tồn tại. Chưa thử SIGKILL/mất điện hoặc network blackhole trong heartbeat DB; join thread không chứng minh recovery có thời hạn dưới mọi lỗi thiết bị/mạng.
4. Provider charge/routing/retention, journal storage độc lập, workload 1.000 paper, cleanup 100 trang và p95 chưa được đo. Kế toán common owner NULL vẫn là phương án orchestrator; không biến thành xác nhận trực tiếp mới của chủ dự án. Giữ pricing false/safety pending.

## Evidence và cleanup

Evidence reviewer: `%TEMP%\ras-pr52-review-20261010`, gồm JUnit/log/result từng nhóm, 302 source hashes, `reviewer-verification.json`, `agent-verification.json`, `quality.json` và `cleanup.json`. Evidence agent giữ nguyên tại `%TEMP%\ras-c2d5d2b-20261010`.

Đã gỡ đúng **3 container/6 volume/1 network** reviewer sau kiểm exact ID/label/mount và sole volume consumer, cùng tag image thử do reviewer tạo; worker/beat smoke được chính test gỡ trước đó. Xóa hai file chứa credential thử; `runtime.env` smoke đã được test xóa. Quét evidence còn giữ với credential sinh riêng ở UTF-8/UTF-16 LE/BE: **0 hit**.

Chín container có sẵn giữ nguyên ID/image/name trong snapshot trước/sau, không thiếu container baseline. **`StartedAt` của `ras-worker-1` và `ras-beat-1` có drift**; không tuyên bố các container này giữ nguyên thời điểm khởi động suốt lượt review. Không gọi stop/start/restart/query/migrate dev. Chỉ thao tác Docker trên project thử/tag image đã xác minh. Không đọc/sửa `.env`, gọi provider/arXiv hoặc đổi pricing/cutover. Fault injection/process smoke chưa chứng minh độ bền thiết bị hoặc mất điện.
