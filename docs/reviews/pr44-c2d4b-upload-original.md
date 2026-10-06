# Review PR #44: PUT original và outcome khi cleanup lỗi

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2d4b-upload-original.md) · [Handoff](../c2d4b-upload-original.md) · [Inventory agent](../c2d4b-test-inventory.json)

**Sau review:** chủ dự án xác nhận merge; fetch/GitHub CLI ngày 2026-10-06 xác minh PR #44 MERGED tại `dd8bdc689ef7b495618116add4832628d7d24327`, chứa head `5504810`. [Task tiếp C2d-4c](../tasks/m6c2b2c2d4c-original-downloads.md) đã giao, chưa triển khai. Kết quả/verdict/OPEN tại từng lượt dưới đây giữ lịch sử theo source, không chạy lại tại merge. Safety pending tới C4.

## Review lại P2 tại 5504810

Ngày 2026-10-06, xác minh PR #44 **OPEN, chưa merge**, head `550481007b2bac82dc469c616700266b201593ee`, code sửa `adfdb722fdd0de529b45addddd516872e3b404ac`, base/main `632ec683940a8bccc711c8a55743b0a3992c1d03`. Working tree sạch trước kiểm; source migration `0039`, không query DB dev.

**P2 đã được sửa; không phát hiện điểm chặn merge còn lại trong phạm vi C2d-4b.** Outcome do attempt sở hữu được ghi trước commit và sau commit thành công; rollback/close không xóa các mốc đó. Cleanup acceptance chỉ được phép khi chưa gọi commit và rollback đã được xác nhận. Known commit + teardown lỗi giữ rows/object, không trả IDs; exception chưa phân loại không cấp quyền cleanup. Giữ hợp đồng cancellation và release tài nguyên; không nới ACL, clock, schema/grants hoặc safety pending. Original download C2d-4c tiếp tục riêng sau merge do chủ dự án quyết định.

### Kiểm độc lập trên bản sửa

| Kiểm | Kết quả reviewer |
| --- | --- |
| 10 regression P2 mới | **10/10 pass**, gồm lỗi kép, known commit, precommit rollback và exception chưa phân loại |
| C2d-4b chọn thêm: cancellation/thread, I/O ngoài khóa, race, integrity, fresh actor/clock, pending và receipt | **56/56 pass** |
| Legacy upload runtime + ORM/schema | **10/10 pass** (9 + 1) |
| Tổng selection trên | **76/76 pass**, 0 fail/error/skip, **194,592 s**, 3 warning hiện có |
| Ca tái hiện P2 cũ: commit exception + close lỗi | **1 pass**; `503 upload_outcome_uncertain`, 0 business row, **1 object giữ lại** |
| Control cùng commit exception, close bình thường | **1 pass**, cùng mã lỗi và object giữ lại |

Hai ca ngoài repo thay test body trong bộ nhớ trên fixture runtime/PostgreSQL/MinIO thật; không thêm vào số 76 hoặc inventory production. Commit harness đã rollback DB để SELECT không thấy hàng, API chỉ biết commit exception. Đây là fault injection, không chứng minh crash, remote commit đang chạy hoặc mất điện thật.

Đối chiếu độc lập ba JUnit agent: 239 upload + 155 consent + 69 runtime/ORM = **463 unique pass**, khớp node selection, SHA artifact và 262 source hashes trước/sau/current. Reviewer chỉ chạy lại 76 trong số đó; **387 node của selection agent** và **2.839 node của collection 2.915** chưa chạy độc lập. Agent còn **2.452 not-run**. Không cộng số 808/230/51 lịch sử hoặc hai ca bổ sung vào lượt kiểm mới. Web không đổi so với b578082; không chạy lại web/build/browser, full callers, C3/C4, provider, restore hoặc benchmark.

