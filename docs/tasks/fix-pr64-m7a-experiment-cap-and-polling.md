# Sửa PR #64 — cap experiment, receipt cost và polling

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
