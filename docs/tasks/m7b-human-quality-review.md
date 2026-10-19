# M7b — bộ duyệt chất lượng và nhập phiếu người đọc, không thêm inference

Bạn là coding agent tiếp theo. Triển khai **một PR hoàn chỉnh** để chủ dự án có thể mở tài liệu, duyệt nhãn và chấm câu trả lời đã có, rồi nhập lại phiếu để tính kết quả đáng tin cậy. Đây là phần coding/bàn giao của M7b; chưa có phiếu thật thì kết luận phải là **`awaiting_human_review`**, không gọi M7b hoặc gói D đã nghiệm thu.

## 1. Base và giới hạn

- Repo `C:/Users/ADMIN/Downloads/Code/research-assistant-system`. Fetch lại và xác minh PR #64 đã merge: mốc đã kiểm `origin/main = 2df77d0ac2b3e9371aa33753f4ab591fb09d1fa3`, head review `285fd37` là ancestor, tree khớp. Không pin mốc cũ nếu main đã có commit mới. Tạo nhánh `feat/m7b-human-quality-review` từ main đã xác minh; source migration hiện **0052**.
- Đọc `AGENTS.md`, `docs/mvp-delivery-plan.md`, `docs/rag-evaluation.md`, `docs/m7a-openrouter-live-verification.md`, report PR #64 và các module `app.evaluation`. Giữ toàn bộ docs reviewer/orchestrator modified/untracked; đưa vào PR, không discard hoặc ghi đè chứng cứ cũ.
- **Không đọc key/`.env`, gọi inference/arXiv/provider, polling account, hoặc thay pricing/account setting.** Trần M7a đã hết: 20 request/$0,11675006 cam kết; 19 physical call/$0,01941006 billed là lịch sử đã audit. Không reset ledger, tự cấp budget mới, rerun live, hoặc dùng fake answer để bù ô thiếu.
- Không tác động stack/dữ liệu dev `ras` hoặc Dify. Giữ C4 readiness và toàn bộ ACL/outcome/accounting/routing. Không đổi production retrieval, prompt, pack, provider adapter, model, grants hay migration để làm điểm đẹp hơn. Dùng Conda `ras`, không tạo venv.

## 2. Đầu ra người đọc dùng được ngay

Tái dùng CLI và cấu trúc `api/app/evaluation/`, tránh framework đánh giá hoặc web app mới. Hoàn thành luồng **prepare → người đọc điền phiếu → validate/import → score/report** bằng CLI; tên lệnh chọn theo CLI hiện có và ghi đúng lệnh thực chạy.

Chuẩn bị một bundle local tự đủ nguồn/context, có trang mục lục HTML đọc offline và phiếu CSV/JSON có thể điền. Không cần backend/route/tài khoản mới. Hiển thị câu hỏi, nguồn và vị trí đọc thuận tiện; provenance/hash nằm trong manifest hoặc phụ lục, không bắt người chấm gõ hash bằng tay. Nội dung paper/câu hỏi/claim phải được escape, không chạy HTML/script/link ngoài do corpus chèn vào; nếu có CSV thì xử lý text có thể thành công thức khi mở bằng bảng tính.

### A. Duyệt đủ 80 nhãn hiện có

- Mỗi câu có ID, dev/holdout, group, identity/scope, câu hỏi, expected status, từng phía evidence và quote/locator, non-evidence cùng lý do, supported claim/rationale hiện có. Kèm text context thực từ parser production và đường mở file/trang PDF tương ứng; không bắt người đọc tra offset thủ công. Giữ đủ 80 câu/16 holdout và các nhóm hiện tại.
- Phiếu nhãn tách khỏi phiếu chấm answer: verdict `accepted`, `changes_requested`, `rejected`; reviewer, ngày duyệt, ghi chú/chỗ cần sửa. Chủ dự án là người nhận bộ duyệt mặc định, không tự điền tên hoặc xác nhận thay họ. Agent có thể ghi đề xuất ở trường riêng, không biến thành phán đoán human.
- Không chỉ đổi `label_status` hàng loạt để schema nhận “đã duyệt”. Giữ dữ liệu gốc/baseline; thêm record/overlay review nhỏ bind **nội dung đầy đủ của câu/nhãn, corpus checksum và parser version**. Import chỉ công nhận phiếu hợp lệ đã cung cấp; không cần chữ ký số hoặc hệ thống identity mới, nhưng phải ghi đây là attribution người điền, không xác thực danh tính độc lập.
- Thiếu reviewer/ngày/verdict, ID lạ/trùng, verdict mâu thuẫn, hoặc nội dung/corpus/parser đã thay đổi phải thành lỗi hoặc pending rõ ràng; không áp một phần rồi mới phát hiện sai. `changes_requested` không tự thành accepted sau khi agent sửa. Giữ dev/holdout; không dùng holdout để tuning production.
- Nếu hiệu lực nhãn được tính từ overlay, report tách accepted/needs changes/rejected/unreviewed; kết quả chưa đủ duyệt vẫn provisional. Không ghi đã duyệt 80 khi mới có vài phiếu.

### B. Chấm câu trả lời live M7a đã có

