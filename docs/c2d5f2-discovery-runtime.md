# C2d-5f2 — discovery harvesting qua runtime

**Đã merge PR #56 `808337dc`, 2026-10-11:** tree khớp `d957872`, source `0046`. Kết quả dưới đây giữ source trước merge; chưa rerun tại merge. Bước tiếp theo là [C2d-6 search/query/rerank](tasks/m6c2b2c2d6-search-query-rerank-runtime.md), mới giao, chưa triển khai/kiểm. **Safety pending tới C4**; các trạng thái chờ review bên dưới là lịch sử.

Nhánh `feat/m6c2b2c2d5f2-discovery-runtime`, base PR #55 merge `e6b5991dd7ea9df6b8a5d9307f100ab0b87c7faa`, source **0045 → 0046**. PR chờ review, không merge. **Safety pending tới C4**. [Kế hoạch MVP](mvp-delivery-plan.md) · [Inventory](c2d5f2-test-inventory.json) · [Task](tasks/m6c2b2c2d5f2-discovery-runtime.md).

## Luồng và phạm vi

Direct `app.discovery.run_sync`, CLI `python -m app.cli.sync_arxiv` và Celery `ras.discovery.sync` dùng cùng boundary. Disabled trả số đếm 0 mà không query/HTTP. Runtime lấy từ `RAS_DB__*`; owner/recovery/login sai và thiếu bất kỳ grant nào bị từ chối trước DML/HTTP. Không privileged fallback, endpoint override hay production bypass. CLI chỉ có `--force`, bỏ lịch poll thành công; error backoff và Retry-After vẫn chặn.

Chỉ metadata/topic/license provenance/OAI tombstone và citation target được ghi. Không PDF, file/chunk/index/rights/evidence/provider. License gắn với version quan sát, không cấp quyền. Matcher `arxiv-exact-v2`, hidden tombstone/common revoked gate và trigger 0024 giữ nguyên. Metadata version thấp hơn không thay metadata version cao hơn, kể cả cùng datestamp; tombstone cùng/ngày mới hơn vẫn ẩn paper.

## Boundary, locks và outcome

Mỗi checkpoint có Session riêng: identity + từng grant + writer gate → rollback/close sạch; transaction ngắn mới khóa coordinator → source (khi có) → metadata/topic/license/tombstone → item common UUID order → citation exclusive → flush + deferred constraints → journal exclusive trực tiếp → scalar lease/cursor binding → PostgreSQL `clock_timestamp()` mới → commit → rollback/close sạch. Không lock/flush mới sau journal. Item xuất hiện muộn dùng NOWAIT; không chờ item sau citation. Hook không commit. Entry integrity/hold/journal và final gate thật; chỉ fixture test thay cutover readiness.

Nonce, topic, window from/until và input token ghim acceptance; cursor/counter/window/last_success và metadata/retarget của page cùng commit. `last_success_at` chỉ được ghi khi token hết. Token expired xóa token nhưng giữ nguyên inclusive window để quét lại idempotent. DTO `Cursor` không giữ ORM/Session. `now` caller giữ tương thích nhưng bị bỏ qua; nó không chứng minh lease/giờ retry.

Commit exception luôn `halted:discovery_commit_uncertain`, không SELECT đoán landed, không tự retry hoặc ghi error sau nó. Facts outcome độc lập teardown; lỗi rollback/close dừng, known commit + release lỗi có `committed_unreleased`, page đó không cộng số đếm. Error checkpoint chỉ chạy sau lỗi typed trước commit và release sạch, qua boundary mới. Unknown checkpoint fault dừng pass. Halt để lease tự hết hạn, không speculative cleanup; holder cũ không renew/write/release lease mới.

`pages`/`records` chỉ cộng page known commit và release sạch; records gồm metadata và tombstone quan sát, **không** là số paper mới. `requests` đếm lần gọi fetch đã thử, HTTP có thể lặp. Busy, bound, error và halted có mã riêng. CLI exit 0 completed/bound/disabled, 1 error/halted, 2 configuration, 3 busy.

## Lease, pacing và HTTP

