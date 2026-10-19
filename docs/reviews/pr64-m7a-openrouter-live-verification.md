# Review PR #64 — OpenRouter live và chi phí

Ngày **2026-10-19**, PR **OPEN/chưa merge**, base **`5362ace04d0d211005753aecddd760a4ad7d548b`**, head **`5dc8947051c4d5e3e320118608d0c56ce9ef1ec8`**, code **`7357216`**, source **0052**. **Có ba P2 cần sửa trên cùng PR trước merge**, đều ở tooling experiment/cap/polling. Không cần chạy lại selection 652 hoặc monitor đã hết hạn để xác định các lỗi này.

[Handoff](../m7a-openrouter-live-verification.md) · [Inventory agent](../m7a-test-inventory.json) · [Prompt sửa hẹp](../tasks/fix-pr64-m7a-experiment-cap-and-polling.md)

## P2-1 — runtime hoàn slot chưa có chứng cứ không gửi, lock không phủ action

Vị trí: [`scripts/m7a_runtime_drill.py`](../../scripts/m7a_runtime_drill.py), `Drill.gated()` dòng 148–177; [`m7a_observer.py`](../../scripts/m7a_observer.py), `_take_allowance()`/`observed()`.

`gated()` chỉ giữ `ledger.exclusive()` trong pha admit. Nó nhả khóa trước khi đặt allowance/chạy action. Cuối action nó ghi allowance=0 **không dùng cùng flock với consumer**, ngủ một giây, đọc receipt rồi đánh dấu `not_sent` khi không thấy marker. Tuy nhiên consumer có thể đã lấy permit và còn bị dừng trước khi ghi `sending`; đóng allowance không thu hồi được permit đó. Action HTTP/client kết thúc không chứng minh handler/thread đã kết thúc.

**Tái hiện không mạng trên Linux, không đổi production source:** container `rasm7a-api:local --network none`, chạy chính `Drill.gated` và observer của PR, transport thật được thay bằng callback trả HTTP tổng hợp, không key/DB/provider. Barrier chỉ dừng observer sau khi đã consume allowance và ngay trước marker `sending`.

1. Seed 19 call tổng hợp đã completed ($0,001/call), rồi admit call thứ 20.
2. Handler thứ 20 đã lấy permit nhưng chưa ghi marker; action bên client trả về. `gated()` đóng allowance, chờ một giây rồi ghi `not_sent`. Ledger báo **19 request**, không còn open slot.
3. Một action mới được admit là slot thứ 21 và delegate HTTP giả; sau đó thả handler cũ để nó cũng delegate. Có **19 trước + 2 mới = 21 inference**, trong khi ledger chỉ báo **20**. Ca kiểm “request thứ 21 phải bị từ chối” fail.
4. Ca độc lập thứ hai giữ action đầu đang chạy rồi gọi `gated()` lần nữa. Action thứ hai vẫn bắt đầu; assertion concurrency 1 fail. Hai action còn dùng chung allowance và receipt cursor, nên đây không chỉ là tên khóa không đẹp.

Đây là cửa sổ race có thật ở ranh giới permit/marker, không là provider giả tự vượt qua guard. Linux `fcntl` thật được dùng; không tăng timeout hay sửa giá trị cap để tái hiện. Điều này **không chứng minh lượt live lịch sử đã vượt cap**: artifact lịch sử hiện có vẫn là 19 request.

**Cần sửa:** serialize toàn vòng đời action/outcome của một experiment và giữ quyền sở hữu permit đã consume cho tới khi có chứng cứ outcome. Đóng/kiểm allowance phải cùng cơ chế atomic/flock với consumer; consumed nhưng chưa có receipt là uncertain, giữ đầy đủ slot/trần. Không suy `not_sent` từ im lặng một giây hoặc action đã return. Correlate batch/permit/receipt để response muộn không bị gán sang action khác. Không giữ DB lock qua provider.

## P2-2 — receipt cost âm làm giảm committed budget

Vị trí: [`scripts/m7a_live.py`](../../scripts/m7a_live.py), `probe()` và `Ledger.state()` dòng 100–114; runtime `gated()` cũng chỉ kiểm `isinstance(cost, (int, float))`.

Provider cost không được kiểm hữu hạn/không âm/không boolean. Với HTTP 200 có đúng identity Azure, vector hợp lệ và `usage.cost=-1`, production embedding adapter vẫn trả result (adapter không dùng cost), probe ghi `completed`, rồi `Ledger.state()` tính **`committed_usd = -1`**. Request sau có thể được admit với tổng chi phí thấp giả tạo. Tái hiện bằng MockTransport, không network/key thật; expected giữ ít nhất ceiling **0,0016384** nhưng nhận **-1**. Control `cost=0,001` pass và ghi đúng 0,001.

**Cần sửa:** validate cost ở producer và lúc đọc ledger, có cùng hợp đồng giữa probe/runtime. Chỉ dùng cost có kiểu hợp lệ, hữu hạn, không âm và không boolean để release trần; invalid/missing giữ ceiling hoặc dừng fail closed trước call tiếp. Không clip negative thành 0. Dòng ledger cũ có invalid cost cũng không được làm giảm tổng tiền sau resume. Giữ zero-cost hợp lệ và không đổi estimate thành actual bằng nhãn.

## P2-3 — polling không thi hành deadline trên cả batch

