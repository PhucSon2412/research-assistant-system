# Kế hoạch hoàn thành MVP

**M6d sửa P2 PR #62 tại `a5eb8bb` (chờ review lại, chưa merge):** cleanup theo review UUID → item, guard/C4 giữ nguyên. [Handoff](m6d-private-item-delete-restore.md) · [inventory sửa hẹp](m6d-p2-lock-order-inventory.json): 731 pass, 4.528 not-run; không thay kết quả full theo source cũ hoặc coi hai failure lịch sử đã rõ nguyên nhân. F07 và phạm vi sau M6d giữ nguyên.

**Review M6d, 2026-10-17:** PR #62 tại `7494f8c`, source **0052**, còn [P2 deadlock cleanup/xóa lặp](reviews/pr62-m6d-private-item-delete-restore.md), cần [sửa cùng PR](tasks/fix-pr62-private-cleanup-repeat-delete-lock-order.md) trước merge. Gói C chưa nghiệm thu; F07 còn gap đã công bố. Giữ C4 readiness và các giới hạn D/E, không mở scope mới để sửa P2 hoặc coi 5.250 artifact pass là miễn lỗi mới.

**M6d trên nhánh triển khai, 2026-10-16:** source `0052`, base PR #61 `fb96354c`; chưa merge; đã kiểm cô lập. [Handoff](m6d-private-item-delete-restore.md) và [inventory](m6d-test-inventory.json) giữ luồng owner delete/cleanup/restore, caller/FK/lock map và chứng cứ riêng. C4 readiness giữ thật. F07 còn gap: cảnh báo hiện không xét bản đã ready/indexed, chưa reuse derivatives/canonical version graph hoặc nhận diện paper khác bytes; hồi quy retry không khép toàn F07. Gói D/E chưa mở, không cutover dev/provider thật.

**Sau merge C4, 2026-10-16:** PR #61 đã merge `fb96354c`, tree khớp `3f07901`, source **0051**, chưa rerun tại merge. Gói B hoàn tất trong phạm vi code/kiểm cô lập; [review](reviews/pr61-c4-integrated-runtime-cutover.md) giữ giới hạn production. Giao [M6d — owner delete → cleanup → restore denial](tasks/m6d-private-item-deletion-and-restore.md) thành một PR của gói C, mới giao/chưa triển khai hoặc kiểm. Giữ common độc lập và hồi quy duplicate/retry; gap F07 còn thiếu phải báo riêng, không gọi cảnh báo duplicate là nghiệm thu xong. Gói D/E và cutover thật chưa mở.

**Review C4, 2026-10-16:** PR #61 tại `3f07901`, source **0051**, không có điểm chặn merge mới trong scope C4 đã kiểm; [report độc lập](reviews/pr61-c4-integrated-runtime-cutover.md). Readiness có hậu điều kiện, image/roles và business HTTP/Chrome/hold đã kiểm cô lập; artifact agent giữ đủ 5.163 node cùng năm full drill. PR còn **OPEN/chưa merge**. Chưa cutover dev hoặc mở pricing/provider thật; các gói xóa/restore còn thiếu và vận hành/provider/nghiệm thu tiếp tục theo kế hoạch, không coi C4 là hoàn tất production. Các mốc pending trước C4 bên dưới giữ lịch sử.

**C4 đã triển khai và kiểm cô lập (source `0051`, chờ review/chưa merge):** [Handoff/runbook C4](c4-integrated-runtime-cutover.md) ghi checkpoint, image/roles và phạm vi kiểm cô lập. Không cutover dev hoặc bật provider/pricing thật. Inventory cuối đối chiếu đủ 5.163 node API đều pass, giữ 298 debt; năm full drill và CLI evaluation đã chạy. Các mốc bên dưới giữ lịch sử theo source riêng.

