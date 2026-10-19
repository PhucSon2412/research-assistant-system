# Review PR #64 — OpenRouter live và chi phí

## Review lại tại `9b64901` — HTTP đã hủy đúng, còn P2-3 ở thoát tiến trình khi DNS treo

Ngày **2026-10-19**, head **`9b649015395330ca96e580dce1e2df9aa544f6f3`**, code **`959441c`**, source **0052**, PR **OPEN/chưa merge**. Body/header nhỏ giọt và `/key` chậm đã qua kiểm. **Còn một nhánh P2-3: deadline của hàm không bảo đảm lệnh kết thúc khi DNS bị kẹt trong executor.** Giới hạn được agent ghi là “chưa tái hiện” nay đã tái hiện độc lập; cần sửa cùng PR theo điều kiện không chờ thread treo lúc thoát đã nêu trong prompt trước. P2-1/P2-2 giữ kết luận đã sửa, không mở lại scope.

### P2-3 — `loop.close()` không bỏ được bước join executor lúc Python thoát

Vị trí: [`scripts/m7a_live.py:224`](../../scripts/m7a_live.py), `_get` tạo event loop, `wait_for` rồi `loop.close()` ở dòng 231.

`asyncio.wait_for` hủy coroutine HTTP nhưng không dừng được `socket.getaddrinfo` đã chạy trong thread của default executor. `loop.close()` trả về, song interpreter vẫn join thread executor ở shutdown ([tài liệu Python 3.12](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.ThreadPoolExecutor)). Vì vậy `reconcile` có thể in kết quả pending rồi lệnh CLI vẫn không thoát; comment “never waits on executor threads” chỉ đúng tại lệnh `loop.close`, không chứng minh process đã kết thúc trong deadline.

**Tái hiện độc lập có giới hạn:** container Linux `--network none --rm`, một child Python dùng chính `reconcile` của source này. Chỉ thay `socket.getaddrinfo` bằng fault injection chờ event vô hạn; không DNS/HTTP ngoài, không key thật. Deadline 1 s: `reconcile` trả pending sau **1,015 s**, lượt xác nhận **1,023 s**, nhưng child không exit sau thêm 0,8 s. Parent chụp stack rồi kill/reap child thử, không để thread/process treo. Stack của main dừng tại **`concurrent.futures.thread._python_exit → threading.join`**, worker nằm trong `blocked_dns`. Control cùng entry, resolver trả địa chỉ loopback ngay, kết thúc bình thường. Đây là fault injection chứng minh đường shutdown, **không phải** lỗi DNS hệ điều hành/provider đã quan sát trên môi trường thật.

**Cần sửa:** deadline bao cả kết thúc lệnh và thu hồi worker có thể kẹt, không chỉ thời gian trả của coroutine. Chọn giải pháp nhỏ trong tooling read-only; nếu dùng process riêng, parent phải terminate/reap được nó khi hết hạn, không vô tình chờ process đó ở teardown. Không dùng private executor hook/daemon-thread để né shutdown, không tắt kiểm TLS/routing hoặc hard-code IP provider. Thêm regression subprocess có resolver treo và control resolver bình thường; assert process exit trong bound, không chỉ `reconcile` trả về. Giữ các ca HTTP thật đang xanh.

### Kiểm reviewer tại source này

| Nhóm | Kết quả |
| --- | --- |
| Năm file offline trong repo | **71/71 pass**, exit 0, 6,42 s, HTTP ngoài bị chặn; loopback được phép |
| Repro body nhỏ giọt độc lập Linux | **2/2 pass**: control 0,075 s; deadline 1 s kết thúc 1,003 s, ID chưa hoàn tất giữ pending |
| Subprocess resolver/shutdown độc lập Linux | **1 control pass / 1 failure P2-3**, failure xác nhận hai lượt, không đếm lặp |

