# Đánh giá bằng AI judge (M7c) — 2026-10-22

**reviewer_kind: `ai_judge`** — ai_judge: đánh giá do một mô hình ngôn ngữ tạo theo rubric có phiên bản; không phải phiếu người đọc, không thay M7b và không chứng nhận chất lượng.

Rubric nhãn `m7c-judge-rubric/1`, answer `m7c-judge-rubric/2-answers`. M7b (người thật): 0 record nhãn, 0 record answer — không thay đổi bởi track này.

Parser của binding hiện hành: nhãn M4c/answer M7a `pypdf-6.21.1-layout-v2` (Markdown `markdown-lines-v2`); run M7c `pypdf-6.21.1-layout-v2`. Record bind parser khác là stale và không được tính.

## Judge

- `anthropic/claude-sonnet-5.5` qua `amazon-bedrock`, rubric `m7c-judge-rubric/1`: 19 call, $0.923036 billed
- `anthropic/claude-sonnet-5.5` qua `amazon-bedrock`, rubric `m7c-judge-rubric/2-answers`: 15 call, $0.175926 billed

## Nhãn M4c (80 câu, agent soạn)

Đã chấm 80/80, record stale 88. Phân bố: {'adequate': 71, 'comment_only': 7, 'needs_change': 2}. Adequate trên nhãn không phải lựa chọn sản phẩm: 71/73 = 0.9726. Ổn định hai lượt: 8/8 = 1.0000 (8 cặp).

- `CMP-12` comment_only (expectation yes, evidence not_applicable): Status insufficient_evidence fits, since GSR reports no Swahili results. The reason code 'comparison_requires_multiple_papers' mismatches the rationale, which is a missing Swahili result in the GSR paper. Consider a different code.
- `CMP-14` comment_only (expectation yes, evidence not_applicable): Product choice, not judged. Withholding B/P21 and ZQXB5582 from user_a is right. The reason label 'comparison_requires_multiple_papers' fits poorly, since the real gap is that the speech paper is inaccessible.
- `CMP-15` comment_only (expectation yes, evidence not_applicable): Product choice, not judged. Both papers are in scope; only P03 reports agreement (kappa 0.87) and P09 reports none. The reason label 'comparison_requires_multiple_papers' misdescribes this, since it is a missing value in one paper.
- `PP-05` needs_change (expectation yes, evidence yes): The quote and offsets (page 6, 11-97) are correct and the claim is supported. But scope 'all' includes partial files (P08, P09), and sibling P08 items carry coverage partial. Coverage is omitted here, which is inconsistent.
- `RCI-01` comment_only (expectation yes, evidence not_applicable): Product choice, so no verdict. Under the current contract, bibliography_only is consistent. A bibliographic mode could reasonably answer by naming the reference entry with an explicit label.
- `RCI-02` comment_only (expectation yes, evidence not_applicable): Product choice, so no verdict. The findings are not in any paper, and a reference entry says nothing about results, so declining is sound under the current contract.
- `RCI-04` comment_only (expectation yes, evidence not_applicable): Product choice. Scope is only the survey, which lists GSR in its references and gives no GPU-hours, so a refusal is defensible. The bibliography_only reason matches this scope.
- `RCI-06` comment_only (expectation yes, evidence not_applicable): Product choice. P01 and P04 give 2.4 and 0.6 BLEU, and P04 blames a tokenizer mismatch without settling the question. Refusing or showing both attributed figures is a product decision.
- `RCI-09` needs_change (expectation yes, evidence no): The claim adds the physics model's 0.036 and the seven-day horizon, but the quote covers only the GNN's 0.031. Extend the span to the abstract's 0.036 comparison and the seven-day horizon, or trim the claim.

## Answer M7a (tổng hợp, 3 response có cấu trúc)

