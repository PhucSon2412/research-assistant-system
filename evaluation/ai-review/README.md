# M7c — track đánh giá bằng AI judge (`reviewer_kind: ai_judge`)

Đây **không** phải phiếu người đọc. Track này không ghi vào `evaluation/human-review/` hay cột `human_*` của M7b
(`app/evaluation/ai_review.py` có chốt chặn, test và countercheck). M7b vẫn chỉ đếm phiếu do người điền.

- `records/`: `m4c_labels.json`, `m7a_answers.json`, `m7c_answers.json`. Mỗi record bind theo binding M7b (nhãn: label,
  corpus, parser; answer M7a: các row, response, snapshot, corpus, parser) hoặc theo snapshot run M7c, manifest paper và
  parser. Mỗi record ghi model judge, provider pin, rubric, SHA-256 của prompt, tham số, thời điểm, chi phí và slot ledger.
  Record có binding khác binding hiện hành là stale và không được tính.
- `sources/m7c-run.json`: snapshot run live (claim, quote ngắn, locator, kiểm máy, retrieval, settlement), không có text chunk.
- `report.md` / `report.json`: `python scripts/m7c_judge.py report` (offline). Chấm lại phải gọi provider qua ledger:
  `python scripts/m7c_judge.py --state <dir ngoài repo> labels|m7a|m7c ...`.

Kiểm máy (quote nguyên văn, locator, scope) đứng riêng; judge không ghi đè. Chi tiết:
[handoff M7c](../../docs/m7c-live-quality-verification.md).