`0046` thêm cặp nullable UUID `lease_token` / timestamptz `lease_until` trên coordinator singleton, không đổi grants. Provision hiện hành cấp operational DML; boundary kiểm riêng SELECT/INSERT/UPDATE trên các bảng discovery và quyền citation cần dùng. CHECK giữ cặp cùng NULL/non-NULL, upgrade không đổi dữ liệu cũ. Downgrade từ chối khi còn lease.

Lease toàn installation giữ tuần tự hóa giữa cả cùng/khác topic, nonce mới mỗi claim. Mặc định TTL 90 s, HTTP total deadline 30 s, margin 10 s; validation yêu cầu deadline + margin < TTL. Mỗi checkpoint chỉ renew lease còn hiệu lực. Trước fetch, admission phải known commit/release sạch và còn margin theo monotonic time. Không connection/transaction/row/advisory lock trong HTTP, parse, sleep. Chờ ngoài DB rồi reserve lại với clock PostgreSQL mới, không cấp slot tương lai cho nhiều waiter.

Reservation giữ quiet window tới lease deadline + interval (mặc định 3 s, config không dưới 3). Sau HTTP settle và page/error checkpoint, đặt interval từ clock mới; crash giữ window bảo thủ. Retry-After 429/503 (số giây/HTTP date, clamp mặc định 86.400 s) áp dụng toàn installation, topic khác/force không bypass. Exponential retry mỗi source 60…3.600 s, fresh transaction clock. Max pass mặc định 300 s và tối đa 2 page/topic; kiểm time bound trước mỗi reserve. Một pha đã được nhận có thể kết thúc sau time bound: HTTP có deadline, checkpoint hết lease bị từ chối; DB có timeout từng statement và số hàng hữu hạn, không có deadline toàn transaction. Đây không phải SLO wall-clock; page lớn có thể giữ khóa lâu rồi rollback nếu hết lease.

HTTP cố định `https://oaipmh.arxiv.org/oai`, no redirect; async client/stream được cancel và đóng trước khi timeout trả về, không detached thread. Per-operation timeout 20 s + total phase deadline 30 s, byte cap 4 MiB, record cap 5.000, token cap 4.096, defused XML. Yêu cầu identity encoding, từ chối encoding khác để tránh decompress vượt cap. OAI errors allowlist; status/log chỉ mã ổn định, không exception string/body/token/URL params. Không gọi URL license/original.

Giả định PostgreSQL clock và monotonic clock tiến bình thường, process/transport được scheduler cho chạy cancellation trong hạn. OS/VM pause dài hoặc clock nhảy là giới hạn vận hành; expiry ngăn holder cũ acceptance nhưng không thể thu hồi GET đã gửi. Không tuyên bố exactly-once HTTP, chịu mất điện hoặc SLO. Crash GET có thể lặp; atomic/idempotent page bảo vệ DB.

## Retarget cap

Probe tối đa cap + 1 ở hint và lại dưới citation exclusive, trước khi nạp/khóa toàn fan-out. Mặc định **2.000** nhằm giới hạn hàng nạp/khóa/UPDATE trong transaction; operator có thể tăng tới **50.000**. Đây là giá trị cần đo, không benchmark. Vượt cap rollback cả metadata/link/page/cursor, ghi `retarget_limit_exceeded` + backoff trong status Admin hiện có; không partial advance hoặc eventual-link pipeline. UI hiện có hiển thị `last_error`, không đổi schema API hoặc web.

## Kiểm chứng và giới hạn

Source kiểm cuối: **`7fc6b4e`** (production `0795d42`, commit cuối chỉ cập nhật assertion gate Celery trong test extraction); commit bàn giao sau chỉ đổi docs. File chính:

Seal **`sealed-3`: 389 unique pass = 387 API + 2 smoke Linux**, 0 fail/error/skip, exit 0. Collection **4.828**, **4.439 not-run**; selection/executed khớp identity, 335 source hash trước/sau trùng, **7/7 countercheck** và 388 artifact hash. Không cộng countercheck hoặc chuẩn bị vào unique pass.

