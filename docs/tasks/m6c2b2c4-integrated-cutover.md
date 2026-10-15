# Prompt agent — C4: bản chạy tích hợp, readiness và restore

## Mục tiêu và base

Triển khai **một PR C4 hoàn chỉnh** để kết thúc đợt chuyển auth/runtime ở gói B: bản cài mới và bản nâng cấp có dữ liệu chạy được với đúng image/DB roles, gate sẵn sàng có hậu điều kiện thật, HTTP/browser và restore không dùng seam bỏ pending. Chủ dự án đã ủy quyền orchestrator chọn cách làm; đây là task coding và kiểm tích hợp, không dừng ở khảo sát hoặc mở thêm PR thiết kế.

PR #60 đã **MERGED** tại `44633b99f8407c5b15a8afb967afcd3c4d135018`; head review `c6f200e7b93ec44b345ac90063a05a6d947ff9ae` là ancestor, tree khớp. Source **0050**; không suy revision DB dev hoặc tính kết quả trước merge là rerun. Fetch/xác minh lại rồi tạo nhánh từ `origin/main`, gợi ý `feat/c4-integrated-runtime-cutover`.

**Mã nguồn hiện vẫn pending. Task C4 cho phép triển khai operational readiness theo hợp đồng đã kiểm trên môi trường cô lập.** Không cho phép cutover stack dev, mở traffic thật, bật provider thật hoặc mặc nhiên ghi production đã an toàn. DB chưa đủ điều kiện vẫn phải pending/503. Giữ mọi docs orchestrator/reviewer modified/untracked, gồm review PR #60 và task này, đưa vào PR; không reset/clean/stash làm mất nội dung.

## Đọc trước khi sửa