Vị trí: [`scripts/m7a_live.py`](../../scripts/m7a_live.py), `reconcile()` dòng 322–342.

Đồng hồ chỉ được kiểm ở đầu `while`; bên trong `for gid` có thể chạy mọi GET với timeout 30 s mà không kiểm thời gian còn lại. `sleep` không bị cắt theo remaining, và cuối cùng `/key` luôn được gọi sau deadline.

Ca độc lập dùng đồng hồ giả và `_get` tổng hợp (không thật sự chờ): 19 ID chưa có thống kê, mỗi GET dùng 30 s được cấp, deadline 300 s. Code vẫn bắt đầu GET sau deadline, kể cả `/key` ở mốc 572 s; tổng thời gian mô phỏng **602 s**. Assertion ≤300 fail. Control 19 ID trả ngay hoàn tất 20 GET/0,2 s, pass. Đây là kết quả fake-clock, không phải đo provider thực hay lỗi monitor selection trước đó.

**Cần sửa:** kiểm remaining trước từng GET và sleep, cấp timeout theo remaining, không bắt đầu `/key` hoặc vòng mới khi đã hết thời gian. Trả báo cáo pending/timeout rõ, giữ ID đã tìm được; không gọi thêm inference hoặc chờ vô hạn để làm kết quả hết pending.

## Reviewer thực chạy và đối chiếu chứng cứ

| Nhóm reviewer | Kết quả |
| --- | --- |
| 26 ca M7a + năm embedding transport trong repo | **31/31 pass**, exit 0, 0,82 s; HTTP ngoài bị chặn, không DB/key thật |
| Linux runtime gate/observer ngoài repo | **2 failure**, đúng P2-1, 4,89 s; không mạng |
| Receipt cost ngoài repo | **1 control pass / 1 failure**, đúng P2-2; MockTransport |
| Polling ngoài repo | **1 control pass / 1 failure**, đúng P2-3; fake-clock, không thật sự chờ |

Tổng **31 repo + 2 control ngoài repo pass**, **4 ca tái hiện fail độc lập**; không cộng các lượt lặp vào số test mới. Reviewer không sửa production/test/migration/grants, không đọc key/`.env`, không gửi inference. Một lượt capture stderr cost bằng PowerShell bị format lại được giữ riêng; raw capture cuối dùng subprocess. Các repro là fault scheduling/input tổng hợp, không mô phỏng mất điện thành công.

**Artifact agent đã audit:** 131 hash hiện hữu/khớp; 452 source hash trước/sau trùng và khớp head. Từng trạng thái trong 20 nhóm JUnit khớp **652 pass**, collection **5.324**, **4.672 not-run**, không thiếu/trùng; reviewer không chạy lại selection này. Monitor hết hạn không thay kết quả JUnit đã hoàn tất.

19 ID thực gửi đều có `usage.cost` khớp `/generation total_cost`; tổng Decimal **$0,01941006**, khớp inventory và `usage_daily` đã cập nhật; một slot `not_sent` được giữ riêng. Không tìm ra discrepancy lịch sử trong các số này. Audit này không chứng nhận guard an toàn cho các cửa sổ lỗi vừa tái hiện.

## Các giới hạn đang được ghi đúng, không nâng thành P2 mới

Identity generation snapshot hữu hạn và pin provider được kiểm bằng unit; `pricing_verified` vẫn false. Reservation và settlement mới dùng max(input, cache-write) 2,5/M, estimate không thấp hơn sáu charge đã quan sát; đây vẫn là estimate. Image live build trước sửa ba file cache-write, nên không có live confirmation source cuối. Handoff nêu rõ chưa đủ điều kiện paper thật; không nên đòi chạy lại 19 live call chỉ để sửa tooling.

[OpenRouter routing docs](https://openrouter.ai/docs/guides/routing/provider-selection) mô tả price preferences; giá cache-write quan sát cần được giữ tách khỏi ordinary prompt ceiling, không suy mọi SKU tương lai đều được server bound bởi `max_price.prompt`. Privacy/ZDR negative-live, điều khoản provider/account, common live, M7b human review, production journal/cutover vẫn chưa được xác minh thêm trong lượt này. Không chạy web/build/Chrome/restore vì finding nằm ở scripts/offline cap/polling.

## Evidence, tài nguyên và bàn giao

Evidence ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr64-review-20261019`: `offline.xml/log`, ba test ngoài repo, `runtime-gate.log`, `cost-receipt.log`, `poll-deadline.log`, exit JSON, artifact audit, source hashes/baseline/cleanup. Container repro chỉ mount scripts/api và context thử, không mount `.env`; `--network none --rm`, không tạo volume/DB/secret context. Không còn container của reviewer; giữ image agent.

**24 container có sẵn giữ nguyên ID/name/image/mount/label**. Beat/worker dev đã restarting ở baseline và cùng RestartCount 17→24; StartedAt đổi, không suy nguyên nhân hoặc sửa. Đối chiếu mount ban đầu báo một drift giả do thứ tự mảng; bản so sánh tập mount xác nhận không đổi, cả hai output được giữ. **453 tracked source hashes** reviewer trước/sau trùng. Dify không bị thao tác; không có live request/charge mới hoặc thay pricing.

Giữ report/prompt cùng cập nhật trạng thái **chưa commit**, đưa vào cùng PR #64 sửa. Reviewer không merge hoặc gửi comment GitHub.