Evidence `%TEMP%/ras-pr44-p2-review-20261006`: `review-p2.xml`, `repro.xml`, `control.xml`, ba source JSON, `artifact-checks.json`, runner/fault scripts và cleanup JSON. JUnit SHA-256 lần lượt `ae3c8bb573928e63e2340597adc0c5c9347a0bb76cb1f8f10a030d06ec3455eb`, `2dcfbe2c7b276a00bdb7449469976223feb09f0b44418b3a541396ece186c75a`, `96cefbc2f325b66511c508d62d4faaef5c0cd46d8404ce0c4c05d6f5fa63b0c7`. Script đối chiếu artifact ban đầu dùng tên selection file không tồn tại; đã sửa sang đối chiếu nhóm node theo lệnh chọn và chạy lại đạt, không có test ứng dụng fail trong lượt này.

Conda ras interpreter/pip đã xác minh. PostgreSQL `ras-pr44-p2-review-db-20261006` port 58457, MinIO `ras-pr44-p2-review-minio-20261006` port 59457, label `ras.task=pr44-p2-review-20261006`; credentials tường minh trước import, HTTP ngoài local bị chặn, không đọc `.env`/key. Đã kiểm exact ID/name/label/image/port/mount/sole consumer rồi gỡ đúng **2 container/2 volume thử**, xóa 3 file credentials; **24 container có sẵn giữ nguyên ID/image/StartedAt**. Giữ journal/log/evidence ngoài repo. Không sửa app/test source, commit/push/merge/comment PR, dev hoặc pricing/readiness. Safety **pending tới C4**; giới hạn thread/multipart, reconcile thủ công và release transport vẫn như handoff.

QA sau cập nhật: **50 Markdown, 1.085 liên kết nội bộ, 0 lỗi**; không ký tự control bất hợp lệ, `git diff --check` sạch. Chín file docs/hướng dẫn được cập nhật chưa commit; agent cần giữ và đưa vào PR. 262 source hashes và AST parser/download vẫn khớp; không sửa code ứng dụng để review.

## Kết luận review ban đầu tại b578082 (lịch sử)

Ngày 2026-10-06, Git fetch/GitHub CLI xác minh PR #44 **OPEN, chưa merge**, head `b578082731aeaf321f5d7d5ff597823dc3a5dd44`, base/main `632ec683940a8bccc711c8a55743b0a3992c1d03`. Code/test `68950f3357e740bed6030af53dc385bcef5de585`; source migration `0039`, không query DB dev. Working tree sạch khi bắt đầu review.

**Có một điểm chặn merge: P2 ở đường giữ object khi commit uncertain và Session cleanup cũng lỗi.** 230/230 test chọn sẵn pass, nhưng ca bổ sung tái hiện object bị xóa trái hợp đồng task. Cần coding agent sửa và thêm regression trước merge; chưa giao triển khai C2d-4c. Safety **pending tới C4**.

Reviewer chỉ viết docs và công cụ kiểm ngoài repo; không sửa production/test source, commit/push/merge/comment PR, `.env`, stack dev hoặc provider/pricing. Các số kiểm dưới đây tách khỏi 808 của agent.

## P2: cleanup exception che mất trạng thái uncertain

Vị trí ở [upload_original.py](../../api/app/api/upload_original.py): `accept()` dòng 134–193 và `_attempt()` dòng 321–346 tại source review.

1. `accept()` đặt `commit_started=true`, `db.commit()` ném lỗi. Handler đổi thành `ApiError(503, upload_outcome_uncertain)` đúng như task.
2. Khi thoát `with Session(...)`, `Session.close()` ném lỗi tiếp. Lỗi teardown **che mất** ApiError ban đầu. Đường `finally` rollback rồi close tại dòng 188–193 cũng phải giữ được outcome nếu nó lỗi.
3. `_attempt()` chỉ đặt `keep=true` khi bắt được đúng ApiError có code uncertain. Exception teardown khác đi qua handler chung thành `503 upload_unavailable`; `keep` còn false.
4. `finally` chạy `cleanup()`. Nếu SELECT chưa thấy SourceFile, object bị xóa. Không thấy hàng không chứng minh commit từ xa đã rollback hoặc kết thúc; task đã cấm dùng kết quả SELECT đó để xóa object của một commit uncertain.

