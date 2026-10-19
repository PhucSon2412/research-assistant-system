# Review PR #65 — bộ duyệt chất lượng và provenance

## Review lại tại `8799216` — hai P2 đã sửa

Ngày **2026-10-19**, head **`8799216bf7b6f80efe93e9165ddc592f72e33b18`**, source **0052**, PR **OPEN/chưa merge**. **Không phát hiện điểm chặn merge mới trong phạm vi M7b đã kiểm.** Kết luận hai P2 bên dưới thuộc lượt review trước tại `44e3bd3`; giữ nguyên làm lịch sử, không còn là trạng thái hiện hành.

### Hai bản sửa đã kiểm

- **P2-1:** binding v2 của label gồm nội dung nhãn, manifest, hash bytes corpus thực và parser; answer gồm row, toàn response, snapshot, corpus và parser, kể cả abstention. Report chỉ tính record khớp binding hiện hành. Corpus không khớp run/manifest hoặc bundle M7a stale làm phần tương ứng `source_mismatch`, không có live verdict hay mẫu số chất lượng; Markdown nêu lỗi. Thay parser rồi dựng lại bundle không tái duyệt record cũ. Record thiếu binding/hash không hợp lệ được giữ để truy vết và loại khỏi kết quả.
- **P2-2:** validate/import dùng preflight chung, dựng lại bundle từ nguồn hiện hành rồi so HTML, bản đọc, file nguồn, snapshot và manifest trước khi ghi. Sửa/thiếu nội dung hiển thị bị từ chối; record đã có không đổi bytes. Điền cột human hợp lệ và file phụ của bảng tính vẫn được nhận. Cache của phép dựng bind theo nội dung đầu vào và parser.

### Reviewer thực chạy và đối chiếu

| Nhóm | Kết quả tại head review lại |
| --- | --- |
| 50 test human + 18 evaluation trong repo | **68/68 pass**, 4 DB test deselect, exit 0, **119,94 s**; chặn kết nối DB và HTTP ngoài |
| Bảy repro độc lập từ lượt review trước | **7/7 pass**: control, M4c bytes, M7a parser, rebuild, corpus/abstention, label HTML và answer HTML |
| `human-prepare --check` | Exit 0, bundle khớp nguồn |
| `human-report` ra thư mục ngoài repo | Exit 0, **`awaiting_human_review`**, 0 verdict thật |

JUnit reviewer và agent khớp đủ **68 ID pass**. **10 hash artifact/provenance**, **121 hash nội dung bundle** và **561 source hash trước suite của agent** khớp head. Seal báo before/after unchanged; không có map after riêng để đối chiếu độc lập. Metadata HEAD của seal vẫn là `44e3bd3` (trước commit sửa), nên bằng chứng source đã kiểm là map hash khớp head mới, không suy working tree khi seal là sạch. **618 tracked source/data hash reviewer trước/sau trùng**, gồm phiếu/report thật. Không cộng kết quả lịch sử hoặc countercheck/Chrome của agent thành test reviewer.

Không rerun 13 countercheck hoặc Chrome 85 trang (chỉ audit hash metadata); không full API/DB/MinIO/web/restore/live. Đây là bản sửa evaluation offline, không đổi production/migration/grants/routing/pricing. Các giới hạn M7a (mẫu tổng hợp nhỏ, không evidence pack/gold, Lumen mapping suy từ kế hoạch upload, reviewer tự khai danh tính) vẫn giữ nguyên; review này không chứng minh chất lượng production.

### Dữ liệu thật, tài nguyên và bước tiếp

Evidence reviewer: `C:/Users/ADMIN/AppData/Local/Temp/ras-pr65-rereview-20261019` (`offline.xml/log`, `bindings.log`, audit, baseline/source/cleanup và report ngoài repo). Mọi verdict giả chỉ nằm trong bản sao tạm; **không nhập hay sửa phiếu thật**. Không tạo container/volume/credential; 24 container có sẵn giữ ID/name/image/mount/label, không có container mới. Beat/worker có vòng restart sẵn tiếp tục tăng RestartCount **367→371 / 368→372**, không can thiệp. Không đọc key/`.env`, gọi provider/inference/arXiv, sửa ledger/pricing hoặc query/migrate/restart dev/Dify.

Có thể merge phần coding/bundle của PR #65 trong phạm vi đã review. Sau merge, chủ dự án có thể mở `evaluation/human-review/bundle/index.html`, điền phiếu theo rubric rồi validate/import; hiện **0 verdict thật, `awaiting_human_review`**, M7b/D chưa nghiệm thu về chất lượng. Cache-write live cần ngân sách riêng, policy/paper thật và gói E vẫn còn. Reviewer để cập nhật docs **chưa commit**, không merge/gửi comment GitHub.

