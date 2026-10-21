# Đánh giá bằng AI judge (M7c) — 2026-10-21

**reviewer_kind: `ai_judge`** — ai_judge: đánh giá do một mô hình ngôn ngữ tạo theo rubric có phiên bản; không phải phiếu người đọc, không thay M7b và không chứng nhận chất lượng.

Rubric nhãn `m7c-judge-rubric/1`, answer `m7c-judge-rubric/2-answers`. M7b (người thật): 0 record nhãn, 0 record answer — không thay đổi bởi track này.

## Judge

- `anthropic/claude-sonnet-5.5` qua `amazon-bedrock`, rubric `m7c-judge-rubric/1`: 8 call, $0.418034 billed
- `anthropic/claude-sonnet-5.5` qua `amazon-bedrock`, rubric `m7c-judge-rubric/2-answers`: 13 call, $0.156484 billed

## Nhãn M4c (80 câu, agent soạn)

Đã chấm 80/80, record stale 0. Phân bố: {'adequate': 71, 'needs_change': 2, 'comment_only': 7}. Adequate trên nhãn không phải lựa chọn sản phẩm: 71/73 = 0.9726. Ổn định hai lượt: 8/8 = 1.0000 (8 cặp).

- `CMP-09` needs_change (expectation yes, evidence no): The replication reports two dense baselines: 26.8 in the main run and 25.6 with the original tokenizer settings. Gold cites only 26.8. Add the 25.6 sentence and mention it in the claim, since it explains the gap.
- `CMP-12` comment_only (expectation yes, evidence not_applicable): The insufficient status fits, since the GSR paper has no Swahili results. The reason code 'comparison_requires_multiple_papers' is a poor fit: both papers are owned and in scope, and one side simply lacks the data.
- `CMP-14` comment_only (expectation yes, evidence not_applicable): Refusing is right: B/P21 is private and the ZQXB5582 marker is guarded. The reason label is debatable, since the blocker is an inaccessible paper rather than a missing multi-paper comparison. Product choice, no judgement.
- `CMP-15` comment_only (expectation yes, evidence not_applicable): Only P03 reports agreement (kappa 0.87); P09 reports none. The reason label comparison_requires_multiple_papers fits loosely; no_supporting_evidence might fit better. Product choice, no judgement.
- `RCI-01` comment_only (expectation yes, evidence not_applicable): Product choice. The strict contract (references are not content) gives insufficient_evidence. A bibliographic mode could reasonably return the entry, labelled as a reference. Not judging the choice.
- `RCI-02` comment_only (expectation yes, evidence not_applicable): Product choice. Findings of the cited work appear nowhere in the corpus, so declining is consistent under any bibliographic policy. Not judging the choice.
- `RCI-04` comment_only (expectation yes, evidence not_applicable): Product choice. The survey only cites the GSR paper, so insufficient_evidence for this scope is reasonable. The GPU-hours sit in A/P01, which user_a owns but which is out of scope.
- `RCI-06` comment_only (expectation yes, evidence not_applicable): Product choice. The 2.4 vs 0.6 BLEU conflict is real, and P04 blames a tokenizer mismatch without settling it. Declining or showing both attributed figures are both defensible.
- `RCI-09` needs_change (expectation yes, evidence no): The quote covers only 'GNN reaches MAE 0.031'. The claim's physics-model 0.036 and the question's seven-day horizon come from the rest of the abstract sentence. Extend the span or trim the claim.

## Answer M7a (tổng hợp, 3 response có cấu trúc)

- Response đã chấm: 3; claim 2; record stale 0.
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
