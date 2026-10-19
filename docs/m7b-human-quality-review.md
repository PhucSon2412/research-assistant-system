# M7b — bộ duyệt nhãn/answer và nhập phiếu người đọc

Nhánh `feat/m7b-human-quality-review`, base `origin/main = 2df77d0` (merge PR #64, tree khớp `285fd37`).
Source migration giữ **0052**, không migration/grant, không đổi production retrieval/prompt/adapter/pricing/C4.
[Task](tasks/m7b-human-quality-review.md) · [inventory](m7b-test-inventory.json) ·
[bundle](../evaluation/human-review/README.md). Chưa merge.

## Sửa review PR #65 tại `44e3bd3` — chờ review lại

[Review](reviews/pr65-m7b-human-quality-review.md) tìm hai P2; [prompt sửa](tasks/fix-pr65-human-review-provenance.md).
Chỉ đổi `api/app/evaluation/human.py`, `human_bundle.py`, `__main__.py`, test và bundle tái tạo (manifest
`aecd381e…`); không production/migration/inference.

1. **Hiệu lực verdict theo binding đầy đủ.** Mỗi record lưu `binding` (version 2) + `binding_sha256`. Nhãn:
   question/label hash, hash manifest corpus, **hash bytes thực trên đĩa** (`corpus_files_sha256`) và parser;
   answer: row, cả response, snapshot, bytes corpus M7a và parser — kể cả dòng abstention không có quote.
   Report chỉ tính record có binding đúng bằng binding hiện hành. Corpus M4c sai checksum/size, bundle M7a lệch
   nguồn hoặc corpus M7a không tái lập run → trạng thái **`source_mismatch`** (cả section và tổng), 0 record
   live, mọi mẫu số 0 → `null`; Markdown ghi lỗi nguồn. Record cũ thiếu/sai binding giữ trong file nhưng là
   stale; dựng lại bundle với parser mới không kích hoạt lại verdict cũ — cần phiếu mới (thành record mới).
   Import answer từ chối khi corpus M7a không tái lập run.
2. **Preflight bundle ở cả `human-validate` và `human-import`.** Dựng lại bundle từ nguồn hiện tại (M4c root,
   snapshot trong bundle, corpus M7a) và so mọi trang HTML, bản đọc, file nguồn copy, snapshot và
   `manifest.json`; cột điền của phiếu và file thừa (ví dụ lock file của bảng tính) không tính là drift.
   Lệch/thiếu → lỗi trước mọi mutation, record cũ giữ nguyên bytes. Build được cache theo nội dung đầu vào
   trong một process.

Trước sửa, repro của reviewer (`ras-pr65-review-20261019/test_bindings.py`) **6 fail / 1 pass**; sau sửa
**7/7 pass** (control `reviewed`, 80 accepted, grounded 2/2). Regression trong repo: control đủ phiếu;
bytes corpus đổi khi manifest giữ nguyên (và khôi phục bytes thì hiệu lực lại); parser đổi trước/sau rebuild;
corpus M7a lệch run với dòng abstention; record thiếu/giả binding; 7 ca drift (trang nhãn, bản đọc bị xóa,
file nguồn copy, manifest, trang answer, bản đọc M7a, PDF M7a) cả validate và import; phiếu điền tại chỗ +
lock file vẫn nhận. Selection **68 pass / 0 fail**, collect 72, 4 DB deselect (`tests/test_evaluation.py::…`),
561 source hash trước/sau trùng (`selection.xml` `92a2c090…`). Đối chứng đột biến **13/13** bắt được
(thêm: bỏ `is_live`, bỏ gate+hash bytes corpus, bỏ parser khỏi binding answer, tắt preflight). Chrome 155
offline 85 trang, 0 request ngoài, kết quả trùng lượt trước. Evidence
`C:/Users/ADMIN/AppData/Local/Temp/ras-m7b-20261019/fix-pr65`. Vẫn **0 verdict thật, `awaiting_human_review`**.

Các mục bên dưới là bàn giao lần đầu tại `44e3bd3`; phần binding/import đã được thay bằng mô tả trên.

**Trạng thái: phần coding và bundle hoàn tất; `awaiting_human_review`.** Số verdict thật đã nhập: **0**
(nhãn) và **0** (answer). Không agent/LLM nào điền cột human; test dùng reviewer giả chỉ trong `tmp_path`.
M7b/gói D chưa nghiệm thu.

## Người đọc làm gì

1. Mở `evaluation/human-review/bundle/index.html` (offline, không đăng nhập).
2. Điền `bundle/label-review.csv` (80 dòng) và `bundle/answer-review.csv` (4 dòng); lưu CSV UTF-8, ngày
   `YYYY-MM-DD`. Có thể nộp từng phần: dòng trống là chưa duyệt.
3. Từ gốc repo: `human-validate` (chỉ kiểm) → `human-import` (ghi `records/`) → `human-report`
   (ghi `evaluation/human-review/report.md/json`). Lệnh đầy đủ ở [README bundle](../evaluation/human-review/README.md).

Rubric ở `bundle/README.md` và trang `index.html#guide`: nhãn `accepted`/`changes_requested`/`rejected`
(notes bắt buộc khi không accepted); answer `human_claim_supported` yes/partial/no,
`human_citation_correct` yes/no, `human_abstention_appropriate` yes/no chỉ cho câu kỳ vọng không trả lời.

## Đã xây

- `api/app/evaluation/human.py`: binding, đọc/ghi phiếu, import, record overlay và report. `human_bundle.py`:
  trang HTML tĩnh, phiếu, manifest, `--check`. CLI mới trong `python -m app.evaluation`: `human-prepare`,
  `human-validate`, `human-import`, `human-report`. Không backend/route/web/schema mới.
- `review.score` nay nhóm theo `response_id` khi phiếu có cột này (M7a), nên hai lần trả lời cùng
  câu/mode không bị gộp; phiếu M4c cũ vẫn nhóm theo `question_id/mode`. Bất biến cũ giữ nguyên: grounded
  chỉ vào mẫu số khi response chấm đủ; incomplete/conflicting/unreviewed tách riêng; mẫu số 0 → `value: null`.
- Bundle commit tại `evaluation/human-review/bundle/` (124 file, ~0,8 MB; manifest
  `737748e2…`). `.gitattributes` đặt `-text` cho `evaluation/human-review/**`: bundle được bind bằng SHA-256
  với đúng bytes đã build, git không đổi line ending.

### Nhãn M4c (80 câu, 16 holdout, 6 nhóm)

Mỗi trang có ID, split, group, tags, identity/scope (kèm item/paper), câu hỏi, expected status/reason/
coverage, từng side evidence với quote, dòng/trang/offset, parser, **đoạn text parser production chứa span
(span được tô)**, liên kết bản đọc đúng dòng/trang và file gốc trong bundle (PDF mở `#page=`),
non-evidence với lý do và context, forbidden markers, supported claim, rationale. Cột `agent_note` chỉ đánh
dấu 7 câu có kỳ vọng mà docs nêu là lựa chọn sản phẩm (bibliography-only RCI-01/02/04, conflict RCI-06, so sánh
một phía CMP-12/14/15); không phải verdict.

Không đổi `label_status` hay `questions.json`: verdict là record overlay trong `records/label-reviews.json`, bind
`label_sha256` (JSON chuẩn hóa **toàn bộ** câu/nhãn), `corpus_sha256` (manifest corpus) và phiên bản parser
Markdown/PDF. Report tính hiện hành chỉ record khớp cả ba; nhãn/corpus/parser đổi → record **stale**, câu về
`unreviewed`. Vì vậy `changes_requested` không tự thành `accepted` sau khi agent sửa nhãn: cần phiếu mới.
Report tách accepted/changes_requested/rejected/conflicting/unreviewed theo split và nhóm; chưa đủ 80/80
accepted thì retrieval M4c vẫn `provisional`.

### Answer live M7a (corpus riêng, không cộng mẫu số với M4c)

Nguồn chỉ đọc 5 file trong `C:/Users/ADMIN/AppData/Local/Temp/ras-m7a-20261018/state`: `runtime/answers.json`
(`ab70769f…`), `runtime/chrome-answer.txt`, `runtime/targets.json`, `runtime/corpus.json`, `ledger.jsonl`
(chỉ outcome generation của route `runtime:rag-*`/`chrome-rag`, rút còn seq/route/status/cost/ID/model/
provider/token). Không đọc key/`.env`, ảnh, compose log hay file khác; không khởi động stack. Bản snapshot
chuẩn hóa nằm trong bundle (`sources/m7a-snapshot.json`, `8e83bd48…`) để bundle tự đủ.

| Response | Phiếu | Ghi chú |
| --- | --- | --- |
| `m7a-runtime-2026-10-18/01-single` | 1 dòng citation | item a-lumen, kỳ vọng drill `answered` |
| `…/02-compare` | 2 dòng citation | scope all, so sánh hai phía |
| `…/03-insufficient` | 1 dòng abstention | status `insufficient_evidence`, reason null |
| `…/chrome` | không chấm | chỉ text màn hình (`screen_text_only`) |

- Scope/mode/kỳ vọng lấy từ code drill (`scripts/m7a_runtime_drill.py`: user A, keyword, `rag-<label>`); kỳ vọng
  là ý định drill, **không** phải gold label. Ledger: response ↔ route `runtime:rag-<label>` chỉ khi đúng một
  call và một answer cho label đó (seq 16/17/18; Chrome seq 20); mơ hồ thì không gán.
- File được dẫn: harbor theo `targets.json` (`recorded`); lumen suy từ kế hoạch upload của drill — A chỉ có một
  file Markdown, bản B cùng bytes không thấy với A (`derived:drill_upload_plan_user_a`). Không suy từ title.
- Corpus M7a render lại đúng như drill; SHA-256 khớp `corpus.json` của run (`lumen e4015acd…`, `harbor
  e060b84c…`). **Phát hiện:** run đã upload `lumen-bench.md` dạng CRLF (working copy Windows, autocrlf), còn
  blob git là LF (`2c22bc01…`). Bundle thử cả hai line ending và chỉ dùng biến thể khớp đúng SHA của run, nên
  checkout LF hay CRLF cho cùng bundle; không khớp thì ghi corpus lệch run, không đoán. Kiểm máy chỉ cấu trúc: quote nguyên văn trong chunk Markdown bắt đầu ở dòng ghi / trang PDF ghi
  (3/3 `yes`). `machine_id_in_evidence` và `machine_locator_covers_gold` là `not_available` (run không lưu
  evidence pack; M7a không có gold). Corpus lệch run → kiểm quote cũng `not_available`.
- Mẫu Chrome không chuyển text màn hình thành claim/citation; bản `chrome` trong
  `evaluation/m7a-synthetic/answers.json` là bản chép lúc M7a, M7b không dùng.
- Mỗi dòng có `run_id`/`response_id`/`row_key`; record bind `row_sha256` (mọi cột hiển thị gồm claim, quote,
  provenance) và hash snapshot. Snapshot/bundle đổi → record stale.

## Import và kiểm lỗi

Import là **all-or-nothing**: có bất kỳ lỗi nào thì không ghi gì (ghi tạm + `os.replace`). Lỗi gồm: cột
thiếu/thừa/đổi thứ tự, không UTF-8, ID/row lạ, dòng trùng, thiếu dòng (kể cả cả response/câu), sửa bất kỳ cột
hiển thị (câu hỏi, kỳ vọng, claim, quote, version/file, số claim/citation…), bundle cũ so với nguồn hiện tại,
thiếu verdict/reviewer/ngày, ngày sai định dạng/tương lai, reviewer nhiều dòng, notes thiếu khi không
accepted, giá trị ngoài tập cho phép, abstention ở câu kỳ vọng có đáp án, chấm claim ở response không claim,
claim cùng phiếu lệch nhau, claim `yes` khi mọi citation `no`, abstention lệch trong một response.
Cùng reviewer + cùng nội dung: giống hệt là no-op, khác cần `--replace`. Reviewer khác nhau cùng được giữ;
lệch thì report ghi `conflicting` và loại khỏi mẫu số (citation accuracy cũng loại). Attribution là
`self_reported`: tên/ngày do người điền tự ghi, không phải danh tính hay chữ ký đã xác thực.

Ô hiển thị bắt đầu bằng `= + - @ tab CR '` được thêm `'` khi xuất để bảng tính không chạy công thức; import
đảo ngược đúng. HTML: mọi text escape, CSP `default-src 'none'`, không script/link ngoài.

## Kiểm chứng (evidence `C:/Users/ADMIN/AppData/Local/Temp/ras-m7b-20261019`)

- Selection theo rủi ro trên source cuối (evidence `final/`), `RAS_TEST_DATABASE_ADMIN_URL` trỏ cổng chặn để
  không fixture nào rơi về `.env`: **55 pass / 0 fail / 0 skip**, collect 59, 4 deselect (`selection.xml`
  `540905b9…`): `tests/test_human_review.py` 37 (gồm bundle commit đồng bộ với corpus LF và CRLF, không chứa
  reviewer fixture) + `tests/test_evaluation.py` 18. Bốn test evaluation cần DB/MinIO (`api`/`upload_api`:
  embedder, settings, runner session, ACL runner) **not-run**: không đổi runner/ACL/DB. 561 source hash
  trước/sau trùng. Lượt sớm hơn (54 pass, trước sửa line ending) giữ ở thư mục gốc evidence, không cộng dồn.
- CLI: `validate` (80 câu, 16 holdout, 15 paper OK), `build-pdfs --check`, `human-prepare --check` (up to
  date), `human-report` (`awaiting_human_review`) đều exit 0.
- Đối chứng đột biến 10/10 bị test bắt, file khôi phục đúng hash: nhóm `response_id`, ghi một phần, stale
  label, escape HTML, tiền tố công thức, cột hiển thị bị sửa, đoán ledger call (2 nhánh), line ending corpus
  run, conflict khác reviewer. Lượt đầu có một ca đột biến nhầm bảo vệ không được test phủ; đã thêm test và
  tách ca.
- Chrome 155 cục bộ, context offline, chặn mọi request không phải `file:`: 85 trang render, 0 request ngoài,
  0 dialog; bundle chèn `<script>`/`onerror` không chạy và hiện thành text; liên kết bản đọc tới `#L19`.
- Không chạy: full API >5.000, test DB/restore/Chrome production, web (không đổi). Không provider/arXiv/
  inference/network, không đọc key/`.env`, không chạm dev `ras`/Dify.

## Giới hạn và phần còn lại

- Chưa có phiếu thật: mọi chỉ số là `not measured`; không có số chất lượng nào được tạo.
- M7a chỉ 3 response có cấu trúc trên 2 tài liệu tổng hợp, một run keyword/user A, không gold label, không
  evidence pack; không đại diện paper thật, không là production certification/SLO/CI gate. Không có mẫu
  conflict/bibliography/injection ở M7a (rubric có, số mẫu 0). M4c vẫn không có answer generation (`not_measured`).
- Lumen file binding là suy luận từ kế hoạch upload, ghi rõ basis; stack M7a đã dọn nên không mở lại viewer.
- Sau khi chủ dự án điền và import: cập nhật report/handoff. Gói D còn cache-write live (ngân sách/ủy quyền
  riêng), duyệt provider/policy và paper thật có quyền; E vận hành nhóm thử riêng. Trần M7a cũ đã hết, pricing
  mặc định tắt.
