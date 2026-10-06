# C2d-4a: upload notice và intent

**Đã triển khai trên nhánh review, chưa merge.** Base fetch `99db5285dfa59dd3d26e86345e849393ed88ce33`, PR #42 MERGED và chứa head `8f924c0`. Chỉ hai JSON route; migration source `0039`, không đổi schema/grants và không suy revision DB dev. Safety **pending tới C4**.

[Mục lục](README.md) · [Task](tasks/m6c2b2c2d4a-upload-notice-intent.md) · [Test inventory](c2d4a-test-inventory.json) · [Caller inventory](c2d1-caller-inventory.json)

## Đối chiếu và hợp đồng

Base trả notice từ settings sau dependency; intent dùng principal/role và `now` đầu request, raw commit rồi trả token/ORM mà chưa có proof mới. Đây là khoảng trống được task yêu cầu chuyển. `Literal[True]` của Pydantic còn nhận số `1`: nay dùng `StrictBool` và validator chỉ cho boolean `true`; thiếu/false/null/số/chuỗi/extra fields đều bị từ chối. Giữ filename/kind/size/SHA-256/config và `notice_changed`, không có file trong JSON.

Notice vẫn đọc được bởi User/Admin hợp lệ, không tạo acknowledgement. Intent chỉ cho **current role User**, Admin `403 forbidden`. Session/CSRF/Origin dùng dependency thật; route dùng `require_trusted_origin` với cùng quy tắc thiếu/sai/lặp Origin. SHA-256 khai báo chưa chứng minh bytes; xác minh file thuộc PUT tiếp theo. Mỗi lần tạo acknowledgement và intent/token riêng, DB chỉ lưu hash. Audit chỉ actor/target ID, action/outcome và notice version; không filename, token, cookie, CSRF hoặc payload.

Validation nghiệp vụ notice/filename/limit chạy **sau current User guard trong boundary**, giữ ưu tiên Admin `403` kể cả notice cũ, filename sai hoặc size vượt giới hạn. Pydantic vẫn kiểm schema/strict consent trước handler như hợp đồng hiện hành. Tự review đã tái hiện lệch thứ tự ở source `27dd4c5` + test bổ sung (1 failure: 409 thay vì 403); sửa ở `610e52d`, cùng ca đạt. Suite 579 tại `27dd4c5` giữ lịch sử riêng, selection được chạy lại sau sửa.

## Boundary và lock map

[`GET notice`](../api/app/api/routes/uploads.py) gọi `final_caller` trên transaction mới sau dependency: exact session/User, current scalar profile/role, latest confirmed head/receipt/generation và clock sau predicate SQL cuối. Helper không touch, khóa hàng hay commit; route giải phóng journal shared bằng rollback khi rời boundary. Caller hết hiệu lực → `401 session_expired`, clear cookie; gate đóng → `503 service_recovering`, không notice.

[`api/upload_intents.py`](../api/app/api/upload_intents.py) là helper hẹp riêng cho secret/TTL, tái sử dụng `_actor` C2d-3 ở entry; không sửa helper đó hoặc các writer khác. Không dùng `commit_auth`, không tạo auth journal event cho consent.

| Giai đoạn | Khóa và kiểm |
| --- | --- |
| Entry writer | `require_writer` integrity thật → runtime User `NO KEY UPDATE` → scalar actor/session/CSRF/current User; clock sau SQL |
| DML | acknowledgement INSERT/flush → intent INSERT/flush → audit INSERT/flush; mọi FK trước journal |
| Precommit | `require_final(writer=True)` exclusive trực tiếp, không shared upgrade → expire identities → joined scalar intent/ack binding → caller scalar + predicate → một clock sau SQL cuối cho cả TTL; không DML/khóa hàng mới |
| Commit | Một business commit; User và journal giữ tới commit; không retry/replay |
| Known commit response | Transaction mới, expire identities → intent SHARE → acknowledgement SHARE → journal shared/gate → token hash và scalar binding/metadata/notice/unused → exact caller/current User/CSRF + predicate → clock sau SQL cuối cho cả caller và intent → token |
| Mọi exit | Rollback/close giải phóng transaction/connection; cleanup local không chứng minh remote commit outcome |