Đây là lỗi về ownership/outcome khi hai lỗi xảy ra liên tiếp, không phải lỗi checksum hoặc SQL ACL. Trong ca giả lập, transaction thực được rollback để tạo tình huống SELECT không thấy tham chiếu; API chỉ nhận lỗi commit, **không được phép suy ra rollback từ fault harness**. Không tuyên bố đã quan sát mất file của một commit thật còn chạy trên server.

### Tái hiện và đối chứng

Runtime bootstrap/invitation/login/consent thật, PostgreSQL/MinIO thật, file Markdown tổng hợp. Công cụ ngoài repo thay riêng test body trong bộ nhớ và inject hai lỗi; không thay code production trên disk.

| Ca | Kết quả tại source review |
| --- | --- |
| Commit exception, `Session.close()` bình thường | `503 upload_outcome_uncertain`, 0 business row, **1 object được giữ**; kiểm hợp đồng pass |
| Cùng commit exception, `Session.close()` ném lỗi một lần khi thoát acceptance Session | `503 upload_unavailable`, 0 business row, **0 object**; assertion yêu cầu giữ object fail |

Fault commit/close là injection, không ngắt mạng, kill process hoặc mất điện. Slot/temp và fixture teardown vẫn được kiểm; lỗi có ý nghĩa nghiệp vụ, không phải fixture/setup error.

### Yêu cầu sửa

- Outcome commit phải được giữ độc lập với exception cuối cùng của thread/Session teardown. Một lỗi rollback/close không được đổi **uncertain hoặc known commit** thành precommit failure có quyền xóa object.
- Khi acceptance thread trả exception không thể chứng minh là known precommit rollback, giữ object và trả outcome chưa xác định; không cleanup chỉ vì exception không phải ApiError mong đợi. Có thể dùng state/result hẹp do attempt sở hữu, được cập nhật trước commit; không phụ thuộc riêng vào error code/string.
- Giữ best effort cleanup cho lỗi **known precommit rollback**, và giữ referenced object sau known commit. Không sửa bằng cách bỏ mọi cleanup hoặc bỏ release Session/temp/slot.
- Thêm regression cho commit lỗi + close lỗi, commit lỗi + rollback/close lỗi, known commit + teardown lỗi và control precommit rollback; kiểm mã lỗi/no IDs/no retry, rows/object và resource release. Nếu mô phỏng remote commit còn in flight, SELECT chưa thấy hàng cũng không được delete.
- Chạy lại nhóm upload/consent/runtime/ORM liên quan trên source sửa; giữ expectation hiện hành. Không cần đổi schema hoặc mở safety pending để sửa lỗi này.

## Kiểm độc lập và đối chiếu artifact

| Kiểm | Kết quả reviewer |
| --- | --- |
| Toàn `test_c2d4b_upload_original.py` | **220/220 pass** |
| Toàn legacy runtime `test_uploads.py` | **9/9 pass** |
| `test_orm_models_match_migrated_schema` | **1/1 pass** |
| Tổng selection trên | **230/230**, 0 fail/error/skip, 536,90 s; 3 warning hiện có |
| Ca bổ sung commit + close lỗi | **1 failure đúng lỗi**, 0 error/skip, 3,27 s |
| Control commit lỗi, close bình thường | **1 pass**, 2,88 s; không cộng vào selection 230 |
| Web `npm test --prefix web` | **51/51 pass**, 0 fail/skip |
| Web typecheck | Đạt; không chạy lại build/browser DOM |

Reviewer xác minh ba JUnit agent: 229 upload + 155 consent + 424 regression = **808 unique node**, khớp selection, không fail/error/skip; artifact SHA đúng inventory. 262 API Python/SQL/web source hashes trước/sau agent, trước/sau reviewer và source hiện tại khớp. Đây là kiểm artifact, **không phải reviewer chạy lại 808**.

Selection reviewer khớp JUnit 230 node, hash trước/sau 262 file không đổi. Còn **578 node trong suite agent** và **2675 trong collection 2905** chưa chạy độc lập; agent vẫn 2097 not-run. Ca bổ sung/control là body kiểm ngoài repo dùng fixture thật, không tính thành node production mới hay full inventory pass.

