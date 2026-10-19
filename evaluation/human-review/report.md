# Báo cáo duyệt chất lượng M7b — 2026-10-19

Trạng thái chung: **awaiting_human_review**. Attribution: self_reported: reviewer và review_date do người điền phiếu tự ghi; không phải danh tính hay chữ ký đã được xác thực độc lập.

## Nhãn M4c (80 câu, tách khỏi answer M7a)

Trạng thái: **awaiting_human_review** — kết quả retrieval M4c vẫn provisional cho tới khi mọi nhãn hiện hành được accepted.

| Trạng thái | Số câu |
| --- | ---: |
| accepted | 0 |
| changes_requested | 0 |
| rejected | 0 |
| conflicting | 0 |
| unreviewed | 80 |

Tổng 80 câu, holdout 16; record cũ (stale) 0; bundle khớp nguồn hiện tại: True.

| Split | accepted | changes_requested | rejected | conflicting | unreviewed |
| --- | ---: | ---: | ---: | ---: | ---: |
| dev | 0 | 0 | 0 | 0 | 64 |
| holdout | 0 | 0 | 0 | 0 | 16 |

| Nhóm | accepted | changes_requested | rejected | conflicting | unreviewed |
| --- | ---: | ---: | ---: | ---: | ---: |
| single_paper | 0 | 0 | 0 | 0 | 20 |
| comparison | 0 | 0 | 0 | 0 | 15 |
| no_answer | 0 | 0 | 0 | 0 | 15 |
| partial_pdf | 0 | 0 | 0 | 0 | 10 |
| permission | 0 | 0 | 0 | 0 | 10 |
| references_conflict_injection | 0 | 0 | 0 | 0 | 10 |

## Answer M4c

**not_measured** — các lượt M4c chỉ capture payload generation và trả claims rỗng; 80 câu chưa có câu trả lời generation nào để chấm.

## Answer live M7a (corpus tổng hợp riêng)

Trạng thái: **awaiting_human_review**, run `m7a-runtime-2026-10-18`, bundle khớp: True. Response: 4 (chấm được 3), dòng phiếu 4, record hiện hành 0, record cũ 0.

| Chỉ số | Tử/mẫu (giá trị) |
| --- | --- |
| Grounded answer rate | 0/0 (not measured) |
| Citation accuracy | 0/0 (not measured) |
| Abstention appropriateness | 0/0 (not measured) |

Response answered: complete 0, incomplete 0, conflicting 0, unreviewed 2; abstention: graded 0, conflicting 0, unreviewed 1.

| Nhóm | Grounded | Citation | Abstention |
| --- | --- | --- | --- |
| comparison | 0/0 (not measured) | 0/0 (not measured) | 0/0 (not measured) |
| no_answer | 0/0 (not measured) | 0/0 (not measured) | 0/0 (not measured) |
| single_paper | 0/0 (not measured) | 0/0 (not measured) | 0/0 (not measured) |

Không chấm: `m7a-runtime-2026-10-18/chrome` (screen_text_only).

Kiểm máy (cấu trúc, không phải phán đoán human): machine_file_binding=derived:drill_upload_plan_user_a: 2, machine_file_binding=recorded:targets.json: 1, machine_id_in_evidence=not_available: 3, machine_locator_covers_gold=not_available: 3, machine_quote_verbatim=yes: 3.

| Response | Status | Model / endpoint / provider | Generation ID |
| --- | --- | --- | --- |
| `m7a-runtime-2026-10-18/01-single` | answered | openai/gpt-6-sol / openai/gpt-6-sol-20260922@Azure / Azure | gen-1792338280-Wczvxm6IBWIISrNsCxwd |
| `m7a-runtime-2026-10-18/02-compare` | answered | openai/gpt-6-sol / openai/gpt-6-sol-20260922@Azure / Azure | gen-1792338291-04PzOzOx0pNtrHYFicTL |
| `m7a-runtime-2026-10-18/03-insufficient` | insufficient_evidence | openai/gpt-6-sol / openai/gpt-6-sol-20260922@Azure / Azure | gen-1792338304-Pbu6EsSOm4F19wGVpEao |
| `m7a-runtime-2026-10-18/chrome` | screen text | openai/gpt-6-sol / openai/gpt-6-sol-20260922@Azure / Azure | gen-1792338349-Xl29fXhA96mlZsT73PHT |

## Giới hạn

- M4c: 15 paper tổng hợp, 87 chunk, nhãn do agent soạn; không đại diện paper thật.
- M7a: corpus tổng hợp riêng 2 tài liệu, vài answer live của một run (keyword, user A); không có gold label và không lưu evidence pack.
- Các số ở đây không là chứng nhận production, quality SLO hay CI gate.
- Tên reviewer do người điền phiếu tự ghi, không phải danh tính đã xác thực.