## Lịch sử review lần đầu tại `44e3bd3`

Ngày **2026-10-19**, base **`2df77d0ac2b3e9371aa33753f4ab591fb09d1fa3`**, head **`44e3bd3efe76ca2ae9f1e071bd31d6209be3834a`**, source migration **0052**. PR **OPEN/chưa merge**. **Có hai P2 cần sửa trên cùng PR trước merge.** Không sửa production, DB/grants, pricing hoặc điền phiếu thật trong review này.

[Handoff agent](../m7b-human-quality-review.md) · [Inventory agent](../m7b-test-inventory.json) · [Prompt sửa](../tasks/fix-pr65-human-review-provenance.md)

## P2-1 — report vẫn tính verdict/điểm cũ khi corpus hoặc parser không còn khớp

Vị trí: [`human.py`](../../api/app/evaluation/human.py), `_label_section` dòng 730–766, `_score_rows` dòng 770–791 và `_answer_section` dòng 805–844; phần binding `m4c_binding` dòng 173 và record answer ở `import_answers`.

**M4c:** `corpus_sha256` là hash của manifest khai báo. `check_manifest` đã nhận ra bytes nguồn sai nhưng `_label_section` không dùng `corpus_errors` để loại record hiện hành; `bundle_current` cũng không xét lỗi đó. Sau khi nhập 80 acceptance fixture hợp lệ rồi thay bytes một paper mà không sửa manifest, report vẫn **`accepted_all`, accepted 80, stale 0, bundle_current true**, đồng thời JSON có `P01: checksum or size differs from manifest`. Markdown report không nêu lỗi corpus này.

**M7a:** record chỉ bind `row_sha256` và `snapshot_sha256`, không bind parser/corpus độc lập. `_answer_section` biết bundle stale nhưng vẫn gọi `_score_rows` và tính điểm. Thay metadata phiên bản parser (giữ text/row không đổi) vẫn cho **`reviewed`, 4 live record, grounded 2/2, citation 3/3, abstention 1/1**, dù `bundle_current=false`. **Dựng lại bundle với parser mới** làm `bundle_current=true` nhưng vẫn tái dùng cả bốn verdict cũ. Chỉ đổi cách hiển thị cờ stale không đủ sửa.

Ca thứ ba đổi corpus M7a (thêm thông tin purchase price) làm `corpus_matches_run=false`: ba citation row trở thành stale, nhưng row abstention không chứa text/parser/corpus nên vẫn được tính **1/1**, live 1. Thiếu ràng buộc nguồn ảnh hưởng cả response không claim, không chỉ citation.

Đây là lỗi của đường **report sau khi đã nhập**, không bị guard import chống bundle cũ hiện có chặn. Người đọc có thể nhận số chất lượng đã dựa trên nguồn/parser khác với thứ họ duyệt. Cần bind và đánh giá hiệu lực thống nhất; record không đủ binding phải stale/unreviewed, giữ record cũ nhưng loại khỏi live/accepted và mẫu số. Khi checksum/nguồn không khớp không được `accepted_all`/`reviewed` hoặc giữ tỷ lệ như đã kiểm. Rebuild không tự xác nhận lại verdict.

## P2-2 — validate/import không kiểm các file người đọc xem trong bundle

Vị trí: [`human.py`](../../api/app/evaluation/human.py), `import_labels` dòng 562 và `import_answers` dòng 683; đối chiếu với [`human_bundle.check`](../../api/app/evaluation/human_bundle.py) dòng 471.

Manifest có SHA-256 của HTML, bản đọc và nguồn được copy vào bundle, nhưng `human-validate`/`human-import` chỉ kiểm binding canonical/CSV/snapshot. Chúng không kiểm các hash file trình bày. `human-prepare --check` có phát hiện drift, song không nằm trong đường validate/import mà chủ dự án được hướng dẫn chạy.

**Tái hiện trên bản sao bundle:** thay chữ ngay trong gold span tô sáng ở `labels/SP-01.html`, giữ nguyên CSV, corpus gốc và manifest → `import_labels` vẫn **errors=[], added 80, written=true**. Thay chữ trong context tô sáng ở `answers/01-single.html` → `import_answers` vẫn **errors=[], added 4, written=true**. Phiếu được lưu như thể người đọc đã xem nội dung được bind, dù file hiển thị không còn khớp manifest.

Cần preflight các file không phải ô điền và binding nguồn trước validate/import; drift/missing phải từ chối trước khi ghi record. Giữ hỗ trợ điền một phần và không coi thay đổi các cột human hợp lệ là tamper. Không cần hệ thống ký số/identity mới. Guard chỉ có ở một lệnh kiểm tùy chọn không bảo vệ được luồng nhập được công bố.