**Sau merge B1, 2026-10-15:** PR #60 đã merge `44633b99`, tree khớp `c6f200e`, source **0050**; chưa rerun tại merge. Giao [một PR C4 tích hợp](tasks/m6c2b2c4-integrated-cutover.md), mới giao/chưa triển khai hoặc kiểm: operational readiness có hậu điều kiện, cùng image/roles, HTTP/browser không seam, năm full drill và CLI evaluation. Mã hiện vẫn pending, dev/provider thật chưa được mở. Giữ forecast/checklist tới khi có chứng cứ C4; các mốc review dưới đây là lịch sử.

**Review B1, 2026-10-15:** PR #60 tại `c6f200e` không có điểm chặn merge mới trong scope đã review; [report](reviews/pr60-pre-c4-auth-receipts-regression.md) giữ kiểm reviewer, artifact agent và giới hạn riêng. PR còn OPEN, source **0050**, **safety pending tới C4**. Sau merge chuyển một gói C4 tích hợp đầy đủ, gồm năm drill và CLI evaluation chưa chứng nhận; giữ checklist/forecast và hiện tượng discovery chưa rõ nguyên nhân, không coi B1 pass là end-to-end readiness. Docs review được giữ cho PR kế tiếp.

**B1 đã triển khai và kiểm (2026-10-15, chưa merge):** source **0050**, base merge PR #59 `041f77ef`. [Handoff](pre-c4-auth-receipts-regression.md) và [inventory](pre-c4-test-inventory.json) tập trung provenance, hồi quy/debt và gói C4. **Safety pending tới C4**. C4 phải ghép cùng image/revision/roles, cutover có hậu điều kiện và HTTP/browser/restore không seam; không giảm forecast/checklist chỉ bằng kiểm tầng dưới. Các mốc bên dưới giữ lịch sử.

**Sau merge C3, 2026-10-13:** PR #59 đã merge `041f77ef`, tree khớp `035cef0`, source **0049**; chưa rerun tại merge. Gói B tiếp [B1 — initial receipt và hồi quy trước C4](tasks/pre-c4-auth-receipts-and-regression.md) trong một PR, rồi C4 tích hợp. B1 mới giao tài liệu, chưa triển khai/kiểm; **safety pending tới C4**. Giữ forecast/giả định và checklist, không coi merge là nghiệm thu. Các mốc review dưới đây là lịch sử.

**Review lại C3 tại `035cef0`, 2026-10-12:** hai P2/Ctrl-C release đã qua kiểm độc lập, không phát hiện điểm chặn merge mới trong scope C3; [report](reviews/pr59-c3-operator-auth-recovery.md) giữ reviewer 106 repo + 4 ca ngoài repo pass và artifact agent riêng. PR #59 vẫn OPEN/chưa merge, source `0049`. Sau merge tiếp debt/hồi quy gói B và tích hợp C4, không mở safety/pricing hoặc giảm checklist/forecast bằng suite pass; các mốc P2 dưới đây giữ lịch sử.

**Sửa review lại PR #59 tại `d2d56f3` (2026-10-12, chờ review lại, chưa merge):** Ctrl-C khi trả connection giờ là outcome theo `Attempt` (không còn “nothing was changed” sau commit), connection luôn invalidate/close. Agent: nhóm C3 106/106 có niêm hash, đối chứng 9/9, smoke API/proxy 28/28; [handoff](c3-operator-auth-recovery.md#review-lại-tại-6465dc0-ctrl-c-khi-trả-connection). **Safety pending tới C4.**

**Review lại C3 tại `6465dc0`, 2026-10-12:** còn một nhánh P2-2 (Ctrl-C trong release sau known commit báo `nothing was changed`); [report](reviews/pr59-c3-operator-auth-recovery.md) và [prompt sửa cùng PR](tasks/fix-pr59-c3-final-authority-and-release.md). P2-1/release lỗi thông thường đã qua kiểm độc lập. PR #59 vẫn OPEN/chưa merge, source `0049`; gói B đang C3, chưa mở C4 hoặc thay forecast/checklist. Safety pending; các cập nhật trước dưới đây giữ lịch sử.

