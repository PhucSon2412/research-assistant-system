# Sửa PR #65 — hiệu lực verdict và nội dung bundle đã được xem

**Đã kiểm lại tại `8799216` (2026-10-19): hai P2 đã sửa, không phát hiện điểm chặn merge mới trong scope M7b.** Reviewer 68 repo + 7 repro độc lập pass; [kết luận hiện hành](../reviews/pr65-m7b-human-quality-review.md#review-lại-tại-8799216--hai-p2-đã-sửa). Nội dung dưới là prompt sửa lịch sử tại `44e3bd3`, không giao thêm việc coding. PR chưa merge; 0 verdict thật, `awaiting_human_review`, chưa nghiệm thu chất lượng D.

Sửa **trên cùng PR #65**, head review **`44e3bd3`**, source **0052**. Đọc [report độc lập](../reviews/pr65-m7b-human-quality-review.md), [task gốc](m7b-human-quality-review.md) và [handoff](../m7b-human-quality-review.md). Giữ mọi docs reviewer/orchestrator modified/untracked vào PR, không discard hoặc sửa lịch sử chứng cứ.

Reviewer đã chạy 55 test repo + 1 control độc lập pass, sáu repro fail thuộc **hai P2** dưới đây. Chỉ sửa evaluation/bundle/CLI/test/docs; không thêm production permission, DB migration hoặc hệ thống phê duyệt/ký số.

## 1. Report phải loại verdict không còn bind đúng nguồn/parser

- **M4c:** sau nhập acceptance fixture cho 80 câu, thay bytes P01 nhưng giữ manifest → JSON nhận `corpus_errors` song vẫn `accepted_all`, accepted 80/stale 0/bundle_current true. Chặn hiệu lực record khi nguồn thiếu/sai checksum/size; không chỉ trả danh sách lỗi bị Markdown bỏ qua. Ràng buộc theo corpus hiện có phải phản ánh bytes đã kiểm, không chỉ hash khai báo của manifest.
- **M7a:** đổi parser identity trong khi row text không đổi → report vẫn `reviewed`, 4 live record và grounded/citation/abstention 100%. Dựng lại bundle mới theo parser đó vẫn tái dùng record cũ. Bind record/hash với snapshot + corpus + parser đầy đủ, gồm cả row không claim/citation. Không vá bằng `bundle_current=false` rồi vẫn tính số; rebuild không tự reapprove.
- Corpus M7a đổi làm `corpus_matches_run=false` nhưng abstention vẫn 1/1 vì row không chứa quote. Không dùng verdict lịch sử cho source không khớp run; thiếu nguồn/binding phải stale/unreviewed/not measurable rõ. Mọi mẫu số live phải đi qua cùng kiểm hiệu lực, không chỉ grounded.
- Giữ record cũ để truy vết, không xóa phiếu/attribution và không tự bổ sung binding mới để biến chúng thành đã được duyệt. Record cũ thiếu binding mới phải fail closed hoặc stale, không mặc định tương thích bằng chứng giả. Phân biệt record cũ với một phiếu thật được người đọc nhập lại sau khi xem nguồn hiện hành.
- Report JSON/Markdown/overall status nhất quán: không `accepted_all`/`reviewed` khi evidence binding không hợp lệ, không chỉ gắn `provisional` mà giữ tỷ lệ từ verdict stale. Mẫu số 0 là null/not measured, không 0% hoặc 100%.

## 2. Validate/import phải kiểm bundle được hiển thị

- Manifest đã có hash HTML/bản đọc/file nguồn. Hiện hai import không kiểm chúng: đổi gold span trong `labels/SP-01.html` vẫn ghi 80 record; đổi context trong `answers/01-single.html` vẫn ghi bốn record, errors rỗng.
- Thêm preflight dùng chung ở đường `human-validate`/`human-import`, kiểm các file hiển thị/nguồn/snapshot liên quan tồn tại và đúng nội dung/hash. Có thể tái dùng kiểm bundle hiện có; không để người dùng phải nhớ chạy một lệnh khác để có bảo vệ.
- Mismatch/missing phải từ chối trước mutation, không ghi một phần hoặc sửa record đã có. Cột human hợp lệ được phép điền/thay đổi theo hợp đồng; không hash nguyên CSV rồi từ chối mọi phiếu được điền. Giữ phát hiện missing/duplicate/edited display và hỗ trợ nộp từng phần (mọi row vẫn hiện diện).
- Không cần chữ ký số hoặc identity verification, không bắt chủ dự án tự xác nhận hash. Không biến máy kiểm structural thành người phán đoán ngữ nghĩa.

## 3. Kiểm chứng gọn nhưng đủ

- Reuse repro ngoài repo `C:/Users/ADMIN/AppData/Local/Temp/ras-pr65-review-20261019/test_bindings.py`; đưa regression phù hợp vào repo. Mọi verdict/reviewer giả chỉ trong fixture/temp, không ghi vào bundle/records thật.
- Bao các case: control không đổi; raw corpus bytes đổi mà manifest chưa đổi; parser đổi trước và sau rebuild; corpus M7a lệch run, nhất là row abstention; file HTML/context/raw source bị sửa/thiếu cho cả label và answer; phiếu điền từng phần hợp lệ vẫn nhận; import lỗi giữ bytes record cũ.
- Kiểm JSON/Markdown/status và **mẫu số** thật, không chỉ assert khác mã lỗi hoặc có cảnh báo stale. Giữ các test grounded/citation/conflict/response ID/formula/HTML escape hiện có.
- Chạy lại 55 test offline reviewer đã dùng cùng regression mới; node ID deselect là `tests/test_evaluation.py::...` theo root thực, không tiền tố `api/tests/...`. Dùng cấu hình test tường minh và chặn DB/HTTP thật. Giữ source hashes/JUnit/exit và mọi lượt fail/rerun/not-run riêng.
- Không rerun full >5.000/DB/MinIO/web/restore/live chỉ để sửa những nhánh offline này. Browser local chỉ cần nếu đổi render/link/escaping, không mạng ngoài.

## 4. Bàn giao

Không đọc key/`.env`, provider/account, ledger mutation, dev `ras` hoặc Dify; không inference, đổi pricing/model/adapter/ranking, grant/migration/readiness. Trần M7a cũ đã hết; không cần ngân sách mới cho bản sửa.

Commit/push cùng PR, cập nhật mô tả PR tiếng Việt/handoff/inventory và trạng thái hiện hành; giữ author/committer, không AI trailer/promo, không merge. Các phiếu thật hiện còn trống; vẫn `awaiting_human_review`/0 verdict thật. Sau review đạt mới bàn giao người đọc nhập phiếu; không gọi gói D hoàn tất bằng test fixture.
