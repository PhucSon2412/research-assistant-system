# C2d-4c: tải original riêng, chung và review

[Review độc lập PR #45 tại `b23ff82`](reviews/pr45-c2d4c-original-downloads.md): 350/350 API, 57/57 web và typecheck đạt; không phát hiện điểm chặn merge scope C2d-4c. Kết quả agent 991 bên dưới giữ riêng, PR sau đó đã merge `070cb37`, chưa rerun tại merge; safety pending tới C4.

[Task](tasks/m6c2b2c2d4c-original-downloads.md) · [Inventory](c2d4c-test-inventory.json) · [Caller inventory](c2d1-caller-inventory.json)

Base đã fetch và xác minh `dd8bdc689ef7b495618116add4832628d7d24327`; PR #44 MERGED, main chứa head `5504810` và code P2 `adfdb72`. Nhánh `feat/m6c2b2c2d4c-original-downloads` giữ mọi docs orchestrator chưa commit. Source migration `0039`, không migration/grants mới. **Safety pending tới C4; PR #45 đã merge `070cb37`.** Kết quả triển khai dưới đây giữ theo source trước merge; [C2d-5a writer](tasks/m6c2b2c2d5a-review-json-writers.md) được giao tiếp.

## Đối chiếu trước sửa

| Nhánh | Code tại base | Boundary C2d-4c |
| --- | --- | --- |
| Private owner | Open S3 rồi stream trực tiếp bằng dependency Session; không checksum/final caller | Proof ngắn → đóng Session → spool size/SHA-256 → transaction response mới |
| Common | Spool có pool/hash; session trước gate SQL cuối; audit commit rồi dùng proof cũ | Scalar target, clock sau mọi SQL, audit writer riêng, proof response mới sau known commit |
| Admin review | Spool riêng không pool; ORM sau rollback có thể lazy-load; chỉ recheck grant | Current Admin + exact grant; request SHARE → item SHARE; chung pool/response ownership |
| Proxy | ReadableStream nhưng chưa clear-cookie/cancel upstream | Giữ hai route riêng; Set-Cookie đầy đủ, abort fetch/cancel reader, không buffer file |

Các mô tả M1c/M6a/M6b-2b1 cũ là lịch sử; không coi checksum hoặc principal đầu request là chứng cứ quyền response mới. Không chuyển writer review/publication hay provider callers trong PR này.

## Boundary và lock map

[`api/original_downloads.py`](../api/app/api/original_downloads.py) nhận định danh từ dependency thật; không chuyển dependency Session sang executor. Mỗi giai đoạn tạo/đóng Session riêng trong thread. Target frozen chỉ chứa scalar item/version/file/key/hash/size/MIME/filename/epoch và request/checksum review. Headers lấy từ target của proof response cuối.

| Giai đoạn | Ordinary private/common | Review |
| --- | --- | --- |
| Proof trước I/O | Current caller scalar/predicate → candidate SQL ACL → item SHARE → journal shared → SQL target/gate → final caller/clock | Current Admin → exact candidate → request SHARE → item SHARE qua exact grant → journal shared → exact target/gate → current Admin/clock |
| Storage | Session/connection đã trả; acquire pool → temp → đọc S3 theo chunk, kiểm size/SHA-256 object và đọc/băm actual spool trước byte đầu | Cùng pool và ownership; hash file phải khớp checksum request qua grant |
| Audit thành công | Private không thêm audit; common: writer entry → User NO KEY UPDATE → item SHARE → audit/FK flush → journal exclusive → actor/gate/clock → commit | Writer entry → User NO KEY UPDATE → request SHARE → item SHARE → audit/FK flush → journal exclusive → current Admin/exact grant/clock → commit |
| Response | Transaction mới, map proof đọc trên; common chỉ tới đây sau known audit commit | Transaction mới sau known audit commit; không dùng proof/ORM trước commit |
| Stream | Nhả hết DB locks/connection; stream đúng spool; ASGI response sở hữu slot/temp | Cùng ownership và boundary thu hồi |

Resource locks luôn trước journal; không nâng shared lên exclusive, không lấy User/session/tombstone sau journal. Item lock bảo vệ các writer/cleanup theo protocol; version/file immutable trong các writer được hỗ trợ nên không thêm locks ngoài map. Read helper không touch idle/commit. Audit actor GET không thêm CSRF/Origin của POST. Review cần role Admin mới; private/common giữ owner/download gate độc lập role. Clock được đọc sau SQL gate và chính predicate caller cuối.

## Outcome, tài nguyên và lỗi

`AuditOutcomeState` do operation sở hữu, cập nhật trước/sau commit, độc lập rollback/close. Commit exception luôn uncertain; không SELECT để suy outcome, retry audit mutation hay xóa original. Known commit rồi quyền/clock mất hoặc teardown lỗi vẫn không trả file; audit có thể đã lưu. Teardown thử rollback, close và invalidate; không chuyển lỗi cleanup thành quyền phát bytes.

| Kết quả | HTTP/mã |
| --- | --- |
| Missing/foreign/malformed file/request; grant/rights không còn đúng | `404 not_found` |
| Caller exact session/User/head/receipt/binding/status/generation/TTL không hợp lệ | `401 session_expired`, clear session cookie (helper API hiện hành; proxy giữ mọi Set-Cookie) |
| Current review role không phải Admin | `403 forbidden` |
| Size/hash sai | `409 private_file_integrity_failed` / `common_file_integrity_failed` / `review_file_integrity_failed` |
| Object missing | Private/common `409` integrity; review giữ `503 file_unavailable` của nhánh open cũ |
| Storage/temp/spool/DB/teardown unavailable | `503 file_unavailable` |
| Pool đầy | `503 download_busy` |
| Journal/recovery/pending đóng | `503 service_recovering` |
| Audit commit exception | `503 download_audit_outcome_uncertain`; có thể audit đã lưu, không tự retry |

Giữ action/target/details audit common/review, chỉ IDs/mã. Denied/failure audit best effort không che denial; không thêm audit private. File response có Content-Type/Length, Content-Disposition percent-encoded, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`; không URL/key/token/storage exception.

Pool download hiện hành mặc định 4 **mỗi process API**, riêng pool upload; cấu hình ban đầu cần đo, không rate limit/SLO toàn hệ thống. Temp anonymous; không đọc cả paper vào RAM. Attempt shield executor, quan sát HTTP disconnect/task cancellation, giữ slot/temp cho tới thread settled; không close spool dưới thread. `VerifiedFileResponse` đóng owner khi hoàn tất, send lỗi hoặc disconnect ASGI 2.3/2.4; finalizer chỉ fallback, iterator chưa đọc vẫn đóng được. Nếu native cancellation bỏ lại read thread, close chờ owner lock trong executor để giữ slot tới thread settled và không chặn event loop. Không xóa original khi integrity/audit/response lỗi.

Proxy giữ streaming same-origin qua đúng hai route Next; forward header `Cookie` hiện hành tới API, không forward `X-CSRF-Token`. Helper không lọc riêng từng cookie trong header; Cookie/CSRF của browser không tới storage SDK. Clear cookies theo `getSetCookie`; downstream cancel abort fetch và cancel reader, incomplete body kết thúc stream lỗi, không chèn JSON vào file hay tự retry. SDK timeout từng thao tác giữ nguyên, không là deadline tổng và không hứa kill thread treo an toàn.

## Thu hồi và giới hạn

Boundary cuối là current caller/gate/target/clock dưới locks trên transaction response mới. Closure/revoke thắng trước boundary: không file headers/bytes. Boundary thắng: sau nhả locks response có thể truyền tiếp sau thu hồi; request mới/cookie hoặc URL cũ bị chặn. Không recheck mỗi chunk, không hủy hồi tố hay thu hồi bytes đã gửi.

Runtime fixture chỉ scope cutover cho nghiệp vụ; pending HTTP riêng không patch. Auth qua bootstrap/invitation/login/runtime/head/receipt/journal thật; resource setup/corruption có đặc quyền riêng trong DB thử. Legacy common/review dùng engine đặc quyền chỉ cho POST dựng tài nguyên (approve/reject/withdraw/publish/revoke): writer hiện hành chưa có quyền ghi journal mirror bằng runtime (`permission denied for table revocation_journal_records`). Auth/GET vẫn runtime; không thêm grant, không chứng nhận các POST đó runtime-ready. Không mở readiness thật, dev, pricing, provider hoặc C3/C4. Fault injection ở Python/driver boundary không chứng minh mất điện/production durability; proxy/browser smoke không là cutover production.

Regression common cũ coi lần kiểm trong audit là boundary response, nên writer thu hồi ngay sau audit vẫn mong `200`. C2d-4c chuyển ca đó sang `404/no attachment` vì writer đã thắng trước proof response mới; vẫn giữ kiểm writer chờ item lock và không deadlock. Ca mới thu hồi sau proof response cuối vẫn giữ `200`/đúng bytes và request kế tiếp `404`. Không bỏ kiểm quyền/bytes/audit/disconnect để đổi fixture.

## Kiểm chứng

Code chốt `4c49919203db085dacd0778b3ce62e6787fd3823`. Suite cuối **991/991 API pass**, 0 fail/error/skip, 2.288,880 s; JUnit có 991 node unique, đúng selection, không thiếu/thừa/trùng. **269 source hash trước/sau bằng nhau**, không sửa code trong suite. Current collect **3.246 unique**, **2.255 not-run**; node và lý do chọn/chưa chọn trong [inventory](c2d4c-test-inventory.json). Không cộng số lịch sử 463/76/51 của PR #44 hoặc các lượt preparation vào C2d-4c.

| Nhóm chọn | Pass |
| --- | ---: |
| 330 ca mới + socket Next/Chrome | 331 |
| PUT original, đủ 10 regression P2 | 230 |
| Consent/notice/intent | 155 |
| Legacy common/review/upload | 27 |
| Final read clock | 65 |
| Final caller | 93 |
| Runtime login/session | 68 |
| Foundation/journal 19 + runtime reset/invitation 2 + ORM/schema 1 | 22 |

**Web 57/57**, 0 fail/skip; typecheck/build pass. Chrome `154.0.8037.98` bấm anchor same-origin qua Next thật bằng cookie A/B/Admin: **7 tải khớp SHA-256**, **3 ACL denial**; HTTP proxy kiểm `401` clear đúng một session cookie của API hiện hành, `404/409/503` không attachment. Node test riêng giữ hai Set-Cookie synthetic. File tổng hợp **30 MiB** hủy downstream qua HTTP/Next, đóng spool/slot và tải common tiếp được; **0,500 s** là một khoảng tổng gồm verify/auth tới byte đầu, cancel/cleanup và reload tiếp, không latency riêng byte đầu/p95/SLO. Chưa kiểm trọn luồng điều hướng library/review UI hoặc cancel từ UI Chrome.

**8/8 đối chứng** bị bắt bằng assertion, 0 setup error, chạy nối tiếp trước suite cuối; restore byte/hash đủ bốn file. Bỏ private SHA, clock sau gate/predicate hoặc proof sau audit cho `200` thay vì `409/401`; bỏ response/cancel cleanup giữ owner/temp chưa đóng. Bỏ exact review gate cho `503` thay vì `404`; bỏ current-role guard vẫn deny `404` thay vì `403`: hai ca này chỉ chứng minh độ nhạy hợp đồng, không chứng minh bytes trái quyền đã phát. Đối chứng cancel giữ strong reference và chỉ cleanup sau assertion fail; plugin không có trong suite cuối.

Các lượt chuẩn bị có lỗi fixture/registration/privilege, recovery body/time, hook đệ quy, immutable-file fixture, cookie-count và mô hình scheduling ASGI. Chúng cũng phát hiện các lỗi code về ưu tiên recovery gate, owner role và lỗi teardown kép; đã sửa trước source chốt. [Inventory](c2d4c-test-inventory.json) giữ pass/fail/error của từng lượt riêng, không cộng focused/mutant vào 991. Tool cleanup ban đầu từ chối ID Docker rút gọn và BOM của snapshot PowerShell; chưa xóa tài nguyên, sửa parser rồi kiểm lại full ID/metadata trước cleanup.

Đã dọn **2 container/2 volume độc quyền** sau đối chiếu exact ID/name/label/image ID/port/mount/sole consumer. **24 container có sẵn giữ nguyên ID/image/StartedAt**; không đọc `.env` hoặc query/migrate/restart dev. Xóa credentials/env và raw own-container inspect có credentials; giữ metadata, source hash/JUnit/log và journal test ngoài repo. Evidence `%TEMP%/ras-c2d4c-20261006`; không artifact/paper/secret trong commit.

Not-run gồm full business HTTP/provider/search/RAG/worker/cutover ngoài selection, writer review/publication chưa chuyển đổi, C3/C4, production restore/crash/mất điện và benchmark. Không provider/key/pricing, không mở readiness pending. Fault ở Python/driver và môi trường test không chứng minh production durability; PR chỉ để review, chưa merge.
