# Prompt coding M6c-2b2c1 / C1 — nền auth safety

[Mục lục](../README.md) · [Quyết định nền đã chốt](../decisions.md#quyết-định-nền-c1-theo-ủy-quyền-2026-10-03) · [Thiết kế](../auth-restore-safety.md) · [Hợp đồng kiểm thử pending](../auth-restore-safety.md#hợp-đồng-kiểm-thử-khi-c1c3-còn-pending)

## Giao việc và nguồn xác nhận

Bạn là coding agent chính. Bắt đầu **coding C1** theo thiết kế auth sau restore; hoàn tất triển khai, kiểm chứng, commit/push và mở PR tiếng Việt để chủ dự án review, chưa merge.

Chủ dự án đã yêu cầu orchestrator: “Tùy bạn quyết định sao cho hợp lý, thuận tiện nhất”. Orchestrator đã chốt A, baseline legacy một lần, C1–C3 chỉ thử cô lập/pending và runtime/migrator với trusted identity rehash. Quyết định có hiệu lực trong phạm vi C1; không xin xác nhận lại bốn đầu vào này, không tiếp tục một lượt chỉ khảo sát. Không ghi rằng chủ dự án trực tiếp chọn từng phương án.

Operator thực tế, inventory UUID/chứng cứ, quyền initial status/Admin, delivery và cutover/storage production theo milestone riêng. Chúng không chặn C1. Origin compatibility được hoàn thiện trước C2.

## Chuẩn bị và nhánh

1. Đọc [AGENTS.md](../../AGENTS.md), CLAUDE.md, mục lục docs; thiết kế auth, quyết định nền, data-model, access-and-data, testing, dev-setup và hợp đồng journal session/reset/invitation.
2. Inspect status/diff trước khi chuyển nhánh. Các cập nhật quyết định và prompt của orchestrator có thể đang chưa commit trong workspace; giữ nguyên, đưa vào PR C1. Không reset/clean hoặc bỏ chúng vì chưa có trên origin/main; nếu có thay đổi ngoài phạm vi, giữ riêng và báo rõ.
3. Fetch origin; xác minh main chứa PR #32, merge đã quan sát `a5c75b88da72eaf3d8a28c0c00d82d12099f28a4`. Tạo `feat/m6c2b2c1-auth-safety-foundation` từ main mới nhất, giữ tài liệu bàn giao. Báo base SHA/head migration thực tế; không suy revision DB dev từ source.
4. Trước code, nêu ngắn phạm vi, mapping quyết định vào schema/routine và mâu thuẫn code/docs. Giải quyết chi tiết triển khai thường lệ trong phạm vi đã chốt, ghi lý do cùng thay đổi.

## Phạm vi C1

### Schema và phân quyền

- Thêm revision mới sau head thực tế, không sửa migration đã merge. Nếu head vẫn `0032`, revision tiếp nối nó; không ấn định số trước khi kiểm.
- Thực hiện schema nền theo thiết kế: User credential/status generation, state/safety, operation/reason; receipt append-only với FK ghép đúng User/domain/generation/operation; projection auth heads không FK User; typed mirror của hai auth kind; session generation; reset-token fence/purpose; safety version và tiến độ projection/backfill.
- Legacy không có default usable/proven; giữ verifier, role và business status. Migration chỉ tác động DB, không I/O journal.
- Tách kết nối/quyền runtime API/worker và migrator/owner trong cấu hình môi trường thử. Runtime không sở hữu bảng/routine, không superuser/DDL/BYPASSRLS/tắt trigger hoặc quyền chuyển sang migrator. Các auth mutation được bảo vệ chỉ qua routine được cấp quyền; không raw-write receipt/head để vượt hợp đồng.
- Routine có quyền tối thiểu, search_path cố định, thu hồi EXECUTE từ PUBLIC. Recovery chỉ thực thi hạn chế được phép; không có routine replay cho active/usable thiếu chứng cứ.
- Xây guard/routine CAS rehash nền và kiểm trực tiếp: old verifier/generation/operation/state, PHC/policy hợp lệ; giữ generation/password_changed_at. Không GUC/caller reason bypass. Trusted identity code chịu trách nhiệm verify cùng mật khẩu; chuyển verify/login/writer thuộc C2.

### Parser, batch và generation

- Thêm credential_transition và account_status_decision theo body/reason/decision trong thiết kế. Python parser và SQL kiểm độc lập: đúng field/type/UUID/time, missing/null/extra, ID/domain/operation/generation binding; từ chối trước append.
- Mirror typed cột đúng body, không nhận SQL UNKNOWN. Giữ append-only và chỉ confirm false → true với mọi cột còn lại bất biến.
- Operation/generation dùng CSPRNG UUID; kiểm không tái dùng trên toàn journal hiện hành ở mọi User/domain, gồm event rollback và User absent. DB unique chỉ là lớp bổ sung. Seq theo head journal độc lập, không timestamp/counter snapshot.
- Receipt là chứng cứ transaction DB, journal_confirmed không chứng minh nghiệp vụ commit. Receipt mới chỉ đi cùng mutation hợp lệ, không được dựng từ replay/backfill.
- Toàn mirror batch false và flush trước append; auth event trước closure, giữ thứ tự tương đối invitation → reset → session. Confirm cả batch sau append/fsync thành công; lỗi ghi giữ toàn batch false.
- Không password/verifier/fingerprint verifier/token/email/payload delivery trong journal/log/audit.

### Replay và backfill

- Mirror/kiểm toàn chain, fold latest theo seq từng User/domain rồi mới đối chiếu snapshot và áp hạn chế. Kiểm hậu điều kiện cả record đã present; không chỉ skip seq có trong mirror.
- Không để disable cũ phá snapshot có enable mới hơn cùng receipt hợp lệ. Giữ trạng thái snapshot phù hợp bằng chứng; không tạo verifier/receipt/User/role hoặc tự giải phóng quarantine/held từ replay.
- User absent chỉ có mirror/projection, không chuyển quyết định theo email.
- Replay file ahead trước backfill. Mọi credential legacy chưa có bằng chứng, kể cả Admin, quarantine; legacy active held. Legacy disabled chỉ journal hạn chế khi chain chưa có status event cho UUID đó; không ghi đè quyết định mới hơn.
- Đóng session/reset token legacy thiếu generation/fence qua batch closure hợp lệ với auth_safety_backfill; cập nhật parser, enum và DB đồng bộ. Giữ seq liên tục, retry/idempotence và hậu điều kiện journal trước đó.
- hold trước migrate vẫn chỉ đọc seq/hash và registration tương thích snapshot cũ. init/register-existing/confirm-db-is-current không baseline credential/status thành usable/proven active.

### Gate, recovery và phạm vi phục vụ

- Safety mới giữ pending tới C4. HTTP được bảo vệ, worker/beat/process_job và mọi auth writer chưa chuyển hợp đồng đều bị chặn, gồm bootstrap và đường batch closure rỗng. /healthz sống; /readyz không ready.
- Tách kiểm integrity/hold khỏi pending cutover; không flag/env/header/CLI/nhánh test trong production để bypass gate. recover phải báo riêng đối soát đã hoàn tất và safety chưa cho phục vụ; không gọi pending hợp lệ là journal hỏng hoặc báo traffic ready.
- Tuân thủ thứ tự khóa từng flow trong thiết kế, journal sau khóa hàng; không giữ journal lock rồi lấy User/item của event tiếp theo.
- Compose hiện dùng /readyz làm API healthcheck và web chờ API healthy. Smoke dùng override khởi động **project cô lập** để kiểm API/proxy khi readiness còn 503; không đổi /readyz thành 200, không coi toàn stack healthy là tiêu chí C1. Giữ kiểm gate thật.
- Chưa triển khai auth writer/predicate/luồng login mới C2, recovery User/Admin/delivery C3 hoặc safety ready/cutover C4. Schema/routine nền cần kiểm trực tiếp thuộc C1.

## Kiểm chứng và tiêu chí hoàn thành

Theo T15–T19/T21/T22 ở tầng C1 đã triển khai, dùng PostgreSQL và journal tạm thật:

| Nhóm | Phép thử bắt buộc |
| --- | --- |
| Schema/role | ORM/schema match; upgrade từ 0032 có dữ liệu, hold snapshot cũ, downgrade fence. SQL dưới runtime thật có ca routine hợp lệ được phép và raw mutation/DDL/GUC bypass bị chặn |
| Parser/binding | Missing/null/extra, sai UUID/User/domain/op/generation/reason/time; mirror/body/receipt FK ghép; append-only/confirm-only; User absent hợp lệ với ID khớp |
| Generation/seq | Collision/reuse xuyên User/domain, event rollback/absent, retry quyết định mới, seq liên tục |
| Batch lỗi | Append none/torn/prefix/full-fsync-error × DB commit/rollback/uncertain; không confirm sớm, prefix chỉ áp hạn chế có thật |
| Replay/backfill | Fault từng bước, present-record hậu kiểm, fold hai pha, không dựng receipt/active, chạy lại idempotent, file ahead trước backfill, legacy không usable |
| Hồi quy trực tiếp | Common/review/session/reset/invitation có cả ca được phép và bị chặn; fence/cleanup/hậu điều kiện cũ giữ nguyên, không dùng global 503 làm chứng cứ |
| HTTP pending | Không mock: auth/library/file/search/RAG/Admin/collection bị 503, health sống, ready không ready; worker/auth CLI không đi vòng gate |
| Secret | Marker tổng hợp không ra journal/log/audit, parser từ chối field secret; verifier/token hash vẫn chỉ ở bảng auth đúng quyền |

Không đổi hàng loạt expectation nghiệp vụ cũ thành 503, không tính skip/hoãn là pass. Báo inventory riêng: đã chạy, pass/fail, HTTP nghiệp vụ hoãn tới C4 và test tầng dưới thay thế. Không tuyên bố full API hồi quy đã đạt nếu ca được phép chưa thực thi.

Kiểm đối chứng guard trọng yếu tuần tự, khôi phục trước lượt kiểm chính; không sửa code hoặc chạy mutant song song suite đang đo. C2/C3 sau này chỉ patch pending cutover tại test fixture theo thiết kế, giữ integrity/auth/hold/DB guard thật; C1 kiểm HTTP pending không patch.

## Môi trường và bàn giao

- Python/pip host dùng C:\Users\ADMIN\.conda\envs\ras; kiểm interpreter/pip trước khi chạy/cài, không venv.
- Project/DB/journal/ports/credentials thử tường minh và cô lập. Đặt RAS_TEST_DATABASE_ADMIN_URL thử trước pytest để tránh fixture fallback .env. Không đọc/sửa .env, migrate/restart hay tác động tài nguyên dev; không lấy artifact/journal thật làm fixture.
- Không gọi OpenRouter, giữ pricing_verified; không commit artifact/journal/paper/secret. Fault injection/fsync thành công không là chứng cứ mất điện hoặc độ bền production.
- Cập nhật docs cùng code: trạng thái C1 đang review, schema/role/config, kết quả test thực chạy, pending và giới hạn. Chỉ thêm lệnh đã chạy thật vào AGENTS/dev-setup; không ghi schema/CLI chưa triển khai là tính năng đã có.
- Đưa các cập nhật quyết định/prompt bàn giao vào PR C1. Commit/push/mở PR tiếng Việt theo AGENTS; không merge.

Báo cuối: base/commit/PR, file thay đổi, schema/role/routine và replay/backfill, lệnh/inventory/kết quả thực chạy cùng ca hoãn/lỗi, xác nhận pending và tài nguyên dev giữ nguyên. Bàn giao C2 mapping toàn writer/predicate, receipt/projection, token/session fence và lock order; chỉ rõ quyết định còn mở theo milestone. Duy trì tiến độ tới khi phần coding và kiểm chứng C1 hoàn thành.