## Reviewer thực chạy

| Nhóm | Kết quả |
| --- | --- |
| 37 test human + 18 evaluation trong repo | **55/55 pass**, 4 DB test deselect, exit 0, 62,56 s; HTTP và kết nối DB thật bị chặn |
| Control độc lập: corpus/parser/bundle không đổi | **1 pass**, nhập fixture hợp lệ và report đúng |
| Độc lập: M4c bytes đổi; M7a parser đổi; rebuild sau parser đổi; M7a corpus đổi | **4 failure**, đúng P2-1 |
| Độc lập: label page và answer context page bị sửa | **2 failure**, đúng P2-2 |

Tổng **55 repo + 1 control độc lập pass / 6 ca độc lập fail**, nhóm thành hai P2. Các lượt tái hiện đầu (ba/five failure) giữ riêng, không cộng lặp vào số ca mới. Mọi reviewer/verdict trong repro là fixture, chỉ nằm trong `TemporaryDirectory` bên ngoài repo và đã dọn; không phải một human review thật.

**Trục trặc runner của reviewer:** lượt đầu dùng sai tiền tố `--deselect` (`api/tests/...` trong khi node ID là `tests/...`). Có 55 pass nhưng bốn fixture DB không bị deselect và lỗi kết nối tới **URL dummy/cổng chặn 127.0.0.1:1**, tổng 195,58 s, exit 1; không kết nối được DB hay chạy SQL và không fallback `.env`. Giữ XML/log `offline-wrong-deselect.*`. Sửa riêng runner và thêm guard DB; lượt cuối đúng 55 pass/4 deselect như bảng. Không tính các lỗi setup này thành finding của PR.

## Đối chiếu artifact và phạm vi chưa kiểm

- **13 hash artifact/provenance** khớp: seal, JUnit, counterchecks/browser metadata, manifest/phiếu/snapshot commit và năm file đầu vào M7a đã được phép đọc. **121 hash nội dung bundle** khớp manifest; bundle đang commit hiện không bị sửa. Repro cố tình đổi bản sao để kiểm guard, không kết luận bundle hiện tại đã bị tamper.
- **561 source hash trước suite của agent** hiện khớp head; seal báo before/after unchanged và changed rỗng. Không có map after riêng để đối chiếu độc lập. Seal ghi HEAD `cc8ad7e`, source làm việc lúc seal được xác nhận bằng các hash hiện khớp `44e3bd3`; không gọi metadata HEAD là chứng cứ tree sạch lúc đó. JUnit hai bên khớp đủ **55 ID pass**.
- Không rerun Chrome/85 trang hoặc mười countercheck của agent; chỉ đối chiếu hash metadata, không cộng thành test reviewer. Full API/DB/MinIO/web/restore/live không chạy vì thay đổi là evaluation offline và các finding đã được tái hiện.
- Corpus LF/CRLF được chọn theo SHA run và phân tách M4c/M7a hiện được ghi rõ. Lumen file binding suy từ kế hoạch upload, không phải bản ghi mapping; không có evidence pack/gold M7a, Chrome không chấm, reviewer self-reported: giữ các giới hạn này, không nâng chúng thành xác minh nguồn/quyền production.

## Tài nguyên, dữ liệu thật và bàn giao

Evidence `C:\Users\ADMIN\AppData\Local\Temp\ras-pr65-review-20261019`: `offline.xml/log`, lượt runner lỗi, `test_bindings.py`/`bindings.log` và các lượt trước, `audit-result.json`, source/baseline/cleanup. **618 tracked source/data hash** reviewer trước/sau trùng, gồm bundle/phiếu/report thật. Không có verdict thật nào được ghi, không có record fixture lọt vào repo.

Không tạo container/volume, không query/migrate/restart dev/Dify hoặc DB thật; không key/`.env`, inference/arXiv/provider/account/pricing. Chỉ đọc các file provenance M7a cho phép để hash; ledger không bị sửa. **24 container có sẵn giữ ID/name/image/mount/label**; beat/worker thay state theo vòng restart đã có (RestartCount 169→177/178), không can thiệp.

Giữ report/prompt và các cập nhật trạng thái **chưa commit** cho cùng PR sửa; reviewer không merge/gửi comment GitHub. M7b vẫn `awaiting_human_review` với 0 verdict thật; chưa đề nghị chủ dự án nhập/chấm như thể phần binding đã nghiệm thu. Không cần thêm inference hoặc ngân sách để sửa hai P2 này.