- Nguồn tổng hợp sẵn: `C:/Users/ADMIN/AppData/Local/Temp/ras-m7a-20261018/state/runtime/answers.json`; corpus trong `evaluation/m7a-synthetic/`; ledger/receipts redacted và inventory M7a để đối chiếu provenance khi cần. `chrome-answer.txt` cùng thư mục là chứng cứ bổ sung, không tự chuyển text màn hình thành claims/citations có cấu trúc đầy đủ.
- Chỉ đọc file kết quả/provenance cần thiết. Không quét `.env`, key, account/session/config secret, ảnh chụp hoặc toàn bộ context để “tìm dữ liệu”; không khởi động lại stack cũ. Mất artifact/thiếu trường thì ghi unavailable/not checkable, không tái sinh response hay đoán binding.
- Mẫu M7a là **corpus khác** bộ 80 câu M4c: giữ tập riêng và ID/provenance riêng; không gán nó thành 80 câu đã được generation thật hoặc trộn mẫu số. Đối chiếu label/receipt/route/request ID chỉ khi có mapping đủ chắc, không đoán từ title hoặc UUID mới.
- Mỗi mẫu có question/scope, observed status/reason, tất cả claim/citation có thật, quote và context nguồn, locator/version/file khi đã ghi; phân biệt kiểm máy với phán đoán human. Chấm grounded claim, citation support/accuracy, abstention theo rubric ngắn, bao gồm comparison cần hai phía, conflict, partial, bibliography và injection. Không suy đúng nghĩa từ JSON hợp lệ/quote verbatim/ID hợp lệ.
- Không có đủ raw evidence pack hoặc full locator thì machine check tương ứng là `not_available`, không giả vờ đã mở lại viewer đúng quyền. Không cố khôi phục DB chứa tài liệu đã xóa chỉ để chấm mẫu.

## 3. Tính điểm và provenance

- Tái dùng `answer_rows`/`score-review` và bất biến M4c: grounded chỉ vào mẫu số khi mọi claim/citation được chấm đủ, partial/conflicting/unreviewed tách riêng; zero denominator là **not measured/null**, không 0% hay 100%.
- Bundle có manifest ổn định cho toàn bộ câu/response/claim/citation được xuất. Import đối chiếu manifest để phát hiện **xóa cả response hoặc cả câu**, xóa dòng citation/claim, thêm trùng, sửa claim/quote hay đổi provenance; không chỉ tin các count có thể sửa trong CSV. Giữ ID run/response riêng để không gộp hai lần trả lời cùng question/mode.
- Phiếu còn trống không được tạo số chất lượng. Test fixture với reviewer giả chỉ dùng trong test/minh họa tách rõ, không commit như verdict thật. Không dùng agent/LLM tự chấm rồi điền `human_*`.
- Report có số mẫu được duyệt/thiếu/mâu thuẫn, numerator/denominator theo tập và group, giới hạn đại diện, run/code/model/provider khi có. Không biến ba/vài câu tổng hợp thành chứng nhận production hay tự đặt quality SLO/CI gate.
- Hoàn thành bundle và đường nhập/chấm trước khi đề nghị người đọc điền. Chưa có input thật vẫn hoàn tất phần coding, không ngồi chờ hoặc tự ký. Bàn giao tệp cần mở, tệp cần điền, và lệnh import/score cụ thể; đánh dấu human review pending.

## 4. Kiểm chứng đúng rủi ro

- Validate dataset, checksum/PDF reproducibility và test evaluation hiện có. Thêm test nhãn thiếu/sai reviewer/date/verdict, stale question/corpus/parser, duplicate/conflict/unknown ID và import thất bại không sửa một phần dữ liệu.
- Test answer chưa chấm/chấm dở/chấm đủ, thiếu một dòng và thiếu toàn response, đổi nội dung/binding, hai run cùng question/mode, denominator rỗng, tách M7a/M4c, thiếu artifact/evidence. Giữ các test grounded/citation/abstention cũ, không làm lỏng expectation để pass.
- Kiểm bundle render/escape và workflow export/import/report offline bằng dữ liệu tổng hợp; minh họa test không được xuất hiện trong khu vực verdict thật. Nếu xuất HTML, kiểm mở/hiển thị trong browser local, không gọi network; không cần rebuild web app khi app không đổi.
- Chỉ chạy selection theo code evaluation/CLI/format bị tác động, với cấu hình DB test tường minh nếu cần tránh fallback `.env`. Nếu cần runner/DB, dùng context tổng hợp cô lập/runtime thật và fake provider bị chặn outbound, không dev. Không full >5.000 hoặc cả bộ restore/Chrome production khi không có thay đổi shared gate/schema.
- JUnit/selection/hash/exit rõ, không chỉnh source khi suite đang chạy. Giữ failure/rerun/not-run theo source; số pass fixture không phải nhãn human được nghiệm thu. Kiểm cleanup đúng ownership, không đụng tài nguyên có sẵn.

## 5. PR và phần còn lại

Commit/push, mở **một PR tiếng Việt**, không merge, không AI co-author/trailer/promo. Giữ author/committer và toàn bộ docs review/task có sẵn. Thêm `docs/m7b-human-quality-review.md` và inventory ngắn; cập nhật status hiện hành trong kế hoạch/README liên quan, không nhân bản toàn bộ lịch sử vào mọi file.

Bàn giao cuối phải có: đường mở bundle; input/commands cho người đọc; số nhãn và answer thực có; số verdict thật (nếu chưa có = 0); tests/source/not-run; phần không thể kiểm và các bước còn lại. M7a đã merge không phải enable pricing; cache-write live cần ngân sách/ủy quyền riêng, paper thật cần policy/quyền rõ, E vận hành nhóm thử vẫn riêng. Đừng tự mở những phần đó để gọi M7b hoàn tất.

**Inventory lượt giao (2026-10-19):** chỉ kiểm Git/source/docs và đường tồn tại artifact answer; xác minh merge/tree PR #64, không đọc key/`.env` hoặc verdict/answer raw, không chạy test/DB/provider/dev. Task **mới giao/chưa triển khai/kiểm**; người đọc chưa được ghi nhận đã duyệt.
