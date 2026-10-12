# Kế hoạch hoàn thành MVP

**Sửa review lại PR #59 tại `d2d56f3` (2026-10-12, chờ review lại, chưa merge):** Ctrl-C khi trả connection giờ là outcome theo `Attempt` (không còn “nothing was changed” sau commit), connection luôn invalidate/close. Agent: nhóm C3 106/106 có niêm hash, đối chứng 9/9, smoke API/proxy 28/28; [handoff](c3-operator-auth-recovery.md#review-lại-tại-6465dc0-ctrl-c-khi-trả-connection). **Safety pending tới C4.**

**Review lại C3 tại `6465dc0`, 2026-10-12:** còn một nhánh P2-2 (Ctrl-C trong release sau known commit báo `nothing was changed`); [report](reviews/pr59-c3-operator-auth-recovery.md) và [prompt sửa cùng PR](tasks/fix-pr59-c3-final-authority-and-release.md). P2-1/release lỗi thông thường đã qua kiểm độc lập. PR #59 vẫn OPEN/chưa merge, source `0049`; gói B đang C3, chưa mở C4 hoặc thay forecast/checklist. Safety pending; các cập nhật trước dưới đây giữ lịch sử.

**Cập nhật sửa review C3, 2026-10-12:** hai P2 PR #59 đã sửa trên cùng PR (head `0ee17f3`), chờ review lại, chưa merge; [handoff](c3-operator-auth-recovery.md#sửa-review-pr-59-2026-10-12). Gói B vẫn ở C3; debt/hồi quy toàn inventory và C4 chưa giao. Không giảm checklist hay forecast bằng suite pass. Safety pending.

**Cập nhật review C3, 2026-10-12:** PR #59 còn OPEN, source `0049`; [review](reviews/pr59-c3-operator-auth-recovery.md) có hai P2 và [prompt sửa cùng PR](tasks/fix-pr59-c3-final-authority-and-release.md). Gói B đang C3, chưa giao cutover C4 hoặc giảm checklist bằng suite pass. Safety vẫn pending; giữ forecast/giả định bên dưới, đánh giá lại tại C4 với debt và tích hợp thực tế.

Ngày chốt **2026-10-11**. Chủ dự án giao orchestrator chọn phạm vi và cách làm hợp lý để đưa dự án tới một bản dùng được. Kế hoạch này là nguồn trạng thái hiện hành; các report/handoff cũ giữ chứng cứ tại thời điểm chạy. Không suy kết quả kiểm trước merge thành kết quả mới.

## Đích phát hành

**MVP cho một nhóm nghiên cứu nhỏ, một instance, tài khoản theo lời mời.** Hoàn thành khi nhóm thử có thể dùng liên tục các luồng upload → index → tìm kiếm → hỏi có nguồn, và Admin có thể duyệt/công bố/thu hồi; xóa và restore giữ đúng quyền. PDF/Markdown, không OCR, không tự động tóm tắt paper. Các yêu cầu đã xác nhận trong [product requirements](product-requirements.md) và [access/data](access-and-data.md) vẫn giữ nguyên.

Đây là mốc nghiệm thu phần mềm và vận hành tối thiểu. Không đồng nghĩa đã đạt chất lượng production trên mọi paper, chịu mất điện, hoặc hiệu năng ở quy mô lớn.

## Trạng thái hiện tại