JUnit reviewer SHA-256: `38b8d2a16b311b7d2fd9e61e524ca3a4b75cc3c9ff419760ba9aee65ee422d6a`. Repro: `88fb959e017072c9dbe7ca5c5c2343358d0933e41c69aac2879a074df99f762f`; control: `92375c43293cdf36fbc480da5fd9e0610b87f50887d8c2649429976c9ad8b706`.

## Phạm vi được xác nhận và giới hạn

- Short proof intent/ack/journal trả connection trước body/parser/S3/readback; runtime auth có thể revoke khi I/O bị chặn. Writer User → intent → ack → DML/FK/audit flush → journal exclusive; response transaction mới theo item → job và exact consumed binding.
- Đã chạy ma trận actor/generation/receipt/clock/notice, checksum cùng size, race hai staging/cùng intent, slot/temp, cancel HTTP/cancel-all, pending không patch và HTTP Uvicorn qua TS proxy handler. Worker advance trước response không bị ghi đè. Parser/download AST, auth/authz/identity/models/migrations không đổi so base.
- HTTP proxy smoke và form callback không phải browser DOM/Next.js stack hoàn chỉnh. Không chạy lại full consent/regression của agent, bốn mutants, web build, C3/C4/cutover/restore, provider hoặc benchmark. Không suy quality/SLO/production durability từ kết quả này.
- Timeout tổng/thread bị treo/multipart chưa hoàn tất và journal consent xuyên restore vẫn là giới hạn đã công bố; không đề xuất mở dev readiness hoặc triển khai C2d-4c trước fix P2.

## Môi trường và cleanup

Conda ras interpreter/pip xác minh; PostgreSQL `ras-pr44-review-db-20261006` port 58456, MinIO `ras-pr44-review-minio-20261006` port 59456, label `ras.task=pr44-review-20261006`. Credentials DB/runtime/recovery/MinIO tường minh trước fixture import; chặn HTTP ngoài local, bỏ key provider khỏi process env; không đọc `.env` hoặc key. Scoped cutover chỉ ở fixture nghiệp vụ, pending HTTP riêng thật.

Evidence `%TEMP%/ras-pr44-review-20261006`: runner/setup, `review.xml`, `uncertain-close-repro.py`, `repro.xml`, `uncertain-close-control.py`, `control.xml`, source hashes, artifact checks và cleanup JSON. Đã kiểm exact container ID/name/label/image/port/mount/sole consumer rồi gỡ đúng **2 container/2 volume thử**, xóa đúng 3 file credentials trong thư mục evidence đã xác minh. **24 container có sẵn giữ nguyên ID/image/StartedAt**; journal/log/khả năng tái hiện giữ ngoài repo. Không tác động DB hoặc volume dev.

Kiểm tài liệu sau review: **50 Markdown thuộc AGENTS/README/docs, 1070 liên kết nội bộ, 0 lỗi**; `git diff --check` sạch. Các thay đổi chưa commit chỉ là docs/report, cần coding agent giữ vào PR sửa; 262 source hashes vẫn khớp.

## Bổ sung của coding agent trước review lại: sửa P2 trên cùng PR

Theo yêu cầu chủ dự án, source `adfdb722fdd0de529b45addddd516872e3b404ac` giữ outcome do attempt sở hữu độc lập exception rollback/close; chỉ known precommit rollback được cleanup, known commit + teardown lỗi giữ rows/object và không trả IDs. Tám regression tái hiện 7 fail/1 pass trước sửa, sau sửa 8 pass; thêm hai ca exception chưa phân loại, focused 10 pass. Suite liên quan **463/463**, collect 2.915/not-run 2.452, source/JUnit/hash ở [handoff P2](../c2d4b-upload-original.md#sửa-p2-outcome-độc-lập-với-teardown) và phần p2_fix của [inventory](../c2d4b-test-inventory.json).

Tại thời điểm bổ sung này, agent triển khai **chưa có review lại từ reviewer**. Kết luận, số 230/51 và ca fault/control của review ban đầu giữ theo b578082; không cộng vào kết quả sửa. Review lại trên bản sửa nằm ở đầu report. PR OPEN/chưa merge, safety pending tới C4, original download C2d-4c riêng. Không web/dev/`.env`/provider hoặc schema/grants thay đổi.