- [AGENTS.md](../../AGENTS.md), [mục lục](../README.md), [kế hoạch MVP/checklist/phạm vi các gói](../mvp-delivery-plan.md).
- [Handoff B1, đặc biệt “Gói tích hợp C4”](../pre-c4-auth-receipts-regression.md), [inventory B1](../pre-c4-test-inventory.json), [review PR #60](../reviews/pr60-pre-c4-auth-receipts-regression.md).
- [Thiết kế auth restore và T01–T23](../auth-restore-safety.md), [C3 operator](../c3-operator-auth-recovery.md), [runbook hiện hành](../dev-setup.md), [quyết định](../decisions.md), [quyền/dữ liệu](../access-and-data.md).
- Handoff C2d của worker/index, cleanup, search và RAG trước khi sửa entrypoint/transport/accounting tương ứng.

Rà `auth_safety.cutover`, recovery progress/hold, provisioning, Compose và CLI evaluation thực tế. B1 đã sửa provenance và fixture; không bỏ guard để nối stack. Nếu docs/code khác, kiểm hành vi và ghi quyết định cùng thay đổi.

## 1. Hợp đồng readiness và recovery

Thay placeholder `auth_safety_pending` bằng hợp đồng C4 có kiểm chứng, dùng chung cho API, writer, worker/beat và các final boundary. Chọn cách triển khai tối thiểu phù hợp source; không xây thêm hệ thống điều phối phân tán.

- Phân biệt **binary/schema hỗ trợ contract**, provisioning đúng, registration/journal/installation hợp lệ, replay/backfill/projection đã hoàn tất và integrity/hold hiện tại. Một flag “ready”, revision mới, HTTP 200 hoặc lời xác nhận operator không thay những điều kiện đó. Runtime không tự sửa marker/progress để cấp readiness; nếu cần schema mới, thêm revision sau head thực tế, không sửa `0050` hoặc revision đã merge.
- Các điều kiện phụ thuộc journal phải kiểm lại theo cùng hợp đồng khóa đang có. Journal thiếu/hỏng/unconfirmed, DB sau/trước journal chưa đối chiếu, hold/recovery đang mở, projection/binding/provenance sai hoặc sai runtime identity phải đóng. Gate tĩnh/cached từ lúc khởi động không thay final check trước commit/provider/response.
- Progress phải đúng sau **các mutation bình thường tiếp theo**, không chỉ ngay sau `recover`: login/logout/reset, accept invitation, review closure và revoke không được tự làm service mất readiness vì so với một seq cũ chưa cập nhật. Chứng minh vòng ready → mutation → ready và ready → hold/lỗi journal → đóng → recover hợp lệ → ready.
- Không cấp readiness nhờ owner fallback hoặc ENV/header/route/`--force` bỏ gate. Nếu tách operational contract khỏi kiểm identity theo entrypoint, giải thích rõ để CLI migrator/recovery/operator hợp lệ không bị nhầm với runtime, trong khi API/worker cấu hình owner vẫn bị từ chối.
- `/healthz` là liveness; `/readyz` trả 200 khi instance thật sẵn sàng, 503 khi chưa đủ điều kiện. Paid model chưa xác minh không được làm giả điều kiện auth readiness; từng route trả phí vẫn giữ gate riêng.

### Chốt cách cài mới và account còn cách ly

Không tạo vòng lặp “phải có Admin mới ready, nhưng bootstrap phải ready mới tạo Admin”. **Cho phép DB `new_install` đã được xác minh rỗng, provision/reconcile hoàn chỉnh đạt operational contract để chạy bootstrap runtime một lần**, theo toàn bộ singleton/genesis/receipt guards hiện hành. Bootstrap không tự tạo tài khoản mặc định, không chuyển thành HTTP đăng ký tự do và không dùng để rescue Admin sau restore.

Readiness không có nghĩa mọi account đều usable: held/quarantined/disabled vẫn bị chặn theo từng account; không yêu cầu bật lại chúng để service ready. Với upgrade/restore, quy trình giữ maintenance trong lúc operator phục hồi Admin cần thiết bằng các case C3 thật; credential và initial status riêng, latest disabled không được mở. Kết thúc hold/recovery và kiểm lại trước traffic. Không tự baseline usable/proven hoặc dựng receipt/verifier/User từ journal.

Nếu thêm bước hoàn tất C4/marker, bước đó phải kiểm hậu điều kiện thực bằng quyền quản trị đã provision, có outcome và idempotence rõ ràng; không là cờ bypass. Snapshot mang marker cũ vẫn phải đi qua hold/migrate/recover với journal hiện hành; không dùng marker để bỏ replay/cleanup.

## 2. Compose, image và DB roles

Đưa cấu hình và lệnh dựng về một topology dùng được, thay identity POSTGRES owner đang cấp cho API/worker/beat:

1. Migrator cho DDL/provision và các lệnh recovery quản trị được phép; LOGIN runtime cho API/worker/beat và business CLI; recovery chỉ quyền hạn chế; NOLOGIN definer; operator là login riêng có mapping/case C3. Không để mật khẩu migrator trong environment của runtime service nếu service không cần nó.
2. Provision LOGIN/NOLOGIN và ACL bằng quy trình tường minh, credentials deployment cấp riêng, không mặc định/hardcode; không truyền password trên argv/log. Tái chạy không đổi password/grant của role thuộc project khác. Runtime không owner/DDL/TEMP/BYPASSRLS/raw auth writes, không được thực thi operator hoặc cleanup operator do cấu hình sai. Kiểm bằng login thật và cả quyền âm sau upgrade/reprovision.
3. Cùng source/image digest và revision hỗ trợ contract cho migrate, API, worker, beat và CLI. Thứ tự dependency init/provision/reconcile/start không deadlock vì một service chờ readiness còn service cần hoàn tất readiness lại chưa được chạy. Instance có sẵn cần maintenance phải báo trạng thái/lệnh rõ ràng, không tự `register-existing --confirm-db-is-current` hoặc tin snapshot là live.
4. Journal không nằm trong DB/object backup; test dùng thư mục riêng hiện hành. Bảo đảm UID/permission/mount cho API/worker/CLI và writer serialization. Nhiều process không được nối journal bằng các lock không phối hợp. Không ghi rằng volume khác trên cùng máy đã chứng minh storage production độc lập.
5. Chỉ sửa `.env.example` với secret trống và cấu hình có tài liệu nếu cần; **không đọc/sửa `.env` thật**. Kiểm Compose bằng env file synthetic ngoài repo, project/ports/volumes riêng. Build context không chứa credentials/journal/backup.

C4 vẫn là bản chạy development/cô lập có runbook rõ; HTTPS/delivery production, scheduler/retention/alert và storage production thuộc gói E, không kéo vào task này.

## 3. Luồng hoàn chỉnh qua image thật và trình duyệt

Chạy API, web proxy, worker/beat, PostgreSQL, Redis và MinIO trên project cô lập từ image C4. API không được thay bằng backend host giả. **Không monkeypatch cutover, integrity, hold, receipt, ACL, final caller hoặc outcome** trong luồng nghiệm thu.

- Cài mới: migrate/provision/init/reconcile → operational readiness → bootstrap một lần → invitation/accept/login/reset/logout bằng runtime thật. Giữ secure input của CLI, Origin/CSRF/session clock/generation. Dev outbox chỉ dùng trong môi trường development tổng hợp; không coi nó là kênh delivery production đã xác minh.
- A/B/Admin: upload Markdown và PDF có intent/consent riêng; worker extract → index; coverage/partial/needs_review; original đúng SHA-256; library/chunk/citation đúng version/file/locator; collection, keyword/hybrid, rerank và RAG item/collection/all.
- Review đúng một version/file → approve tạo common pending riêng → publish → common index → download/search/RAG theo từng `permits_*`. Thu hồi chặn request mới trước cleanup, job/provider không công bố sau mất quyền, private cùng bytes và item khác còn quyền không bị ảnh hưởng. Không biến reference list/citation link thành content evidence.
- Có cả được phép và bị từ chối: private khác owner/Admin, scope/version/file sai, quyền external-processing/download thiếu, membership thay đổi, session expired/revoked, rights epoch/tombstone. Kiểm qua API và proxy; browser Chrome thật đi qua luồng chính và mở/tải nguồn.
- Worker/beat dùng runtime trong image mới, xử lý job/cleanup/discovery/citation theo gate; kiểm một crash/resume hoặc Redis outage có kiểm soát trên project này. Không chấp nhận restart loop hoặc all-503 làm nghiệm thu. Reuse process smoke hiện có, không lặp mọi phép thử crash lịch sử khi không có rủi ro mới.

### Provider giả và pricing

Giữ mặc định sản phẩm `pricing_verified=false`, không dùng key thật. Chạy riêng ca mặc định chặn paid call đúng mã và **0 external call**.

Để kiểm `indexed`/hybrid/rerank/RAG `answered`, **được phép dùng cấu hình paid đã xác minh giả chỉ trong test context synthetic**, với key giả và transport/provider giả kiểm soát được. Đây là fixture dependency, không phải xác minh giá thật. Dùng adapter/provider contract thật, model/dimension/identity/usage và ledger/budget thật; không bypass accounting hay auth. Chặn outbound trước khi cấp cấu hình giả; không kế thừa key từ môi trường dev. Có thể inject transport bằng harness thử ngoài app, nhưng API/worker phải chạy code image thật và mọi boundary thật. Không thêm nhánh production `environment=test` để bỏ gate. Ghi đúng lớp đã giả lập, không coi fake usage/charge là charge thật.

## 4. Upgrade và năm full restore drill

Chạy dữ liệu có sẵn từ `0050`, và snapshot legacy đại diện trong các test đã có; **hold bằng code hiện hành trước migrate** phải tiếp tục dùng được. Không dùng schema head giả làm snapshot cũ. Sau upgrade/reprovision/recover, account có proof hợp lệ còn được phép; receipt không chứng minh được vẫn held/quarantined tới case mới.

Chạy đầy đủ, sửa orchestration/helper theo hợp đồng thật nếu cần:

- `scripts/drill_backup_restore.py`
- `scripts/drill_session_revocation_restore.py`
- `scripts/drill_reset_token_restore.py`
- `scripts/drill_review_closure_restore.py`
- `scripts/drill_invitation_restore.py`

Các drill dùng env/context synthetic ngoài repo, runtime riêng, migrator override tường minh cho recovery, cùng image C4 và **không seam cutover**. Không chỉ gọi `seed_accounts` rồi ghi full drill pass. Tái sử dụng scaffold và dùng project/port động hoặc kiểm ownership trước mutation; không ghim container/port lịch sử.

Chứng minh hai vòng dump/restore DB + object thật với journal mới nhất nằm ngoài artifact: sau restore trước recover request nội dung bị 503; sau recover instance ready và đúng quyết định. Common đã revoke, review grant đã đóng, session/reset/invitation đã kết thúc không sống lại. Cookie/token cũ vẫn trong TTL khi kiểm; có control còn hiệu lực để không pass do tất cả đã hết hạn. Case operator dùng rồi không dùng lại; credential mới bị mất do restore không được dựng lại, cần case mới. Account không rollback và private cùng bytes còn quyền vẫn dùng được.

Giữ delete fence/retention/cleanup và artifact checksum. Restore lỗi phải giữ gate đóng; đọc lại bytes object để kiểm SHA-256, không chỉ size/HEAD. Không đảm bảo S3 delete exactly-once hay rollback object cùng DB. Chỉ một DB phục vụ/writer cho mỗi journal khi chuyển snapshot. Không mở rộng bảo vệ sang private deletion chưa triển khai ở gói C, hoặc tuyên bố journal/DB cùng rollback đã được phát hiện.

## 5. CLI evaluation và hồi quy tổng

- Sửa `app.evaluation.runner.environment` để chạy bằng context rõ ràng: migrator chỉ tạo/migrate/provision DB tạm và synthetic metadata setup, business dùng runtime; journal/roles/storage đúng C4. Không implicit đọc `.env` hoặc cổng dev khi context thiếu. CLI hiện còn owner/`.env` legacy dù seed đã sửa; phải thực chạy CLI, không chỉ unit-test `seed`.
- Chạy validate và evaluation `run` trên corpus hiện có, embedder/generation capture cục bộ, chặn HTTP ngoài context. Giữ nhãn `pending_human_review`; không cần duyệt lại nhãn, tuning retrieval hoặc chấm semantic trong task này. Chứng cứ này xác nhận tool chạy được, không chứng minh chất lượng production.
- `test_health.py::test_readyz_ok_after_migration` đang là node deferred duy nhất của B1: **giữ expectation 200**, dựng fixture có registration/provision/reconcile/operational contract thật và runtime identity đúng. DB chỉ migrate nhưng chưa hoàn tất hợp đồng phải có ca negative 503 riêng; không nới gate để tên test cũ pass.
- Chuyển shared business fixture và suite HTTP hoàn chỉnh sang readiness thật, không trả ready bằng lambda. Test lịch sử C1–C3 cho trạng thái pending hoặc unit fault isolation được giữ riêng và ghi rõ; không tính fixture có seam là nghiệm thu C4 không seam.
- Collect/run toàn API inventory cuối theo node ID, JUnit/source hashes, không thiếu/trùng; không bỏ debt lịch sử. Chạy web tests/typecheck/build và Chrome như trên. Reuse runner, selection qua `@args`; chỉ shard nếu DB roles/journal/resources không đụng nhau. Theo dõi exit/process/log, không chờ vô hạn file kết quả sau khi process đã chết. Giữ lượt gián đoạn/fail/rerun riêng, không suy pass từ dấu chấm.
- Ca discovery `test_persistent_oai_deleted_record_is_hidden_and_checkpointed` đã từng fail rồi rerun pass cùng source, chưa xác lập nguyên nhân. Thêm diagnostic outcome/lease/gate trong harness khi chạy tích hợp; nếu tái hiện, sửa đúng nguyên nhân. Không thêm retry sản phẩm hoặc quy lỗi cho Windows mà không có chứng cứ.
- Countercheck vài guard C4 quan trọng (identity, contract/progress, hold/journal, final recheck) bằng assertion nghiệp vụ; khôi phục bytes trước suite cuối. Không chạy mutant/sửa source khi suite chính đang chạy. Không tạo thêm hệ thống seal/báo cáo đồ sộ nếu tooling hiện có dùng được.

## Hoàn thành, giới hạn và bàn giao

Task hoàn thành khi cài mới và upgrade cô lập đạt ready có điều kiện, API/worker/beat dùng đúng runtime/image và ổn định, luồng được phép thật sự thành công, các ca trái quyền thật sự bị chặn, năm full drill và CLI evaluation thực chạy được, inventory cuối đối chiếu đầy đủ. Lỗi quyền/nghiệp vụ/journal không được miễn bằng nhãn baseline. Nếu còn blocker, ghi chưa hoàn tất phần tương ứng; không tự chia thêm vi PR hoặc mở ready để giấu lỗi.

Chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`, kiểm interpreter/pip, không tạo venv. Chụp baseline Docker; mọi thao tác chỉ trên project/context do task tạo. Kiểm label/path/mount trước dọn, không prune; giữ tài nguyên có sẵn, ghi drift không suy đoán. Không đọc `.env`/key, query/migrate/restart dev, gọi arXiv/provider thật hoặc đổi pricing của môi trường thật. Không dùng host fallback để nghiệm thu API image.

Một handoff/inventory tập trung, gợi ý `docs/c4-integrated-runtime-cutover.md` và `docs/c4-test-inventory.json`, ghi contract/roles/images/commands thực chạy, topology và các lớp giả lập, từng luồng/drill, số pass/fail/error/skip/not-run, lỗi chuẩn bị, cleanup và giới hạn. Cập nhật ngắn kế hoạch MVP, decisions, auth restore, dev setup, README/mục lục/AGENTS; giữ report cũ đúng source. Không ghi lệnh dự kiến như đã kiểm, không nhân bản lịch sử kiểm ở đầu mọi file.

Runbook dev cutover phải cụ thể và review được nhưng **chưa được chạy trên dev**; production vẫn cần storage journal độc lập/hiện hành, ủy quyền người thật, delivery/HTTPS/backup vận hành và charge/routing/retention ở các gói sau. Chỉ đánh dấu checklist thực được chứng minh; fake provider không chứng minh chất lượng/chi phí, fault injection/SIGKILL không chứng minh chịu mất điện, DB/journal cùng rollback còn giới hạn.

Commit/push và mở **PR tiếng Việt**, không merge; không thêm ghi nhận AI vào tác giả/committer/trailer/PR. Giữ docs orchestrator/reviewer trong PR. Báo base/code/head, source revision, bằng chứng thành công và từ chối, việc chưa xong và điều kiện cho bước tiếp theo. Đích là bản tích hợp dùng được trên môi trường cô lập, không mở thêm tính năng ngoài checklist MVP.