Tổng **71 repo + 3 ca độc lập pass / 1 ca độc lập fail**. Không rerun selection 652, web/Chrome/restore hoặc live; test repo đã bao header nhỏ giọt và `/key` chậm. Hai P2 khác không thay code nên giữ chứng cứ review trước, không cộng thành pass mới.

**Audit:** **8 artifact hash** khớp; **485 source hash** agent trước/sau bằng nhau và khớp source hiện tại; JUnit/inventory/reviewer khớp đủ **71 ID pass**. Không đọc/sửa ledger lịch sử trong lượt này; giữ mốc đã audit 20 request/$0,11675006 cam kết và 19 physical call/$0,01941006 billed, không cấp thêm lượt inference.

Evidence `C:\Users\ADMIN\AppData\Local\Temp\ras-pr64-rereview2-20261019`: `offline.xml/log`, `poll-drip.log`, `dns-exit-first.log`, `dns-exit.log`, `dns-child-blocked.log` (stack), `dns-child-fast.log`, các helper/exit JSON, `audit-result.json`, source/baseline/cleanup. **455 tracked source hash reviewer** trước/sau trùng. Container thử tự gỡ; child fault bị parent kill/reap; không tạo volume/credentials. **24 container có sẵn giữ ID/name/image/mount/label**; dev worker/beat đã restarting, RestartCount 97→100, không can thiệp. Không key/`.env`, inference, DB/dev/Dify/pricing hoặc account setting. Report/prompt và cập nhật trạng thái để **chưa commit**, dành cho cùng PR sửa; reviewer không merge/gửi comment GitHub.

## Lịch sử review lại tại `f538ddf`

## Review lại tại `f538ddf` — còn P2-3 ở deadline HTTP

Ngày **2026-10-19**, head **`f538ddfa534a87132cb4e2b095da9e2c6ce7d2bb`**, code **`37684c6`**, source **0052**, PR **OPEN/chưa merge**. **P2-1 và P2-2 đã sửa qua kiểm độc lập; P2-3 mới sửa phần điều phối batch, còn thiếu deadline bao trọn HTTP đang đọc body. Chưa nên merge.** Phần review tại `5dc8947` bên dưới là lịch sử, không phải ba finding còn mở ở head này.

### P2-3 còn lại — `timeout=remaining` không phải deadline toàn request

Vị trí: [`scripts/m7a_live.py:205`](../../scripts/m7a_live.py) (`_get`), các chỗ gọi trong `reconcile` dòng 362 và 381.