| Nhóm | Pass |
| --- | ---: |
| Discovery/runtime/HTTP/retarget + parser/API cũ | 98 |
| Citation/runtime/worker hooks | 118 |
| Extraction private/common | 74 |
| Migration/ORM/config | 88 |
| Recovery liên quan | 4 |
| Cleanup liên quan | 5 |
| Linux CLI + Celery/Redis SIGKILL/resume | 2 |

Hai seal trước giữ lỗi lịch sử: `sealed-1` có 10 lỗi fixture/helper cũ, `sealed-2` có 3 assertion extraction còn kỳ vọng response Celery `skipped`; đã sửa fixture/assertion, không đổi kỳ vọng gate/quyền và không tính các lượt này thành pass cuối. Inventory giữ JUnit/hash và các lỗi chuẩn bị riêng.

| File/nhóm | Thay đổi |
| --- | --- |
| `api/app/discovery.py` | Runtime entry/checkpoint, lease/cursor DTO, pacing, HTTP deadline/cancel, retry và outcome |
| `api/app/cli/sync_arxiv.py`, `api/app/ingest/celery_app.py` | CLI mới và Celery cùng boundary |
| `api/app/models/discovery.py`, migration `0046` | Cặp nonce/thời hạn lease trên coordinator có sẵn |
| `api/app/citations.py`, `api/app/config.py` | Retarget probe/cap hai lần và config có validation |
| `api/tests/test_c2d5f2_*`, các fixture discovery/citation/cleanup | Runtime, fault/race, HTTP fixture, SIGKILL/resume, sửa chữ ký helper cũ |
| `scripts/c2d5f2_test_host.py`, `c2d5f2_counterchecks.py`, `c2d5f2_seal.py` | Môi trường cô lập, đối chứng, selection/JUnit/source manifests |
| Demo M5a/M5b cũ | Retire owner demo M5a; bỏ monkeypatch pacing không còn dùng ở M5b |

Lệnh đã thực chạy bằng Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe`: `scripts/c2d5f2_test_host.py --context <ctx> setup`, các lượt `test` ghi trong inventory/JUnit, `scripts/c2d5f2_counterchecks.py --context <ctx> --out <ctx>/counterchecks-sealed-3`, `scripts/c2d5f2_seal.py --context <ctx> --name sealed-3`. `<ctx>` là `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5f2-20261011`, ngoài repo. Image smoke là tag riêng từ image worker đã có, code được mount read-only vào `/srv/api/app`; image ID và hash source nằm trong inventory, không mô tả là build image production mới.

Kết quả seal/source/selection và lượt chuẩn bị giữ tại inventory; không cộng pass lặp. PostgreSQL/Redis/MinIO/journal và credentials task cô lập, Conda ras, outbound httpx guard và Linux socket guard; không đọc/sửa `.env`, không thao tác stack dev hoặc gọi arXiv/provider thật. Các docs review/task orchestrator được giữ chưa commit.

Đã dọn project `rasc2d5f2c4745795`: ba container nền, ba volume riêng, network và tag image smoke; container smoke đã được test teardown. Generated credentials/Compose đã xóa, giữ raw evidence/journal thử ngoài repo. Chín container baseline giữ nguyên ID/name/image, mọi volume baseline còn nguyên; không khẳng định thời điểm restart tự động của stack có sẵn. Chi tiết và hash cleanup ở inventory.

Hai fixture discovery debt dùng runtime bootstrap/invite/session hợp lệ; harvesting và API ACL thật được chạy dưới scoped cutover. Các debt khác giao gói B, không chứng nhận đã sửa. Trong đó test restore `test_restore_from_before_a_revocation_serves_and_processes_nothing_until_recover` chưa chạy, helper cũ còn stub `run_sync` và kỳ vọng response wrapper `skipped`; cần cập nhật khi xử lý fixture restore, không dùng lượt này chứng nhận restore. Web không đổi nên chưa chạy npm/browser. Live arXiv HTTP 406 chưa xác định nguyên nhân; fixture không là chứng cứ đồng bộ live. Search/query embedding/rerank là gói kế tiếp; RAG/generation, C3/C4, private deletion/restore, provider charge và triển khai vẫn riêng.
