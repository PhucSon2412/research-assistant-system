# Kế hoạch hoàn thành MVP

Ngày chốt **2026-10-11**. Chủ dự án giao orchestrator chọn phạm vi và cách làm hợp lý để đưa dự án tới một bản dùng được. Kế hoạch này là nguồn trạng thái hiện hành; các report/handoff cũ giữ chứng cứ tại thời điểm chạy. Không suy kết quả kiểm trước merge thành kết quả mới.

## Đích phát hành

**MVP cho một nhóm nghiên cứu nhỏ, một instance, tài khoản theo lời mời.** Hoàn thành khi nhóm thử có thể dùng liên tục các luồng upload → index → tìm kiếm → hỏi có nguồn, và Admin có thể duyệt/công bố/thu hồi; xóa và restore giữ đúng quyền. PDF/Markdown, không OCR, không tự động tóm tắt paper. Các yêu cầu đã xác nhận trong [product requirements](product-requirements.md) và [access/data](access-and-data.md) vẫn giữ nguyên.

Đây là mốc nghiệm thu phần mềm và vận hành tối thiểu. Không đồng nghĩa đã đạt chất lượng production trên mọi paper, chịu mất điện, hoặc hiệu năng ở quy mô lớn.

## Trạng thái hiện tại

- PR #56 đã merge tại `808337dcccfa472eefb05f2c968250948bae9bc2`; head review `d957872` là ancestor, tree khớp. Source migration **0046**; không suy revision DB dev.
- Các chức năng chính đã có, nhưng đợt chuyển auth/runtime chưa kết thúc. **Safety vẫn pending tới C4**; không coi Compose dev đang lỗi readiness/restart là một bản sẵn dùng.
- Citation reconcile và [discovery harvesting](c2d5f2-discovery-runtime.md) đã chuyển runtime. [Review PR #56](reviews/pr56-c2d5f2-discovery-runtime.md) giữ kết quả trước merge; chưa rerun tại merge. Docs orchestrator còn modified/untracked được giao đưa vào PR kế tiếp, không mở lại PR #56.
- Đã giao [C2d-6 search/query embedding/rerank](tasks/m6c2b2c2d6-search-query-rerank-runtime.md) thành một PR hoàn chỉnh; mới giao, chưa triển khai/kiểm.
- Còn search/query/rerank, RAG/generation, operator recovery và tích hợp/cutover. Private deletion/restore, provider thật và vận hành triển khai cần nghiệm thu riêng.
- Nhãn đánh giá RAG hiện còn `pending_human_review`. Charge thật chưa đối soát; không bật `pricing_verified` bằng kết quả provider giả.

## Các gói công việc còn lại

Ước lượng là **ngày làm việc**, giả định một coding agent, reviewer theo sát và khoảng một lượt sửa mỗi PR. Usage limit, hạ tầng hoặc lỗi tích hợp có thể kéo dài. Các gói có thể chồng một phần; không cộng thời gian chờ người vận hành thành thời gian coding.

| Gói | Phạm vi và đầu ra | Ước lượng | Trạng thái |
| --- | --- | --- | --- |
| A — hoàn tất runtime | Search + query embedding + rerank trong một gói (2–3 ngày); RAG + generation trong một gói (2–3 ngày). Giữ quyền trước retrieval/provider và kế toán hiện hành | 4–6 ngày còn lại | Discovery đã merge PR #56; C2d-6 đã giao, chưa triển khai/kiểm; RAG/generation kế tiếp |
| B — recovery và bản chạy đầy đủ | C3 CLI operator tối thiểu; xử lý debt hồi quy; C4 Compose/image/role/journal, readiness thật, HTTP/browser không patch pending, hai vòng restore/recover | 5–8 ngày | Chưa giao |
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

- **C3:** chốt inventory UUID/chứng cứ và người vận hành trước khi thực sự phục hồi account. Không tự coi quyền DB là bằng chứng danh tính User.
- **C4/triển khai:** chọn host, nơi lưu journal ngoài miền sự cố DB/object/backup và cách cập nhật hiện hành. File khác volume trên cùng máy chưa chứng minh độc lập; DB/journal cùng rollback có thể không phát hiện.
- **Gói D:** charge/routing/retention và quyền gửi paper thật phải được kiểm trước khi bật. Kế toán common embedding hiện là phương án orchestrator chọn, cần ghi đúng nguồn quyết định khi nghiệm thu.
- **Debt:** 27 node được tách ở PR #55 và bốn mục PR #50 là lịch sử cần đối chiếu theo node, không cộng máy móc thành số lỗi khác nhau. C2d-5f2 đã chuyển hai fixture discovery sang runtime và thực chạy harvesting; gói B nhận các mục còn lại.
- **arXiv HTTP 406:** chưa xác định nguyên nhân live. C2d-5f2 kiểm runtime/retry bằng fixture; live network smoke nằm ở nghiệm thu sau, không báo đồng bộ live hoàn tất bằng feed giả.
