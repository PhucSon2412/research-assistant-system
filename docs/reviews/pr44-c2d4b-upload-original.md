# Review PR #44: PUT original và outcome khi cleanup lỗi

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2d4b-upload-original.md) · [Handoff](../c2d4b-upload-original.md) · [Inventory agent](../c2d4b-test-inventory.json)

## Kết luận

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
