# Prompt C2d-5f2: hoàn tất discovery harvesting qua runtime

## Nhiệm vụ

Bạn là coding agent chính. Hoàn thành **một PR** cho toàn luồng discovery: khảo sát → runtime boundary → HTTP/pacing/lease → checkpoint metadata/cursor → retry/resume → kiểm thử → docs → commit/push và mở PR **tiếng Việt**; **không merge**. Không dừng ở một PR kế hoạch mới. Chủ dự án đã giao orchestrator chọn cách làm hợp lý; các lựa chọn scoped dưới đây đã chốt cho task này.

Đọc [kế hoạch MVP](../mvp-delivery-plan.md). Mục tiêu là hoàn tất luồng có thể nghiệm thu; không mở thêm một loạt task theo từng helper/endpoint. Chỉ chia PR nếu phát hiện phụ thuộc thật khiến không thể review một PR, và báo rõ phần đang thiếu.

## Base, tài liệu và môi trường

- PR #55 đã merge tại **`e6b5991dd7ea9df6b8a5d9307f100ab0b87c7faa`**, tree khớp head `bca5d90`; source migration **0045**. Fetch và xác minh lại head/ancestor, tạo nhánh `feat/m6c2b2c2d5f2-discovery-runtime` từ `origin/main`; không suy revision DB dev.
- Giữ các docs review/task chưa commit, gồm [review PR #55](../reviews/pr55-c2d5f1-citation-reconcile-runtime.md), kế hoạch MVP và task này; có thể commit riêng. Không reset/clean/ghi đè thay đổi của người khác.
- Đọc [AGENTS](../../AGENTS.md), [mục lục](../README.md), [yêu cầu](../product-requirements.md), [quyền/dữ liệu](../access-and-data.md), [decisions](../decisions.md), [citation handoff](../c2d5f1-citation-reconcile-runtime.md), [worker handoff](../c2d5d2a-worker-runtime-extraction.md) và [auth safety](../auth-restore-safety.md).
- Chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước chạy. Dùng PG/Redis/MinIO/journal và credentials thử cô lập, login runtime thật. Không đọc/sửa `.env` hoặc key, query/migrate/restart dev, gọi arXiv/OpenRouter thật hay đổi pricing. Chụp baseline tài nguyên, chỉ dọn tài nguyên task đã tạo.
- **Safety vẫn pending tới C4.** Kiểm trực tiếp runtime với fixture cutover scoped theo hợp đồng hiện hành; integrity/hold/journal/predicate thật. Có kiểm pending production riêng, không thêm bypass production hoặc đổi test nghiệp vụ thành kỳ vọng 503.

## Khảo sát đã có — kiểm lại bằng source

`app/discovery.py::run_sync` hiện giữ connection cho session advisory lock xuyên pass; `_reserve_request` sleep trong transaction; `_ensure_sources` tự commit. HTTP có per-operation timeout, chưa có deadline toàn pha; checkpoint dùng mốc `now` đầu pass. `run_sync` chưa có runtime boundary/lease riêng; Celery chỉ kiểm gate đầu task. Hiện **chưa có CLI sync arXiv**.

`_write_page` upsert metadata/topic/license/tombstone rồi gọi `citations.reconcile_arxiv_ids` trong cùng transaction; page/cursor/counter commit nguyên tử. Hook PR #55 đã sửa deadlock: item common trước citation exclusive, item xuất hiện muộn dùng NOWAIT và `RetargetBusy`; fan-out vẫn chưa có trần. Giữ bảo đảm reference và metadata tới theo cả hai thứ tự không mất link.

Đối chiếu engine/role, entrypoints, grants thật và mọi caller của các helper trước sửa. `has_table_privilege` với danh sách quyền có nghĩa OR: kiểm từng quyền cần thiết, không coi bất kỳ một quyền là đủ.

## Thiết kế scoped đã chọn

### 1. Một boundary và pass hữu hạn

- Direct `run_sync`, Celery và **CLI one-shot mỏng mới** dùng cùng boundary runtime. `enabled=false` không gọi mạng/ghi DB; owner/recovery/role không hợp lệ bị từ chối trước HTTP và DML. Không fallback privileged engine.
- Mỗi checkpoint có Session ngắn: entry identity/grants/gate, DML/constraint flush, journal lock đúng thứ tự, lease/cursor binding và clock mới trước commit. Không nâng journal shared lên exclusive. Không dùng `now` do caller cung cấp làm bằng chứng thời gian thật.
- Giới hạn pass theo config đã có; bổ sung trần pha cần thiết ở config có validation/test. Không queue framework mới, không HTTP mutation route mới, không event journal cho từng metadata công khai.
- CLI không nhận arbitrary endpoint/bucket/key; `--force` nếu giữ chỉ bỏ lịch poll, không vượt gate, lease, pacing, retry/backoff hoặc Retry-After. Báo rõ busy/bound/halted/error; chỉ cộng kết quả đã known commit và release sạch.

### 2. Durable lease và pacing, không giữ DB xuyên I/O

- Thay session advisory connection giữ dài bằng **lease bền trong PostgreSQL**, token/nonce mới mỗi claim và thời hạn hữu hạn. Có thể mở rộng coordinator hiện có hoặc thêm bảng nhỏ; không xây scheduler tổng quát. Migration mới sau head thực tế, giữ revision đã merge; ORM/schema match.
- Lease/cursor snapshot trả ra là DTO tách Session. Không giữ connection, transaction hay row/advisory lock khi HTTP, parse, sleep hoặc chờ thread. Nếu dùng thread, attempt sở hữu tài nguyên tới khi thread settle; không giải phóng lease rồi tiếp tục ghi bằng holder cũ.
- Phối hợp request start theo clock PostgreSQL toàn installation, giữ tối thiểu cấu hình hiện hành (không dưới 3 giây). Chờ ngoài DB rồi kiểm/reserve lại bằng clock mới; không chỉ đặt lịch tương lai khiến các tiến trình thức trễ bắt đầu cùng lúc.
- Deadline HTTP toàn pha hữu hạn, đóng/cancel transport khi hết hạn; lease/margin hợp lệ so với deadline. Holder cũ hết lease không checkpoint/gia hạn/thả lease của holder mới. Có chính sách cho process chết và retry, ghi rõ giả định clock/timeout và không tuyên bố exactly-once HTTP.
- Gate đóng, lost lease hoặc release lỗi: không gọi request kế tiếp. Không giữ User/resource lock khi gọi mạng. Trước HTTP phải có proof/known commit/release sạch; sau HTTP dùng transaction mới kiểm lại trước nhận page.

### 3. Page/cursor nguyên tử và fan-out có trần

- Metadata, license provenance, source tombstone, citation retarget, cursor/window/counter của **một page cùng transaction**. Exact binding gồm topic, window, token đầu vào và lease nonce; worker cũ không ghi đè checkpoint mới. Không công bố `last_success_at` trước khi cửa sổ kết thúc.
- Token hết hạn quét lại cùng cửa sổ inclusive; upsert/idempotency không nhân bản paper/link. Retry dùng clock mới, không cố định thời gian đầu pass. Tombstone/metadata cũ hơn không hồi sinh trạng thái mới hơn.
- **Chọn hard cap retarget cho MVP, giữ atomic page.** Probe tối đa `cap + 1` ở hint và dưới citation exclusive; giới hạn trước khi nạp/khóa toàn fan-out. Khi vượt trần: rollback page, giữ cursor/counter, lưu mã `retarget_limit_exceeded` với backoff và hiển thị được trong trạng thái Admin. Không cắt âm thầm rồi advance cursor, không thêm outbox/eventual-link pipeline trong PR này. Trần mặc định phải có lý do và ghi là giá trị cần đo; người vận hành có thể tăng trong giới hạn cấu hình, page lớn vẫn là giới hạn được báo rõ.
- Giữ thứ tự **item → reference nếu cần → citation → journal**, UUID order và NOWAIT cho item muộn; không chờ item sau citation lock. Giữ filter common revoked/tombstone và derived fence. Không đổi matcher `arxiv-exact-v2` hoặc loại bỏ serialization để né race.

### 4. HTTP, lỗi và outcome

- Giữ endpoint OAI cố định, no redirect, defused XML và size cap; bổ sung giới hạn record/token nếu cần. Không tải PDF, không follow license/original URL, không tạo file/chunk/index/rights/evidence. License URL theo version quan sát là provenance, không cấp quyền.
- 429/503 xử lý Retry-After (giây/ngày HTTP) với giới hạn cấu hình; transport/timeout/malformed/406 có mã ổn định và exponential backoff hữu hạn. Error code OAI phải allowlist; không lưu `str(exception)`, response body, token hay URL chứa params vào log/status. Không “sửa 406” bằng tắt pacing, đổi host tùy ý hoặc retry liên tục.
- **Commit exception luôn uncertain:** không SELECT để đoán thành công, không tự gửi lại mutation; dừng pass. Outcome giữ độc lập rollback/close; release lỗi cũng dừng. Chỉ ghi error checkpoint mới khi chứng minh transaction trước chưa commit và session đã release; checkpoint lỗi cũng phải qua boundary hiện hành.
- HTTP GET có thể được lặp sau crash/lease expiry; idempotent page commit bảo vệ DB. Không đồng nhất request gửi ra, records quan sát và paper mới; report tách các số này nếu cung cấp.

## Kiểm chứng hữu hạn

Không yêu cầu chạy toàn bộ hơn 4.700 node ở PR này. Chọn nhóm bao phủ các rủi ro dưới đây, giữ selection/JUnit/commit/source hash; không sửa source trong lúc suite cuối chạy. Những ca không chạy không được tính pass.

| Nhóm | Chứng cứ bắt buộc |
| --- | --- |
| Runtime/gate | Direct, CLI, Celery qua login runtime thật; owner/recovery/missing từng grant bị chặn trước HTTP/DML; pending/hold/journal lỗi ở đầu và giữa pass; no production bypass |
| Connection/I/O | Chặn HTTP, body chậm và sleep có kiểm soát; pool không còn connection của discovery, revoke/extraction khác vẫn commit; cancel/timeout trả tài nguyên và không ghi muộn |
| Lease/pacing | Hai worker/process, cả cùng/khác topic; khoảng cách request start; holder chết/hết hạn và holder cũ quay lại; bound pass, busy và lock timeout có mã rõ |
| Cursor/retry | Hai page, empty/noRecordsMatch, expired token, tombstone/metadata cũ, license version, 406/429/503/Retry-After, malformed/oversize/deadline; retry/restart không nhân bản hoặc skip page |
| Atomicity/outcome | Error trước commit, landed/not-landed uncertain, rollback/close lỗi; không advance cursor/counter hoặc gọi mạng tiếp dựa trên outcome đoán; lỗi ở retarget rollback cả page |
| Citation concurrency | Metadata/reference tới cả hai thứ tự; extraction/discovery/revoke xen nhau không deadlock; cap đúng biên và vượt trần kể cả candidate xuất hiện muộn, không partial link hoặc mất cursor |
| Schema/hồi quy | Upgrade có dữ liệu nếu thêm revision, ORM/schema, config, discovery parser/API, citation, worker hook, recovery liên quan. Sửa **hai fixture discovery debt** đã ghi ở PR #55 để thực chạy đường harvest thay vì fail ở legacy bootstrap |

Smoke Linux/container với runtime thật và server HTTP fixture cục bộ: chạy one-shot rồi Celery qua Redis, SIGKILL sau một page commit hoặc giữa fetch, khởi động lại chứng minh cursor resume/idempotency. Không dùng mạng arXiv thật. Một smoke hoàn chỉnh có thể dùng chung kịch bản cho cả hai entry; không nhân nhiều harness chỉ để tăng số pass. Nếu UI/status thay đổi, kiểm web liên quan và một smoke browser; nếu không đổi web, ghi rõ chưa chạy.

Chọn vài countercheck có sức phân biệt cho identity/gate, lost-lease binding, I/O/lock, cursor atomicity và cap; khôi phục bytes trước seal. Không tối ưu số mutant hoặc tổng số test. Debt còn lại giao gói B trong [kế hoạch MVP](../mvp-delivery-plan.md), không tự coi baseline fail là đã giải quyết.

## Bàn giao và tiêu chí xong PR

1. Direct/CLI/Celery cùng chạy trọn luồng runtime, checkpoint bền và HTTP ngoài DB; tests/smoke trên đạt. Không chỉ sửa wrapper `_gate` hoặc chạy API trực tiếp rồi gọi web đã đạt.
2. Một handoff và inventory mới: file đổi, revision/grants, entry/lock/outcome/cursor, lệnh đã chạy, source được kiểm, pass/fail/not-run, debt và giới hạn. Giữ các lượt lỗi chuẩn bị riêng, không cộng pass lặp thành unique.
3. Cập nhật **kế hoạch MVP**, docs hợp đồng thật sự bị ảnh hưởng, roadmap/index và PR #55 merged. Không chép toàn report lịch sử vào đầu hàng chục docs; không thêm lệnh “đã kiểm” chưa chạy.
4. Không báo live arXiv đã hết 406/đồng bộ xong bằng fixture. Live smoke nhỏ, theo chính sách nguồn hiện hành, sẽ ở bước nghiệm thu network sau; không vì thiếu live mà tách phần runtime ra một chuỗi PR mới.
5. Commit/push và mở PR tiếng Việt, không trailer hay dòng quảng bá AI. Báo giới hạn, tài nguyên đã dọn và phần tiếp theo **search/query embedding/rerank** trong một gói hoàn chỉnh. Safety/pricing/dev vẫn giữ nguyên.