Binding gồm intent/ack ID, User/session/notice, kind/filename/declared size/checksum/token hash, created/expires time, acknowledgement timestamp; `used_at` và `item_id` phải NULL. Lifetime lấy config từ mốc mới sau entry lock/check, không gia hạn sau SQL wait hoặc commit. Response không dùng ORM/proof trước commit.

PUT hiện lấy intent UPDATE trước transfer/parser/storage, sau đó ghi item/version/job/source/FK/audit và commit. Response reader C2d-4a lấy intent rồi acknowledgement trước journal; không lấy User/FK locks sau đó. Vì vậy PUT phải chờ SHARE reader trước khi claim/ghi intent; reader đợi PUT UPDATE xong mới đọc unused lại. Đây chỉ là lock compatibility: **PUT chưa có final caller/boundary mới**, vẫn giữ lock qua I/O, không được mô tả toàn upload đã an toàn.

## Lỗi và giới hạn commit

| Giai đoạn | Mã và trạng thái |
| --- | --- |
| Auth/role/CSRF/gate | `401 session_expired` + clear cookie; `403 forbidden/csrf_failed`; `503 service_recovering` |
| Binding/unused/metadata đổi | `409 upload_intent_changed`; không token |
| Intent TTL | `409 upload_intent_expired`; không token, không gia hạn |
| SQL/flush/lock lỗi trước commit | `503 upload_intent_unavailable`; rollback toàn ack/intent/business audit |
| Commit exception, dù đã landed | `503 upload_intent_outcome_uncertain`; không token, retry/replay hay suy đã lưu/chưa lưu |
| Known commit, DB hậu kiểm lỗi | `503 upload_intent_response_unavailable`; ack/intent có thể đã lưu, không token |
| Known commit, auth/role/TTL/binding/gate mất | Giữ mã từ chối tương ứng; rows có thể còn tới expiry, không tự xóa hoặc hứa mutation rollback |

Web chỉ thêm error mapping; form giữ checkbox trống/xác nhận riêng và POST intent trước PUT. Không tự gửi lại khi lỗi, không dùng mã lỗi để suy chưa có gì lưu. Anonymous với Origin/CSRF hợp lệ nhận `401`; middleware có thể từ chối `403 csrf_failed` trước auth khi thiếu CSRF, giữ hợp đồng Origin/CSRF hiện hành.

Response đã qua final boundary và nhả khóa có thể tiếp tục truyền sau thu hồi; không lấy lại bytes đã gửi. Intent/ack không có journal xuyên restore trong PR này. Không đổi file download, provider/search/RAG, ACL, quota/model/pricing, job/worker, `.env` hoặc dev. C2d-4b nhận PUT (parser/storage, claim, before save/job/commit, unknown commit/orphan); C2d-4c nhận private/common/review download (after I/O/before byte đầu, checksum/slot/disconnect). Writer/provider khác và C3/C4 vẫn mở.

## Kiểm chứng

**Suite cuối tại code/test `610e52d22292e16534c7c1771ca40fe6528b467a`: 579/579 pass, 0 fail/error/skip.** Collect 2.685 unique nodes, **2.106 not-run**; hai JUnit không trùng, hợp đúng selection. `final-core`: 313 / 783,825 s; `final-reg`: 266 / 409,836 s, chạy đồng thời trên DB/journal fixture riêng. SHA-256 **259 API/web source files** (220 Python/SQL API + 39 TS/TSX/MJS web) giữ nguyên trước/sau cả hai nhóm. Dirty trước suite chỉ docs chưa commit; không sửa source/test/mutant khi chạy. [Từng node, JUnit/source SHA, preparation và not-run](c2d4a-test-inventory.json).

| Nhóm cuối | Pass |
| --- | ---: |
| C2d-4a runtime upload | 155 |
| Final clock PR #42 | 65 |
| C2d-1 final caller | 93 |
| C2a login/session | 68 |
| C2a Origin | 66 |
| C2d-3 writer success/clock/uncertain/postcommit/pending/waits | 50 |
| Config | 54 |
| Migration/ORM | 28 |
| **Tổng** | **579** |