**Cập nhật sửa review C3, 2026-10-12:** hai P2 PR #59 đã sửa trên cùng PR (head `0ee17f3`), chờ review lại, chưa merge; [handoff](c3-operator-auth-recovery.md#sửa-review-pr-59-2026-10-12). Gói B vẫn ở C3; debt/hồi quy toàn inventory và C4 chưa giao. Không giảm checklist hay forecast bằng suite pass. Safety pending.

**Cập nhật review C3, 2026-10-12:** PR #59 còn OPEN, source `0049`; [review](reviews/pr59-c3-operator-auth-recovery.md) có hai P2 và [prompt sửa cùng PR](tasks/fix-pr59-c3-final-authority-and-release.md). Gói B đang C3, chưa giao cutover C4 hoặc giảm checklist bằng suite pass. Safety vẫn pending; giữ forecast/giả định bên dưới, đánh giá lại tại C4 với debt và tích hợp thực tế.

Ngày chốt **2026-10-11**. Chủ dự án giao orchestrator chọn phạm vi và cách làm hợp lý để đưa dự án tới một bản dùng được. Kế hoạch này là nguồn trạng thái hiện hành; các report/handoff cũ giữ chứng cứ tại thời điểm chạy. Không suy kết quả kiểm trước merge thành kết quả mới.

## Đích phát hành

**MVP cho một nhóm nghiên cứu nhỏ, một instance, tài khoản theo lời mời.** Hoàn thành khi nhóm thử có thể dùng liên tục các luồng upload → index → tìm kiếm → hỏi có nguồn, và Admin có thể duyệt/công bố/thu hồi; xóa và restore giữ đúng quyền. PDF/Markdown, không OCR, không tự động tóm tắt paper. Các yêu cầu đã xác nhận trong [product requirements](product-requirements.md) và [access/data](access-and-data.md) vẫn giữ nguyên.

Đây là mốc nghiệm thu phần mềm và vận hành tối thiểu. Không đồng nghĩa đã đạt chất lượng production trên mọi paper, chịu mất điện, hoặc hiệu năng ở quy mô lớn.

## Trạng thái hiện tại

