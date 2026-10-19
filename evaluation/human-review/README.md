# Duyệt chất lượng bằng người đọc (M7b)

| Đường dẫn | Nội dung |
| --- | --- |
| `bundle/index.html` | **Mở file này** (trình duyệt, offline): mục lục 80 nhãn M4c, 3 answer M7a có cấu trúc + 1 mẫu Chrome chỉ có text, hướng dẫn |
| `bundle/label-review.csv` | **Phiếu điền 1**: duyệt nhãn M4c (`verdict`, `reviewer`, `review_date`, `notes`) |
| `bundle/answer-review.csv` | **Phiếu điền 2**: chấm answer M7a (`human_*`, `reviewer`, `review_date`, `notes`) |
| `bundle/README.md` | Hướng dẫn và rubric đầy đủ |
| `bundle/manifest.json` | Binding: hash từng nhãn/dòng, corpus, parser, snapshot M7a; không cần gõ tay |
| `records/` | Tạo bởi `human-import`; chỉ chứa phiếu đã nhập hợp lệ. Chưa có = chưa ai duyệt |
| `report.md`, `report.json` | Tạo bởi `human-report`; hiện là `awaiting_human_review` |

Lệnh (từ gốc repo, Conda `ras`):

```powershell
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py -m app.evaluation human-validate labels evaluation\human-review\bundle\label-review.csv
& $py -m app.evaluation human-import labels evaluation\human-review\bundle\label-review.csv
& $py -m app.evaluation human-validate answers evaluation\human-review\bundle\answer-review.csv
& $py -m app.evaluation human-import answers evaluation\human-review\bundle\answer-review.csv
& $py -m app.evaluation human-report
```

Tái tạo bundle: `& $py -m app.evaluation human-prepare` (dùng lại snapshot M7a trong bundle; từ chối ghi
đè phiếu đã điền nếu không có `--force`); kiểm đồng bộ: `human-prepare --check`. Chi tiết và giới hạn ở
[handoff M7b](../../docs/m7b-human-quality-review.md).