Runtime role LOGIN `c1_runtime`, accounts qua bootstrap/invitation/login/receipt/head/journal thật. Nghiệp vụ chỉ scoped cutover; hai pending HTTP upload cùng các pending hồi quy giữ gate thật. Clock trong chính predicate SQL cuối kiểm idle/absolute/intent tại/quá/sát trước hạn, không gia hạn; intent timestamps lấy config và clock sau dependency/lock. Exact binding từng trường trước/sau commit, User/session khác/Admin, raw token/hash, role/CSRF/latest head/receipt/quarantine/held, flush/SQL/commit/postcommit faults đều được kiểm. Token chưa trả khi auth/role/TTL/binding/gate mất hoặc DB recheck lỗi dù rows đã lưu.

Races dùng Event và quan sát `pg_stat_activity.wait_event`: User/journal waits, timeout, auth closure/writer với hai bên thắng, và response SHARE/intent claim UPDATE với hai bên thắng. Claim là **mô phỏng cùng khóa intent bằng SQL runtime**, không gọi PUT/parser/storage/job; không là kiểm toàn PUT. Success SQL trace chứng minh flush FK trước exclusive, response ở transaction mới và resource locks trước shared, không DML/row locks sau journal. Pool trả về trên các đường success/denial/fault/race. Journal không thay đổi vì consent bình thường, không item/version/file/job/chunk/index hoặc provider/storage I/O.

Ba đối chứng tuần tự **3/3** bị đúng assertions bắt: bỏ notice final caller, bỏ `_check` trước commit (generation fault), bỏ `_check` response (current role đổi). Mỗi lượt đúng 1 failure, 0 error/skip, bytes/SHA khôi phục trước suite cuối. Đây là fault injection ở Python boundary có SQL/commit hoặc rollback thật, không là crash mạng/mất điện hay bằng chứng durability production.

Lịch sử chuẩn bị giữ riêng: observer đầu 1 failure vì nhầm khóa của transaction response thành khóa sau journal precommit; sửa observer. Expanded 132 pass/10 fail: anonymous thiếu CSRF cookie và chín hook postcommit so `None` với `False`, không chạy injection; sửa fixture, không nới guard. Refined 24 pass (gồm races), late 4 pass; không cộng các lượt chồng nhau vào 579. Selection parser đầu đọc PowerShell UTF-16 sai nên thu 0 node, phát hiện trước khi chạy selection; sửa decoding/assert inventory. Đối chiếu JUnit đầu dừng vì chuẩn hóa toàn node làm mất dấu `\` trong đúng một tham số Origin; đã xác minh mapping 1:1 trên 2.685 node, chuẩn hóa chỉ path cho báo cáo, giữ execution selection gốc. Không đổi identity test/source hoặc coi đọc artifact là chạy lại.

Web **47/47**, typecheck và build đạt tại `27dd4c5`; web source không đổi trong sửa `610e52d`, hashes cuối khớp, không tính là chạy lại web ở commit mới. Test smoke chạy callback form thật bằng hook host, `api()` và proxy tới HTTP upstream tổng hợp: checkbox ban đầu trống, chưa xác nhận không POST, intent lỗi không PUT/retry, confirmation reset sau lỗi. Không browser DOM/full product HTTP hoặc truyền file thật. Proxy/form code không đổi, chỉ error mapping/test mới.

Conda ras interpreter/pip đã xác minh; PostgreSQL standalone `ras-c2d4a-db-20261006`, label `ras.task=c2d4a-20261006`, `127.0.0.1:58454`, journal/credentials/evidence tổng hợp ngoài repo `%TEMP%/ras-c2d4a-20261006`. Runner explicit DB parts/admin URL trước fixture, chặn HTTP transport sync/async; không `.env`/dev/provider/pricing/safety ready. Sau suite đã kiểm ID/label/image/port/mount/sole consumer rồi dọn đúng container/anonymous volume và hai file credentials. **24 container có trước task giữ nguyên ID/image/StartedAt**, không query DB dev. Journal/JUnit/source/selection/log/scripts tổng hợp giữ ngoài repo.

2.106 node chưa chạy được liệt kê theo file/lý do: helper/resource paths không đổi ngoài representative selection; full PUT/download/parser/storage và legacy fixtures; provider/search/RAG/worker, review/publication writers, nền/recovery/restore/drill ngoài scope. Full product/proxy/browser/cutover/production durability và C3/C4 chưa chứng minh. AST giữ nguyên `upload_original`, `download_original`, `_validate_file`, `_delete_if_unreferenced`; identity/predicates/auth_safety/authz/config/migrations không đổi. Không dùng số 124/1094 lịch sử PR #42 làm kết quả mới.