`client.get(..., timeout=remaining)` dùng timeout theo từng pha/chờ dữ liệu của HTTPX. Read timeout giới hạn thời gian chờ **một phần dữ liệu tiếp theo**, không giới hạn tổng thời gian đọc body ([tài liệu HTTPX](https://www.python-httpx.org/advanced/timeouts/)). Một response tiếp tục gửi từng byte trước khi read timeout hết có thể giữ `_get()` lâu hơn deadline, thậm chí không có giới hạn tổng. Kiểm đồng hồ ở vòng ngoài chỉ chạy lại sau khi `client.get` đã đọc xong response. `/generation` và `/key` cùng dùng đường này.

**Tái hiện thực chạy:** container `rasm7a-api:local --network none --rm`, HTTP server chỉ nghe loopback trong container, key tổng hợp; gọi chính `_get`/`reconcile` hiện hành, không mock transport hay đồng hồ. Server trả JSON hợp lệ từng byte cách nhau 80 ms. Với `deadline_s=1`, lượt đầu mất **2,329 s**, lượt xác nhận mất **2,305 s**, assertion kết thúc trước 1,5 s fail cả hai lần. Control cùng server/body nhưng gửi ngay mất **0,041 s**, pass. Đây là test deadline rút ngắn để tái hiện, **không phải** đo OpenRouter hay chạy chờ 300 s. `key_status=skipped_deadline` chỉ bỏ GET kế tiếp; không hủy GET đã vượt hạn.

Hai fake-clock test của reviewer và test trong repo đều qua vì `_get` giả tự kết thúc trong timeout được cấp. Chúng xác nhận batch không cấp thêm thời gian sai, nhưng chưa xác nhận HTTP thật tuân deadline tổng. Bản sửa cần một deadline có thể hủy toàn thao tác HTTP (headers/body, cả `/key`), giữ kết quả đã hoàn tất và ID pending, giải phóng response/client. Không chỉ kiểm sau khi body đã đọc xong, không dùng thread timeout rồi chờ thread treo lúc thoát. Giữ phạm vi tooling nhỏ và [prompt sửa cùng PR](../tasks/fix-pr64-m7a-experiment-cap-and-polling.md).

### Kết quả reviewer ở head sửa

| Kiểm | Kết quả |
| --- | --- |
| Bốn file offline trong repo, HTTP ngoài bị chặn | **67/67 pass**, exit 0, 2,23 s; JUnit khớp từng ID với agent |
| Linux permit/observer độc lập | **3/3 pass**: giữ slot 20 khi claimed/chưa marker, chặn action chạy chồng, receipt muộn không gán sang action sau |
| Cost MockTransport độc lập | **2/2 pass**: positive control và negative giữ đầy đủ trần |
| Poll fake-clock độc lập | **2/2 pass**: fast control và batch dừng ở 300 s, không gọi `/key` sau hạn |
| HTTP loopback thật trong container không mạng | **1 control pass / 1 failure P2-3**; failure được xác nhận lại, không đếm lặp thành test mới |
| Hai phía Windows/Linux cùng quan sát một token trước `rename` | **12/12 kiểm pass**, 6 claimed / 6 host revoked, không token đồng thời ở hai đích |

Tổng **67 repo + 8 ca độc lập pass, 1 ca độc lập fail**; 12 kiểm token tách riêng. Barrier token dừng observer ngay trước `rename`, buộc cả hai phía thấy cùng source trước khi release; đây là chứng cứ trên bind mount hiện tại, không phải bảo đảm cho mọi filesystem hay mất điện. Ledger lock giữ qua action/outcome; consumed không receipt giữ trần, receipt gắn token. Không phát hiện điểm chặn mới ở P2-1/P2-2.

**Audit lượt sửa:** **16 artifact hash** khớp; **484 source hash** trước/sau của agent bằng nhau và khớp source hiện tại; JUnit **67 ID** khớp inventory và lượt reviewer. Đọc lại ledger lịch sử mà không sửa bytes: **20 request / $0,11675006 cam kết**, không còn lượt inference trong trần gốc. Dữ kiện lịch sử 19 physical call / $0,01941006 billed và audit selection 652 giữ riêng theo lượt trước; không rerun live/selection. Việc agent đã đọc key một lần để quét vắng mặt được giữ như deviation trong handoff/inventory; reviewer không đọc lại key để kiểm.

Evidence: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr64-rereview-20261019` (`offline.xml/log`, `runtime-gate.log`, `cost_receipt.log`, `poll_deadline.log`, `poll-drip-first.log`, `poll-drip.log`, `rename-result.json`, `audit-fix-result.json`, source/baseline/cleanup). Không thay production, tests repo, migration/grants hoặc pricing; không key/`.env`, provider inference, DB/dev/Dify. Container thử tự gỡ, không tạo volume; **24 container có sẵn giữ ID/name/image/mount/label**, worker/beat đã restarting từ baseline, RestartCount 48→53, không can thiệp. **454 tracked source hash reviewer** trước/sau trùng.

Giữ report và prompt cập nhật **chưa commit** cho cùng PR sửa; reviewer không merge/gửi comment GitHub. Cache-write live, policy/account verification, M7b và E vẫn riêng; P2 còn lại không cần inference hoặc ngân sách mới để sửa.

## Lịch sử review tại `5dc8947`

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
