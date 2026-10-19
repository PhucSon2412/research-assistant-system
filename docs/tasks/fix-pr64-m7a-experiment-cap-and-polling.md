# Sửa PR #64 — cap experiment, receipt cost và polling

**Đã qua review lại tại `285fd37` (code `38d5409`, 2026-10-19):** ba P2 và các nhánh deadline/DNS đã sửa; reviewer 73 repo + 4 ca độc lập pass, không thấy điểm chặn merge mới trong scope M7a. [Kết luận và giới hạn](../reviews/pr64-m7a-openrouter-live-verification.md#review-lại-tại-285fd37--ba-p2-đã-sửa-không-thấy-điểm-chặn-merge-mới). Các prompt bên dưới giữ lịch sử, không phải giao thêm vòng sửa hoặc inference. PR vẫn OPEN/chưa merge; pricing mặc định tắt, trần live gốc hết, cache-write live/M7b/E còn riêng.

## Review lại tại `9b64901`: chỉ còn deadline thoát tiến trình khi DNS treo

Body/header nhỏ giọt và `/key` chậm **đã được reviewer xác nhận sửa**: 71 repo pass và repro Linux body 1,003 s với deadline 1 s. P2-1/P2-2 giữ kết luận đã sửa. Chỉ sửa tiếp nhánh shutdown của P2-3, cùng PR #64/source **0052**.

Giới hạn DNS trong handoff nay đã tái hiện: fault injection làm `socket.getaddrinfo` chờ vô hạn, `reconcile` trả pending sau 1,023 s nhưng Python không exit. Stack main nằm ở `_python_exit → threading.join`, worker kẹt trong resolver; control resolver bình thường exit. `loop.close()` không đợi tại chỗ nhưng interpreter vẫn join thread executor. [Report mới](../reviews/pr64-m7a-openrouter-live-verification.md#review-lại-tại-9b64901--http-đã-hủy-đúng-còn-p2-3-ở-thoát-tiến-trình-khi-dns-treo) và evidence `C:/Users/ADMIN/AppData/Local/Temp/ras-pr64-rereview2-20261019` có helper subprocess và stack, không provider/key thật.

- Deadline cần bao **kết thúc lệnh và thu hồi worker có thể kẹt**, không chỉ coroutine trả kết quả. Chọn thay đổi nhỏ ở tooling read-only; một worker process có thể terminate/reap là phương án nếu cần. Không thêm framework auth/accounting, không private executor hook/daemon-thread để lách shutdown, không hard-code provider IP hoặc nới TLS/routing.
- Nếu dùng process/IPC, không đưa key vào argv/log/file/ledger; giữ truyền secret tối thiểu trong bộ nhớ và kiểm redaction bằng key tổng hợp. Lượt sửa vẫn **không đọc key/`.env` hoặc inference**. Không thay producer inference/probe, production/grants/migration/pricing/cap.
- Thêm test **subprocess thực**: resolver treo không trả, resolver trả bình thường; cả hai phải có kết quả/deadline và process exit trong bound. Parent kiểm/kill/reap để regression không làm suite treo. Chỉ assert elapsed của hàm hoặc không còn thread ở caller chưa đủ. Giữ bốn test HTTP loopback thật và 67 test cũ.
- Chạy lại 71 ca + regression mới và repro Linux tương ứng, chứng cứ exit/source/JUnit rõ. Không cần full 652/>5.000/web/Chrome/restore/live, không reset ledger. Trần gốc vẫn hết: 20 request/$0,11675006 cam kết.
- Giữ report/prompt reviewer và mọi docs modified/untracked, đưa vào cùng PR sửa. Cập nhật PR tiếng Việt/handoff/inventory, phân biệt fault injection với DNS thật; không gọi giới hạn này là “chưa tái hiện”. Không merge/AI trailer/promo, không tác động dev/Dify/provider/account.

## Prompt lượt trước — HTTP body/header đã qua review tại `9b64901`

## Review lại tại `f538ddf`: chỉ còn deadline HTTP của P2-3

**P2-1/P2-2 đã qua review độc lập; không mở lại hai phần đó.** Sửa tiếp trên cùng PR #64, source **0052**, chỉ tooling HTTP polling/test/docs. [Report mới](../reviews/pr64-m7a-openrouter-live-verification.md#review-lại-tại-f538ddf--còn-p2-3-ở-deadline-http) giữ 67 repo + 8 independent pass, một repro fail.

`_get` dùng `httpx.Client.get(timeout=remaining)`: HTTPX read timeout áp cho mỗi lần chờ dữ liệu, không phải tổng headers/body. HTTP loopback thật trong Linux `--network none` gửi JSON từng byte cách 80 ms: `reconcile(..., deadline_s=1)` mất 2,329 s và 2,305 s ở hai lượt; control gửi ngay 0,041 s. Fake `_get` tự kết thúc trong timeout nên không bắt nhánh này.

- Thi hành **deadline tổng có khả năng hủy HTTP đang chờ/đọc**, gồm `/generation` và `/key`, cả headers/body/connection. Có thể dùng async cancellation được giới hạn bởi deadline tuyệt đối ở helper polling nhỏ; không bắt buộc phương án, không thêm framework. Timeout theo pha vẫn có ích nhưng không thay deadline tổng.
- Khi hết hạn, dừng request/vòng mới, trả các ID hoàn tất trước hạn và pending chính xác, đóng response/client. Không chỉ kiểm đồng hồ sau khi `get()` trả về, không bỏ thread treo rồi chờ vô hạn lúc teardown/exit. Không nhận body hoàn tất sau hạn làm bằng chứng đã hoàn tất trong hạn.
- Thêm regression **HTTP loopback thật**, không chỉ MockTransport/fake clock: fast control, body nhỏ giọt luôn đến trước read timeout, header chậm, và `/key` chậm sau khi IDs đã tìm được. Rút deadline để test nhanh, tolerance hợp lý; kiểm elapsed, pending/found, đóng tài nguyên và process thoát. Giữ các ca batch/fake clock hiện có.
- Chạy lại bốn file offline 67 ca cùng test mới và repro HTTP. Không rerun 652/full suite/Chrome/restore hoặc live để sửa nhánh scripts này. Không đổi production/migration/grants/adapter/routing/pricing/cap.
- **Không đọc key/`.env`, không inference mới, không thao tác dev/Dify/account.** Trần gốc đã hết theo ledger fail-closed (20 request/$0,11675006 cam kết); không xóa/reset ledger. Lỗi này kiểm bằng local HTTP không mạng ngoài, không cần xin thêm ngân sách.
- Giữ report/prompt reviewer và docs modified/untracked, commit vào cùng PR sửa; history pass/fail/source tách riêng, không ghi cả ba P2 đã được reviewer duyệt. Cập nhật PR tiếng Việt, không merge, không AI trailer/promo.

Evidence tái hiện độc lập ở `C:\Users\ADMIN\AppData\Local\Temp\ras-pr64-rereview-20261019`: `test_poll_drip.py`, `run_drip.py`, hai log và exit JSON. Reuse/đưa regression phù hợp vào repo; không phụ thuộc đường TEMP này để test sau merge.

## Prompt lượt trước — giữ lịch sử phạm vi ba P2

Sửa **trên cùng PR #64**, head review `5dc8947`, source **0052**. Đọc [report độc lập](../reviews/pr64-m7a-openrouter-live-verification.md), [handoff](../m7a-openrouter-live-verification.md) và [task gốc](m7a-openrouter-live-verification.md). Giữ và đưa mọi docs reviewer/orchestrator modified/untracked vào PR; không discard/ghi đè. Không chia PR mới hoặc mở thêm billing/auth/permission framework.

## 1. Giữ cap/concurrency khi runtime action kết thúc trước handler

`Drill.gated()` nhả ledger lock sau admit, ghi allowance=0 không cùng flock, chờ một giây rồi suy `not_sent` từ receipt vắng. Reviewer tái hiện trên Linux/network none bằng chính observer: request thứ 20 đã consume allowance nhưng chưa ghi marker; action return → ledger hoàn slot; request thứ 21 được admit; handler cũ tiếp tục delegate. Ledger báo 20 trong khi có 19 trước + 2 mới. Hai runtime action cũng được phép overlap.

- Giữ ownership/serialization của experiment xuyên admission → action → đóng allowance → chụp outcome; process khác không được bật/reset allowance của action đang chạy. Local experiment lock qua network được phép; không giữ DB lock.
- Consume permit và đóng/chụp allowance phải atomic theo cùng flock/cơ chế phù hợp, có binding batch/slot/receipt để response muộn không bị gán sai. **Consumed + chưa có marker/response = uncertain, giữ đầy đủ request và ceiling.** Không suy `not_sent` vì sleep một giây hoặc vì client/action đã kết thúc.
- Chỉ hoàn slot thật sự chưa được lấy với chứng cứ đã đóng quyền gửi. Permit đã được lấy nhưng handler còn chạy có thể kết thúc sau đó, và vẫn phải được tính/đối soát đúng. Không hứa cancel host/client giết provider thread.
- Crash-resume/receipt lỗi/lock cũ/teardown giữ fail closed, không reset ledger hoặc allowance để tiếp tục lấy lượt xanh. Chọn thay đổi nhỏ nhất ở tooling hiện có, không thêm bảng sản phẩm/migration/grant.

## 2. Cost receipt không được tạo “credit” âm

`usage.cost=-1` ở response đúng identity/vector được probe ghi completed; `Ledger.state()` tính committed=-1. Producer và ledger reader cần validation chung: số hợp lệ, hữu hạn, không âm, không boolean; missing/invalid giữ trần hoặc dừng trước call mới. Không clip negative về 0; zero-cost hợp lệ vẫn được nhận.

Kiểm cả probe, runtime receipt và ledger cũ khi resume; negative/NaN/Infinity/boolean/kiểu sai không làm giảm committed, không làm admission chạy tiếp với tiền giả. Invalid receipt không được thành chứng cứ actual charge/pricing verified. Giữ riêng estimated và provider-reported billed.

## 3. Deadline phủ mọi GET và sleep

`reconcile()` chỉ kiểm đầu while, nên 19 GET ×30 s +sleep+`/key` thành 602 s trên fake clock dù deadline 300. Kiểm remaining trước **từng GET**, cấp timeout theo remaining, cắt sleep và không gọi `/key` sau deadline. Giữ kết quả đã tìm được và pending rõ ràng, không tự inference hoặc chờ vô hạn. Không sửa test fake-clock để cho phép overrun cả batch.

## Kiểm chứng và phạm vi sửa

- Tái hiện các lỗi ở source review bằng test mới, rồi chứng minh sửa: Linux permit/marker barrier + action trả sớm, hai runtime caller đồng thời, no-receipt vẫn giữ trần; request thứ 21 bị từ chối trước delegate. Có control action bình thường/permit thật sự unused và các call còn pending qua restart. Không chỉ assert khác mã lỗi.
- Receipt matrix gồm positive/zero, negative, NaN/Infinity, boolean/kiểu sai/missing; kiểm lại ledger mới đọc từ đĩa. Guard phải bắt cả một producer bị mock trả cost xấu, không chỉ helper được gọi trực tiếp.
- Poll nhiều ID, mỗi GET chậm, deadline kết thúc giữa batch, skip/cap `/key`, sleep gần hạn, all-found control. Dùng fake clock/mock HTTP và các lỗi transport phù hợp, không tiêu tiền thử lỗi provider.
- Chạy 31 repo test reviewer đã dùng cùng nhóm mới và hồi quy scripts/runtime liên quan, source hash/JUnit/exit rõ. Không mặc định rerun 652/full >5.000 hoặc các live calls; web/Chrome không cần nếu không đổi đường đó. Không mutate source/countercheck trong lúc seal chạy.
- **Không phát sinh live inference cho các bản sửa này.** Lượt lịch sử 19 call/$0,01941006 đã audit đúng, không bị xóa hoặc thay bằng một run mới. Nếu sau đó cần lượt live xác nhận cache-write source cuối, dùng ledger cũ và giới hạn task gốc còn lại sau khi guard đã sửa; không tự cấp lại $0,50/20 request hoặc xóa state. Đây không phải điều kiện để tái hiện ba P2.
- Giữ production routing/identity, giá cache-write và default pricing false; không nới validator, quyền, model, cap hoặc endpoint để test xanh. Source/migration **0052** không đổi trừ nhu cầu riêng có chứng cứ (không dự kiến ở sửa scripts này). Không đọc key/`.env`, thao tác dev/Dify/provider/account trong lượt sửa offline.

Commit/push bản sửa vào PR #64, cập nhật mô tả PR tiếng Việt, handoff/inventory với lịch sử các lượt riêng. Giữ author/committer, không AI trailer/promo; không merge. Nêu rõ ba P2 đã sửa và cache-write live/M7b/E vẫn còn giới hạn nào. Giữ report reviewer nguyên chứng cứ ban đầu, chỉ thêm mốc source mới trong handoff khi thực chạy.