- PR #58 đã merge tại `58d059ab7a383d5f97e1b04e909e9628a11bf78a`; head review `aa9c8a2` là ancestor, tree khớp. Source migration **0048**; không suy revision DB dev.
- Các chức năng chính đã có, nhưng đợt chuyển auth/runtime chưa kết thúc. **Safety vẫn pending tới C4**; không coi Compose dev đang lỗi readiness/restart là một bản sẵn dùng.
- Citation reconcile và [discovery harvesting](c2d5f2-discovery-runtime.md) đã chuyển runtime. [Review PR #56](reviews/pr56-c2d5f2-discovery-runtime.md) giữ kết quả trước merge; chưa rerun tại merge. Docs orchestrator bị sót đã được đưa vào commit của PR #57.
- [C2d-6 search/query embedding/rerank](c2d6-search-query-rerank-runtime.md) đã merge; [review độc lập](reviews/pr57-c2d6-search-query-rerank-runtime.md) giữ chứng cứ trước merge, chưa rerun tại merge. Không suy kết quả này thành RAG runtime hoặc cutover.
- [C2d-7 RAG/generation runtime](c2d7-rag-generation-runtime.md) đã merge PR #58, source **0048**; [review độc lập](reviews/pr58-c2d7-rag-generation-runtime.md) và [inventory](c2d7-test-inventory.json) giữ chứng cứ trước merge, chưa rerun tại merge. Gói A hoàn tất chuyển runtime trong scope đã review, chưa cutover/nghiệm thu end-to-end. Đã giao [C3 operator auth recovery](tasks/m6c2b2c3-operator-auth-recovery.md), chưa triển khai/kiểm; sau đó hồi quy/debt và tích hợp C4. Private deletion/restore, provider thật và vận hành triển khai cần nghiệm thu riêng. Giữ và đưa docs review/task mới chưa commit vào PR kế tiếp.
- [C3 operator auth recovery](c3-operator-auth-recovery.md) đã triển khai trên nhánh review, source **0049**, chưa merge: login operator riêng, case bind installation/User UUID/action/role/heads dùng một lần theo journal, TTY ẩn, maintenance hold, credential/status tách riêng; không mở disabled/ready. Kết quả kiểm theo [inventory](c3-test-inventory.json); chưa có inventory/ủy quyền người thật. Tiếp debt/hồi quy và tích hợp C4.
- Nhãn đánh giá RAG hiện còn `pending_human_review`. Charge thật chưa đối soát; không bật `pricing_verified` bằng kết quả provider giả.

## Các gói công việc còn lại

Ước lượng là **ngày làm việc**, giả định một coding agent, reviewer theo sát và khoảng một lượt sửa mỗi PR. Usage limit, hạ tầng hoặc lỗi tích hợp có thể kéo dài. Các gói có thể chồng một phần; không cộng thời gian chờ người vận hành thành thời gian coding.

| Gói | Phạm vi và đầu ra | Ước lượng | Trạng thái |
| --- | --- | --- | --- |
| A — hoàn tất runtime | Search + query embedding + rerank; RAG + generation. Giữ quyền trước retrieval/provider và kế toán hiện hành | Đã merge | PR #57/#58 đã merge; chưa cutover/nghiệm thu end-to-end |
| B — recovery và bản chạy đầy đủ | C3 CLI operator tối thiểu; xử lý debt hồi quy; C4 Compose/image/role/journal, readiness thật, HTTP/browser không patch pending, hai vòng restore/recover | 5–8 ngày | C3 đã triển khai trên nhánh review (chưa merge); debt và C4 chưa giao |
| C — đóng yêu cầu sản phẩm còn thiếu | Xóa item private + chống xuất hiện lại sau restore; nghiệm thu duplicate/retry hiện hành, chỉ bổ sung chỗ thiếu để luồng dùng được | 2–4 ngày | Chưa giao |
| D — provider và chất lượng | Smoke tổng hợp có trần chi phí, đối soát charge, routing/retention; sau đó thử paper được phép xử lý, người đọc kiểm câu trả lời/citation/abstention | 2–4 ngày | Chưa giao; chưa mở trả phí |
| E — triển khai nhóm thử | HTTPS/cookie Secure, kênh invite/reset dùng được, rate limit, CI, journal độc lập, backup/retention/alert tối thiểu và nghiệm thu browser | 3–5 ngày | Chưa giao |

**Dự kiến:** khoảng **2–3 tuần** để hoàn tất runtime/recovery và có bản end-to-end; khoảng **4–6 tuần tổng cộng từ mốc kế hoạch này** để nghiệm thu MVP cho nhóm nhỏ. Đây là khoảng lập kế hoạch, không phải cam kết ngày giao hay SLO. Cập nhật sau gói A và sau lần C4 đầu; nếu vượt khoảng, nêu rõ việc phát sinh và ảnh hưởng trước khi mở thêm scope.

**Cập nhật sau merge PR #56, 2026-10-11:** tổng còn lại khoảng **16–27 ngày làm việc** theo bảng, có thể chồng một phần. Giữ dự báo thực tế **4–6 tuần**, vì discovery mới giảm một phần của gói A; lỗi tích hợp, debt, restore và nghiệm thu provider/triển khai vẫn chưa được đo đầy đủ. M6 kéo dài do phạm vi đã mở sang chống rollback auth sau restore và chuyển runtime toàn hệ thống, kèm nhiều PR theo endpoint. Hai gói runtime còn lại được gom theo luồng; không thêm tính năng ngoài checklist để kéo dài đích MVP. Usage limit hoặc chờ hạ tầng/charge có thể khiến lịch vượt khoảng, khi đó báo nguyên nhân cụ thể.

## Checklist nghiệm thu cố định

Mỗi mục cần một chứng cứ có commit/image, cách chạy và kết quả. Không đánh dấu đạt bằng build xanh, provider giả hoặc toàn bộ request bị 503.

- [ ] **Bản cài chạy được:** cài mới và nâng cấp có dữ liệu trên project cô lập; image đúng revision, runtime dùng đúng DB role; health/readiness và worker/beat ổn định sau C4. Runbook không yêu cầu người dùng tự đoán các bước pending.
- [ ] **Luồng web chính:** bootstrap một lần, invite/accept/login/reset/logout; upload PDF/Markdown có xác nhận, trạng thái xử lý/coverage, tải original, collection, search keyword/hybrid, hỏi theo ba scope và mở citation; review/approve/publish/revoke. Có kiểm A/B/Admin qua proxy và trình duyệt thật.
- [ ] **Quyền và restore:** request mới sau revoke/xóa bị chặn; không lộ private giữa owner/Admin; worker/provider không công bố dữ liệu mất quyền; restore không hồi sinh common đã revoke, review grant đã đóng, auth đã thu hồi hoặc item private đã xóa trong phạm vi journal được bảo vệ. Ghi rõ giới hạn bytes đã gửi và yêu cầu journal hiện hành độc lập.
- [ ] **Tìm kiếm và trả lời có ích:** đo retrieval trên corpus hiện có; duyệt nhãn và chấm câu trả lời bằng người đọc; thử thêm một bộ paper thật có quyền sử dụng với câu một paper, so sánh, thiếu đáp án, partial và injection. Sai nguồn, lộ dữ liệu hoặc khẳng định không có chứng cứ phải được xử lý; các ngưỡng chất lượng đề xuất không tự thành gate số học.
- [ ] **Chi phí được kiểm chứng:** route/model/dimension và routing/retention được kiểm; charge thực được đối soát trên key; giới hạn User/ứng dụng, concurrent calls và uncertain reservations hoạt động. Chỉ bật từng cấu hình trả phí sau chứng cứ tương ứng, không bật toàn bộ bằng một smoke.
- [ ] **Vận hành nhóm thử:** HTTPS, cookie Secure, delivery ngoài dev outbox, rate limit login/reset, CI cho gate kiểm chính, backup có kiểm checksum, lịch/retention và diễn tập restore; journal lưu độc lập và hiện hành, có người chịu trách nhiệm recovery và alert lỗi/cleanup quá hạn. Không tuyên bố chịu mất điện bằng fault injection.
- [ ] **Đóng nợ phát hành:** inventory toàn bộ test không thiếu/trùng, debt đã phân loại và xử lý; không còn lỗi nghiệp vụ/quyền đã biết nằm dưới nhãn “baseline”. Chạy hồi quy tổng sau tích hợp, không cần chạy toàn bộ mỗi PR.

## Cách tổ chức để kết thúc

1. **Một PR cho một luồng hoàn chỉnh** trong các gói trên; chia tiếp chỉ khi có phụ thuộc thật hoặc rủi ro khiến PR không review được. Không mặc định chia từng endpoint.
2. PR dùng nhóm kiểm phù hợp rủi ro và hồi quy liên quan. Full inventory/hồi quy tổng nằm ở gói B và trước phát hành; không bỏ test cũ vĩnh viễn vì nó fail ở base. Giữ nguyên kỳ vọng nghiệp vụ; sửa fixture nếu hợp đồng mới đã được chứng minh.
3. Mỗi PR có một handoff/inventory ngắn, cập nhật bảng trạng thái tại đây và docs hợp đồng bị ảnh hưởng. Không nhân bản toàn lịch sử kiểm vào đầu mọi tài liệu. Report cũ giữ nguyên chứng cứ.
4. Operator MVP là chủ dự án hoặc người được chỉ định trực tiếp; ưu tiên CLI nhập an toàn và hồ sơ UUID/case rõ ràng. Không xây hệ thống phê duyệt operator nhiều tầng. Latest disabled không tự enable; không dùng bootstrap để phục hồi Admin.
5. Nhóm thử dùng lời mời kín. Kênh gửi link có thể thủ công qua kênh đã xác minh; phải có adapter/quy trình được kiểm, không dùng Admin dev outbox để phục vụ thật. Không bắt buộc mua email service để hoàn thành coding.
6. Không thao tác stack dev hoặc dữ liệu thật trong các task C2/C3. C4 chuẩn bị bản chạy và runbook cụ thể; thay đổi môi trường thật thực hiện ở task cutover được giao riêng.

## Chưa đưa vào MVP

Streaming RAG, graph giao diện nâng cao, OCR, định dạng mới, import DOI/Hugging Face, nhiều tổ chức/nhóm quyền, tối ưu ANN/quy mô lớn và dashboard vận hành phức tạp. Không trì hoãn mốc dùng thử chỉ để đạt các mục tiêu p95/chất lượng chưa được chủ dự án xác nhận là SLO. Không hoãn các bất biến quyền, dẫn nguồn, revoke/xóa/restore và kiểm soát chi phí đã xác nhận.

## Rủi ro và quyết định ở đúng mốc

- **C3:** cách làm đã chốt trong [task](tasks/m6c2b2c3-operator-auth-recovery.md): operator được chỉ định, login riêng/case đúng UUID, TTY an toàn, status riêng và không mở disabled. Inventory/ủy quyền người thật vẫn phải được lập trước vận hành; coding dùng hồ sơ tổng hợp. Không coi quyền DB là bằng chứng danh tính User.
- **C4/triển khai:** chọn host, nơi lưu journal ngoài miền sự cố DB/object/backup và cách cập nhật hiện hành. File khác volume trên cùng máy chưa chứng minh độc lập; DB/journal cùng rollback có thể không phát hiện.
- **Gói D:** charge/routing/retention và quyền gửi paper thật phải được kiểm trước khi bật. Kế toán common embedding hiện là phương án orchestrator chọn, cần ghi đúng nguồn quyết định khi nghiệm thu.
- **Debt:** 27 node được tách ở PR #55 và bốn mục PR #50 là lịch sử cần đối chiếu theo node, không cộng máy móc thành số lỗi khác nhau. C2d-5f2 đã chuyển hai fixture discovery sang runtime và thực chạy harvesting; gói B nhận các mục còn lại.
- **arXiv HTTP 406:** chưa xác định nguyên nhân live. C2d-5f2 kiểm runtime/retry bằng fixture; live network smoke nằm ở nghiệm thu sau, không báo đồng bộ live hoàn tất bằng feed giả.

**Cập nhật sau merge PR #57, 2026-10-11:** search/query/rerank đã merge, chưa cutover. Gói A còn [C2d-7 RAG/generation](tasks/m6c2b2c2d7-rag-generation-runtime.md), khoảng 2–3 ngày coding cộng review theo giả định. Các gói B–E chưa giảm bằng test provider giả; giữ forecast 4–6 tuần tính từ mốc kế hoạch và đánh giá lại khi gói A/C4 có chứng cứ tích hợp. Không chia thêm theo endpoint nếu không có phụ thuộc thật. Kết quả C2d-6 tập trung tại [handoff](c2d6-search-query-rerank-runtime.md) và [review](reviews/pr57-c2d6-search-query-rerank-runtime.md).

**Cập nhật C2d-7 trên nhánh review, 2026-10-11:** phần code gói A đã hoàn tất RAG/generation trong một PR; review/seal/source và giới hạn xem [handoff](c2d7-rag-generation-runtime.md). Không suy thử provider giả thành checklist nghiệm thu MVP đã đạt. C3 + hồi quy/debt/tích hợp C4 là gói kế tiếp, không mở thêm runtime vi mô; forecast B–E giữ các giả định và cần đánh giá lại tại C4. Safety vẫn pending.

**Cập nhật sau merge PR #58, 2026-10-11:** gói A đã merge, giao C3 thành một PR để khép đường operator recovery; tiếp debt/hồi quy/C4 trong gói B. Giữ khoảng B–E và forecast 4–6 tuần theo giả định, đánh giá lại sau lần C4 đầu với lỗi tích hợp/restore thực tế; không trừ thời gian máy móc bằng số PR hoặc fake-provider pass. Kết quả review trước merge không là kiểm mới, checklist MVP chưa được đánh dấu đạt.

**Cập nhật C3 trên nhánh review, 2026-10-11:** C3 hoàn thành trong một PR theo task; [handoff](c3-operator-auth-recovery.md) chốt trust boundary, runbook hold → `recover --hold-for-operator` → operator → `recover`, và bản đồ C4. Gói B còn debt/hồi quy toàn inventory (gồm 27 node baseline và debt receipt `bootstrap`/`invitation_accept` generic) và tích hợp C4; giữ forecast B–E như trên, đánh giá lại sau lần C4 đầu. Không đánh dấu checklist MVP bằng kết quả C3.
