# M7c — gói D: xác minh live, paper thật có giấy phép mở và đánh giá bằng AI

Bạn là coding agent tiếp theo. Triển khai **một PR hoàn chỉnh** khép phần còn lại của **gói D** trong [kế hoạch MVP](../mvp-delivery-plan.md), gồm:

- xác minh live phần settlement cache-write trên source cuối;
- chạy pipeline thật (embedding/rerank/generation qua OpenRouter) trên một bộ paper thật có giấy phép mở;
- đánh giá retrieval/câu trả lời/citation/abstention bằng **AI judge có nhãn rõ ràng**;
- đề xuất route nào đủ chứng cứ để bật `pricing_verified` khi cutover.

## 1. Base và ủy quyền mới của chủ dự án (2026-10-21)

- PR #68 đã merge: đã fetch/xác minh `origin/main = 7009a8483e4f9891f11a966f114df136c90c4953` (parent `e0dcdb4` + head review `36444ae`), tree khớp `4fa14a7`. Source **0052**.
- Fetch lại, tạo `feat/m7c-live-quality-verification` từ `origin/main`. Mang theo mọi docs orchestrator/reviewer modified/untracked (report review PR #68, task này, cập nhật trạng thái) vào PR; không reset/stash/discard.
- **TẠM DỪNG (cập nhật 2026-10-21): chưa giao.** Chỉ bắt đầu khi chủ dự án báo đã có key **thuộc tài khoản OpenRouter do chính chủ dự án tạo và quản lý**. Key OpenRouter đang có trên máy không thuộc tài khoản của chủ dự án: **không đọc, không dùng** cho bất kỳ lượt live, test hay stack nào.
- **Ủy quyền của chủ dự án (2026-10-21):**
  - nhờ orchestrator duyệt policy và chọn paper;
  - nhờ đánh giá thay phiếu người đọc.

  Orchestrator chốt cách thực hiện ở mục 2–4. Đây là lựa chọn của orchestrator theo ủy quyền, ghi đúng nguồn trong decisions.
- **Key:**
  - chỉ dùng key mới của chủ dự án, nhận qua file secret chủ dự án tự tạo ngoài repo (agent không đọc `.env`);
  - truyền vào stack thử qua file secret 0600 trong context ngoài repo;
  - không in key/header/cookie.
  - Cài đặt logging/training của tài khoản do chủ dự án tự đặt trên tài khoản của mình.
- **Trần chi phí của task** (orchestrator đặt để chặn vòng lặp lỗi, không phải giới hạn của chủ dự án):
  - ≤ **$5,00** tổng (gồm judge) và ≤ **400** inference call; ledger fail-closed như M7a;
  - chạm 80 % thì dừng gọi mới và báo cáo.
  - Spending cap $50/ngày của key và budget ứng dụng $10/ngày giữ nguyên.
- Vẫn không đụng dev `ras`/Dify, không gửi mail thật, không cutover, không đổi config pilot. `pricing_verified=true` chỉ bật trong stack thử cô lập của task này.

## 2. Policy provider đã duyệt (orchestrator, theo ủy quyền)

- Giữ ba model ID chủ dự án đã chọn cho embedding/rerank/generation, không tự đổi.
- Route pin provider ZDR như M7a: `routing.only`, không fallback, `data_collection=deny`, `zdr`, `max_price`. Route nào không có provider ZDR khả dụng thì **không** dùng cho dữ liệu thật; ghi rõ và báo.
- Chỉ gửi nội dung paper có giấy phép cho phép xử lý/phân phối lại (mục 3). Không dùng paper riêng của người dùng thật.
- Ghi vào handoff những gì không kiểm được qua API. Ví dụ: cài đặt logging/training của tài khoản OpenRouter là việc chủ dự án tự xác nhận trên trang cài đặt; không suy là đã tắt.

## 3. Bộ paper thật có giấy phép mở

- Chọn **10–15 paper arXiv** cùng một chủ đề gần sản phẩm (ví dụ retrieval-augmented generation / đánh giá RAG / tìm kiếm khoa học).
  - Chỉ nhận version có license **CC BY 4.0, CC BY-SA 4.0 hoặc CC0**, kiểm theo metadata arXiv của **đúng version** tải về.
  - Không dùng license mặc định arXiv (non-exclusive distribution), bản có điều kiện NC/ND, hoặc paper không rõ license.
- Tải PDF qua arXiv (cho phép network tới arXiv trong task này, nhịp lịch sự, có User-Agent mô tả). Ghi arXiv ID, version, license URL, SHA-256, kích thước, ngày tải.
- **Không commit PDF hay toàn văn**. Repo chỉ giữ:
  - manifest (ID/version/license/attribution/SHA-256);
  - script tải lại có kiểm checksum;
  - câu hỏi kèm quote ngắn có ghi nguồn.
- Bộ câu hỏi **30–40 câu**, do agent soạn, nhãn `authored_by: ai_agent`:
  - một paper, so sánh nhiều paper, không có đáp án, partial;
  - injection: chuỗi injection trong câu hỏi, hoặc một tài liệu tổng hợp riêng chèn kèm; **không sửa paper thật**.
  - Mỗi câu có gold evidence: quote nguyên văn kiểm được trên text parser production, trang/locator.
  - Tách holdout như M4c.
- PDF không đạt ngưỡng body/không trích xuất được thì ghi đúng trạng thái sản phẩm (không OCR), không loại khỏi báo cáo để đẹp số.

## 4. Đánh giá bằng AI judge, không phải phiếu người đọc

- **Không ghi vào cột/record `human_*` của M7b**, không điền `label-review.csv`/`answer-review.csv`, không đặt reviewer như người thật.
- Tạo track riêng (ví dụ `evaluation/ai-review/`):
  - record bind theo cùng binding M7b (nhãn/corpus/parser hoặc row/snapshot);
  - ghi `reviewer_kind: ai_judge`, model ID, provider, prompt hash, tham số, thời điểm, chi phí.
  - Report tách hẳn khỏi human report; M7b vẫn hiển thị 0 verdict người thật.
- Judge dùng **một model khác họ với model generation**, chọn qua OpenRouter dưới cùng policy ZDR. Ghi lý do chọn, chạy một mẫu nhỏ lặp lại để đo độ ổn định (tỉ lệ đồng thuận giữa hai lượt).
- Phạm vi chấm:
  1. **80 nhãn M4c**: gold evidence có đủ/đúng cho câu hỏi và kỳ vọng không. 7 câu `agent_note` là lựa chọn sản phẩm, chỉ ghi nhận xét, không đổi kỳ vọng.
  2. **3 answer M7a** có cấu trúc.
  3. Toàn bộ **answer live trên bộ paper thật**: claim có được quote hỗ trợ, citation đúng version/file/locator, abstention đúng ở câu thiếu đáp án, không làm theo injection, không lộ dữ liệu ngoài scope.
- Kiểm máy luôn đi kèm judge: quote nguyên văn, locator, ACL/scope. Judge không ghi đè kết quả kiểm máy.
- Đo retrieval (Recall@5 theo gold) trên bộ paper thật với embedding/rerank thật.
- Báo số theo mẫu số rõ ràng. Ngưỡng trong [nonfunctional](../nonfunctional.md) là mục tiêu đề xuất, không tự thành gate.

## 5. Xác minh live chi phí

- Trên source cuối và image build từ đó, chạy live trong stack thử cô lập (C4/M8 harness, project/volume/journal riêng):
  - ít nhất một lượt generation có **cache write** và một lượt **cache read**;
  - đối soát từng generation ID qua `/generation` với settlement ứng dụng.
- Settlement khác charge thật thì sửa hẹp + regression, không nới công thức để khớp.
- Kiểm budget/quota: `usage_daily`, giới hạn User/ứng dụng, concurrent, reservation uncertain trên đúng đường live.
- Kết luận theo **từng route** (embedding document/query, rerank, generation): đủ chứng cứ bật `pricing_verified` hay chưa, kèm lý do. Không bật trong config pilot ở PR này.

## 6. Nền kiểm chứng (hai P3 từ review PR #68)

- `scripts/m8a_test_host.py` và `scripts/c3_test_host.py` dùng **cùng digest PostgreSQL/Redis** đã pin trong `deploy/compose.yaml` (đọc từ compose như cách đã làm cho MinIO), để CI và deploy kiểm cùng bytes.
- Thêm `tests/test_evaluation.py` (và test mới của task) vào selection CI. Chặn mọi HTTP ngoài trong CI như cũ; test live chỉ chạy tay, không trong CI.

## 7. Kiểm chứng và bàn giao

- Pytest không gọi provider/arXiv thật. Live chỉ chạy qua script riêng có ledger; JUnit/artifact được quét không có key/token/toàn văn.
- Countercheck nhắm:
  - ghi nhầm vào cột human;
  - judge không bind;
  - nhận paper sai license;
  - vượt trần chi phí;
  - settlement lệch charge.
- Một PR tiếng Việt, không merge, giữ author/committer, không AI co-author/trailer/promo. Handoff `docs/m7c-live-quality-verification.md` + inventory gồm:
  - danh sách paper/license;
  - số đo retrieval/answer/judge có mẫu số;
  - chi phí theo call và tổng;
  - kết luận từng route `pricing_verified`;
  - giới hạn: AI judge không thay người đọc; ZDR theo cam kết provider; account setting do chủ dự án tự xác nhận.
- Cập nhật kế hoạch MVP: checklist "Tìm kiếm và trả lời có ích" và "Chi phí được kiểm chứng" ghi rõ phần nào đạt bằng AI judge/live, phần nào còn cần người thật.

Docker inventory trước/sau, chỉ dọn tài nguyên của task sau kiểm label/path. Không `taskkill` theo tên process.

**Inventory khi giao:** chỉ đọc docs/Git, fetch xác minh merge/tree PR #68; chưa đọc key, chưa gọi provider/arXiv, chưa chạy app/test. Task **mới giao/chưa triển khai/kiểm**.