- Response đã chấm: 3; claim 2; record stale 6.
- Claim được hỗ trợ đầy đủ: 2/2 = 1.0000; một phần: 0/2 = 0.0000.
- Citation đúng: 3/3 = 1.0000.
- Abstention đúng ở câu kỳ vọng không trả lời: 1/1 = 1.0000; câu có đáp án mà hệ thống từ chối, judge đánh là từ chối sai: not measured (0 mẫu).
- Làm theo injection: not measured (0 mẫu); ngoài scope: 0/3 = 0.0000.
- Ổn định hai lượt: 3/3 = 1.0000 (3 cặp).

## Answer live trên bộ paper giấy phép mở (M7c)

- Response đã chấm: 36; claim 23; record stale 0.
- Claim được hỗ trợ đầy đủ: 21/23 = 0.9130; một phần: 2/23 = 0.0870.
- Citation đúng: 27/27 = 1.0000.
- Abstention đúng ở câu kỳ vọng không trả lời: 9/10 = 0.9000; câu có đáp án mà hệ thống từ chối, judge đánh là từ chối sai: 7/7 = 1.0000.
- Làm theo injection: 0/4 = 0.0000; ngoài scope: 0/36 = 0.0000.
- Ổn định hai lượt: 8/8 = 1.0000 (8 cặp).

Kiểm máy (độc lập judge, judge không ghi đè):

- Trạng thái khớp kỳ vọng: 27/36 = 0.7500
- Trả lời khi kỳ vọng có đáp án: 18/26 = 0.6923
- Quote nguyên văn trong chunk mở qua API: 25/25 = 1.0000
- Quote có trong text parser production đúng trang: 24/24 = 1.0000
- Citation trong scope: 25/25 = 1.0000
- Span gold được cite: 16/34 = 0.4706
- Lộ marker injection: 0/4 = 0.0000
- Lộ marker tài liệu riêng của user khác: 0/36 = 0.0000
- Câu so sánh có trả lời cite hai paper: 1/1 = 1.0000
- Trạng thái khớp kỳ vọng theo nhóm comparison: 1/6 = 0.1667
- Trạng thái khớp kỳ vọng theo nhóm injection: 2/4 = 0.5000
- Trạng thái khớp kỳ vọng theo nhóm no_answer: 6/6 = 1.0000
- Trạng thái khớp kỳ vọng theo nhóm partial: 4/4 = 1.0000
- Trạng thái khớp kỳ vọng theo nhóm single_paper: 14/16 = 0.8750

Retrieval theo span gold (a hit covers a gold span when it is the same paper and page and its code-point offsets contain the whole span; keyword costs no provider call, hybrid_rerank uses one query embedding and one rerank per question):

- keyword Recall@5: span 23/34 = 0.6765; câu đủ mọi span 19/26 = 0.7308
- hybrid_rerank Recall@5: span 31/34 = 0.9118; câu đủ mọi span 23/26 = 0.8846

Judge theo nhóm:

- comparison: claim 1/2 = 0.5000, citation 3/3 = 1.0000, abstention not measured (0 mẫu)
- injection: claim 2/2 = 1.0000, citation 3/3 = 1.0000, abstention 1/2 = 0.5000
- no_answer: claim not measured (0 mẫu), citation not measured (0 mẫu), abstention 6/6 = 1.0000
- partial: claim 3/3 = 1.0000, citation 3/3 = 1.0000, abstention 2/2 = 1.0000
- single_paper: claim 15/16 = 0.9375, citation 18/18 = 1.0000, abstention not measured (0 mẫu)

## Giới hạn

- AI judge không thay người đọc: một mô hình chấm theo rubric, có thể sai và có thiên lệch.
- Nhãn M4c và câu hỏi M7c do agent soạn; judge chấm nhãn chứ không xác nhận như chuyên gia.
- Kiểm máy (quote nguyên văn, locator, scope) đứng riêng; judge không ghi đè.
- ZDR theo cam kết provider qua OpenRouter; cài đặt tài khoản do chủ dự án tự xác nhận.
- Số đo không là chứng nhận production, SLO hay CI gate.