- PR #61 đã merge tại `fb96354c4f8dc8fb8f4f8a54f1516d9d2e13625e`; head review `3f07901` là ancestor, tree khớp. Source migration **0051**; không suy revision DB dev hoặc cộng pass mới tại merge.
- C4 đã hoàn tất code/review/kiểm cô lập: operational readiness có hậu điều kiện, image/roles đồng bộ, HTTP/browser/năm full restore drill/CLI evaluation đã thực chạy. DB chưa đủ hậu điều kiện vẫn 503; chưa cutover dev hoặc mở provider/pricing thật. M6d của gói C mới được giao.
- Citation reconcile và [discovery harvesting](c2d5f2-discovery-runtime.md) đã chuyển runtime. [Review PR #56](reviews/pr56-c2d5f2-discovery-runtime.md) giữ kết quả trước merge; chưa rerun tại merge. Docs orchestrator bị sót đã được đưa vào commit của PR #57.
- [C2d-6 search/query embedding/rerank](c2d6-search-query-rerank-runtime.md) đã merge; [review độc lập](reviews/pr57-c2d6-search-query-rerank-runtime.md) giữ chứng cứ trước merge, chưa rerun tại merge. Không suy kết quả này thành RAG runtime hoặc cutover.
- [C2d-7 RAG/generation runtime](c2d7-rag-generation-runtime.md) đã merge PR #58, source **0048**; [review độc lập](reviews/pr58-c2d7-rag-generation-runtime.md) và [inventory](c2d7-test-inventory.json) giữ chứng cứ trước merge, chưa rerun tại merge. Gói A hoàn tất chuyển runtime trong scope đã review, chưa cutover/nghiệm thu end-to-end. C3 đã merge PR #59; tiếp B1 hồi quy/debt và tích hợp C4. Private deletion/restore, provider thật và vận hành triển khai cần nghiệm thu riêng. Giữ và đưa docs review/task mới chưa commit vào PR kế tiếp.
- [C3 operator auth recovery](c3-operator-auth-recovery.md) đã merge PR #59, source **0049**: login operator riêng, case bind installation/User UUID/action/role/heads dùng một lần theo journal, TTY ẩn, maintenance hold, credential/status tách riêng; không mở disabled/ready. Kết quả trước merge theo [inventory](c3-test-inventory.json) và [review](reviews/pr59-c3-operator-auth-recovery.md); chưa có inventory/ủy quyền người thật. B1 đã merge PR #60, source `0050`; [handoff](pre-c4-auth-receipts-regression.md) và [review](reviews/pr60-pre-c4-auth-receipts-regression.md) giữ kết quả/giới hạn. C4 đã triển khai và kiểm cô lập tại source `0051`, chờ review; xem handoff/inventory C4. Dev/provider thật chưa được mở.
- Nhãn đánh giá RAG hiện còn `pending_human_review`. Charge thật chưa đối soát; không bật `pricing_verified` bằng kết quả provider giả.

## Các gói công việc còn lại

Ước lượng là **ngày làm việc**, giả định một coding agent, reviewer theo sát và khoảng một lượt sửa mỗi PR. Usage limit, hạ tầng hoặc lỗi tích hợp có thể kéo dài. Các gói có thể chồng một phần; không cộng thời gian chờ người vận hành thành thời gian coding.

| Gói | Phạm vi và đầu ra | Ước lượng | Trạng thái |
| --- | --- | --- | --- |
| A — hoàn tất runtime | Search + query embedding + rerank; RAG + generation. Giữ quyền trước retrieval/provider và kế toán hiện hành | Đã merge | PR #57/#58 đã merge; chưa cutover/nghiệm thu end-to-end |
| B — recovery và bản chạy đầy đủ | C3 CLI operator tối thiểu; xử lý debt hồi quy; C4 Compose/image/role/journal, readiness thật, HTTP/browser không patch pending, hai vòng restore/recover | Đã merge | C3/B1/C4 đã merge PR #59–#61; nghiệm thu cô lập, chưa cutover thật |
| C — đóng yêu cầu sản phẩm còn thiếu | Xóa item private + chống xuất hiện lại sau restore; nghiệm thu duplicate/retry hiện hành, chỉ bổ sung chỗ thiếu để luồng dùng được | 2–4 ngày | M6d đã giao trọn luồng xóa/cleanup/restore; duplicate/retry hồi quy và ghi gap F07, chưa triển khai/kiểm |
| D — provider và chất lượng | Smoke tổng hợp có trần chi phí, đối soát charge, routing/retention; sau đó thử paper được phép xử lý, người đọc kiểm câu trả lời/citation/abstention | 2–4 ngày | Chưa giao; chưa mở trả phí |
| E — triển khai nhóm thử | HTTPS/cookie Secure, kênh invite/reset dùng được, rate limit, CI, journal độc lập, backup/retention/alert tối thiểu và nghiệm thu browser | 3–5 ngày | Chưa giao |

**Dự kiến tại mốc kế hoạch gốc:** khoảng **2–3 tuần** để hoàn tất runtime/recovery và có bản end-to-end; khoảng **4–6 tuần tổng cộng từ mốc kế hoạch này** để nghiệm thu MVP cho nhóm nhỏ. Đây là khoảng lập kế hoạch, không phải cam kết ngày giao hay SLO. Cập nhật sau gói A và sau lần C4 đầu; nếu vượt khoảng, nêu rõ việc phát sinh và ảnh hưởng trước khi mở thêm scope.

**Cập nhật sau merge PR #56, 2026-10-11:** tổng còn lại khoảng **16–27 ngày làm việc** theo bảng, có thể chồng một phần. Giữ dự báo thực tế **4–6 tuần**, vì discovery mới giảm một phần của gói A; lỗi tích hợp, debt, restore và nghiệm thu provider/triển khai vẫn chưa được đo đầy đủ. M6 kéo dài do phạm vi đã mở sang chống rollback auth sau restore và chuyển runtime toàn hệ thống, kèm nhiều PR theo endpoint. Hai gói runtime còn lại được gom theo luồng; không thêm tính năng ngoài checklist để kéo dài đích MVP. Usage limit hoặc chờ hạ tầng/charge có thể khiến lịch vượt khoảng, khi đó báo nguyên nhân cụ thể.

**Cập nhật C4, 2026-10-16:** gói B đã hoàn tất code/kiểm cô lập, chờ review/merge. Gói C–E giữ giả định 7–13 ngày coding theo bảng, cộng review và thời gian chờ người vận hành/provider/hạ tầng; chưa xác lập ngày phát hành. Private deletion/restore, chất lượng/charge và vận hành production vẫn chưa đạt bằng các phép thử giả lập.

## Checklist nghiệm thu cố định

Mỗi mục cần một chứng cứ có commit/image, cách chạy và kết quả. Không đánh dấu đạt bằng build xanh, provider giả hoặc toàn bộ request bị 503.

- [x] **Bản cài chạy được trong scope C4 cô lập:** cài mới và nâng cấp có dữ liệu trên project cô lập; image đúng revision, runtime dùng đúng DB role; health/readiness và worker/beat ổn định sau C4. Runbook không yêu cầu người dùng tự đoán các bước pending.
- [x] **Luồng web chính với provider giả được kiểm soát:** bootstrap một lần, invite/accept/login/reset/logout; upload PDF/Markdown có xác nhận, trạng thái xử lý/coverage, tải original, collection, search keyword/hybrid, hỏi theo ba scope và mở citation; review/approve/publish/revoke. Có kiểm A/B/Admin qua proxy và trình duyệt thật.
- [ ] **Quyền và restore:** request mới sau revoke/xóa bị chặn; không lộ private giữa owner/Admin; worker/provider không công bố dữ liệu mất quyền; restore không hồi sinh common đã revoke, review grant đã đóng, auth đã thu hồi hoặc item private đã xóa trong phạm vi journal được bảo vệ. Ghi rõ giới hạn bytes đã gửi và yêu cầu journal hiện hành độc lập.
- [ ] **Tìm kiếm và trả lời có ích:** đo retrieval trên corpus hiện có; duyệt nhãn và chấm câu trả lời bằng người đọc; thử thêm một bộ paper thật có quyền sử dụng với câu một paper, so sánh, thiếu đáp án, partial và injection. Sai nguồn, lộ dữ liệu hoặc khẳng định không có chứng cứ phải được xử lý; các ngưỡng chất lượng đề xuất không tự thành gate số học.
- [ ] **Chi phí được kiểm chứng:** route/model/dimension và routing/retention được kiểm; charge thực được đối soát trên key; giới hạn User/ứng dụng, concurrent calls và uncertain reservations hoạt động. Chỉ bật từng cấu hình trả phí sau chứng cứ tương ứng, không bật toàn bộ bằng một smoke.
- [ ] **Vận hành nhóm thử:** HTTPS, cookie Secure, delivery ngoài dev outbox, rate limit login/reset, CI cho gate kiểm chính, backup có kiểm checksum, lịch/retention và diễn tập restore; journal lưu độc lập và hiện hành, có người chịu trách nhiệm recovery và alert lỗi/cleanup quá hạn. Không tuyên bố chịu mất điện bằng fault injection.
- [x] **Đóng nợ inventory API của source C4:** inventory toàn bộ test không thiếu/trùng, debt đã phân loại và xử lý; không còn lỗi nghiệp vụ/quyền đã biết nằm dưới nhãn “baseline”. Chạy hồi quy tổng sau tích hợp, không cần chạy toàn bộ mỗi PR.

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
