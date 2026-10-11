# C3 — Khôi phục credential và initial status qua operator

**Mới giao ngày 2026-10-11; chưa triển khai hoặc kiểm chứng C3.** PR #58 đã merge tại `58d059ab7a383d5f97e1b04e909e9628a11bf78a`; head review `aa9c8a2` là ancestor và tree khớp. Source migration **0048**, không suy revision DB dev. [Review PR #58](../reviews/pr58-c2d7-rag-generation-runtime.md) giữ chứng cứ trước merge, không là kết quả chạy mới.

## Mục tiêu và cách tổ chức

Hoàn thành **một PR C3** để operator được ủy quyền khôi phục đúng tài khoản đã có sau restore: inspect → xác minh hồ sơ ngoài app → đặt credential mới → quyết định initial status riêng khi đủ điều kiện → đối chiếu outcome. Đường này hoạt động cả khi mọi Admin bị cách ly và không đọc được outbox, đồng thời không làm sống lại account đã disabled hoặc credential cũ.

Đọc [kế hoạch MVP](../mvp-delivery-plan.md), [thiết kế auth restore](../auth-restore-safety.md), [C1](../c1-auth-safety-foundation.md), [C2b](../c2b-reset-status.md), [bootstrap](../c2c1-bootstrap-receipts.md), [invitation](../c2c2-invitation-runtime.md), `app/{auth_safety,recovery,journal,db_roles}.py`, các auth writers và migration liên quan trước khi sửa. Giữ hợp đồng sản phẩm trong [requirements](../product-requirements.md) và [access/data](../access-and-data.md).

Sau C3 chuyển sang **xử lý debt và tích hợp C4** trong gói B. Không mở thêm đợt chia runtime theo endpoint. Safety **vẫn pending tới C4**; C3 không sửa `cutover` thành ready hoặc thao tác môi trường thật.

## Các quyết định đã chốt cho task

Orchestrator chọn dưới sự ủy quyền của chủ dự án về cách hoàn thành MVP; không ghi rằng chủ dự án đã trực tiếp duyệt từng lựa chọn kỹ thuật hoặc cung cấp danh tính thực.

1. **Thẩm quyền tối thiểu:** chủ dự án, hoặc một người được chủ dự án chỉ định trực tiếp, là recovery operator. MVP cho phép cùng người này duyệt hồ sơ và thực hiện CLI; không xây hệ thống phê duyệt nhiều tầng. Phải ghi rõ sự tin cậy này. Host/DB access, email, cookie cũ và biết mật khẩu snapshot không tự chứng minh người nhận là chủ đúng User UUID.
2. **Inventory và hồ sơ ngoài backup:** trước vận hành thật phải có liên kết installation/journal ID + User UUID + role dự kiến với người nhận, được kiểm qua kênh riêng. Case có UUID, operator/approver UUID, đúng target/installation, một hành động được duyệt, mã phương thức kiểm chứng, thời điểm duyệt/hết hạn và expected auth heads. Chi tiết danh tính/kênh/tài liệu để ở hồ sơ hạn chế ngoài journal và repo. Trong task chỉ dùng hồ sơ/tài khoản tổng hợp; không giả tạo inventory thật.
3. **Boundary máy:** login operator riêng, ánh xạ rõ login thực sang operator UUID qua provisioning tin cậy. Dùng `session_user`/bằng chứng login thực, không tin `--operator-id`, tên role, `current_user` của SECURITY DEFINER hay lỗi probe để cấp quyền. Runtime, recovery replay, migrator và operator credential có chức năng riêng; operator không cần owner/superuser/DDL/TEMP/BYPASSRLS hoặc raw auth writes. Người provision được tin cậy; CLI không tự đăng ký operator từ input bất kỳ.
4. **Credential:** ưu tiên CLI nhập hai lần qua TTY không echo, có kiểm TTY thật và policy. Không password qua argv/env/file/stdin không TTY; hash/verify ngoài transaction. Không cấp cookie, reset link hay session. Với quarantine, từ chối mật khẩu trùng verifier snapshot đang có; không tuyên bố kiểm được toàn bộ mật khẩu lịch sử đã mất.
5. **Status riêng:** `set-credential` giữ nguyên role, business status và `status_safety`. `resolve-initial-status` là **quyết định active mới**, cần case riêng, chỉ cho account hiện `active/held` và latest status head chưa có hoặc là active thiếu receipt hợp lệ. Không áp cho business status disabled hoặc latest journal decision disabled; không thay receipt cho account đã proven hợp lệ. Không operator-enable, đổi role hoặc bootstrap lại. Admin duy nhất disabled tiếp tục bị khóa; Admin active/held chỉ mở được sau quyết định status riêng có chứng cứ, không suy từ role trong snapshot.
6. **Maintenance và delivery:** mutation CLI yêu cầu maintenance hold hợp lệ, journal và DB đã đối chiếu/replay/backfill xong. Hold và pending không phải lý do cho qua missing/torn/diverged/behind/unconfirmed chain. Nhập credential trực tiếp trên thiết bị/kênh do người nhận hoặc operator được ủy quyền kiểm soát là đường recovery MVP; không cần email service hay dev outbox. Quarantine reset qua web tiếp tục không công khai cho tới khi có delivery được kiểm chứng; normal reset/invitation giữ hợp đồng hiện có. Delivery nhóm thử thuộc gói E.

## Triển khai

### 1. Base, hồ sơ và provisioning

- Fetch, xác minh PR #58 đã merge và head thực tế. Tạo `feat/m6c2b2c3-operator-auth-recovery` từ `origin/main`; nếu head tiến lên thì ghi đúng base mới, không ghim revision theo prompt.
- **Giữ và đưa vào commit/PR** toàn bộ docs orchestrator modified/untracked, gồm review PR #58, task này và cập nhật kế hoạch. Có thể commit docs có sẵn riêng. Không reset/clean/stash để làm sạch chúng.
- Chọn một định dạng case/inventory tối thiểu, strict schema và nguồn tin cậy rõ. Có thể dùng file được quản lý quyền ngoài repo cùng provisioning DB hẹp; không cần PKI hay dịch vụ approval mới. Một cờ `verified=true`/UUID tự khai không thay được đối chiếu hồ sơ và binding đã được cấp quyền.
- Case bind đúng action, operator login/UUID, installation, User UUID, role và expected credential/status head; kiểm thiếu/null/sai type/UUID, hết hạn, stale head, dùng sai action/target và dùng lại. Null legacy là trạng thái được khai báo và so NULL-safe, không là wildcard. Kết quả đã có chỉ được inspect; không lặp mutation khi outcome chưa rõ. Chống dùng lại quyết định cũ cả sau restore/replay theo journal hiện hành, không chỉ dựa vào hàng case trong snapshot.
- Thêm migration mới sau head thực tế khi cần schema/routine/grant; giữ mọi migration đã merge. Đồng bộ ORM/schema, provisioning idempotent và upgrade DB có dữ liệu/role đã provision. Chặn downgrade làm mất bảo vệ trên installation đã đăng ký.
- Routines mới có fixed search path, quyền hẹp và binding DB độc lập. Không grant cho operator generic mutation đủ để tự tạo User/receipt/status hoặc sửa journal tùy ý. Kiểm các routine sẵn có để runtime/recovery không giả được `verified_recovery`/`verified_initial_status` qua đường generic; giữ bootstrap/invitation/reset/status hợp lệ. Không nới grants toàn bộ chỉ để chạy CLI.

### 2. CLI và các proof

- Thêm `python -m app.cli.auth_recovery` với `inspect`, `set-credential`, `resolve-initial-status` và output ID/mã trạng thái cần cho đối chiếu. Không chọn target bằng email; không `--password`, `--enable`, `--role`, `--force` hay `--confirm-db-is-current` trong các lệnh mutation.
- `inspect` đọc bằng đúng operator boundary, không lộ verifier/token/email/nội dung hồ sơ. Trình bày riêng credential usable/quarantined, status proven/held, latest head và eligibility; không gọi một account usable khi chỉ credential đã được sửa. User absent phải từ chối mutation, không tạo account thay thế hoặc gắn sang User khác cùng email.
- Kiểm authority/case/installation/gate và chụp scalar snapshot ngắn; release DB sạch trước nhập/verify/hash. Không giữ connection/khóa qua TTY, hash, đọc hồ sơ hoặc I/O ngoài DB.
- Sau hash, khóa User theo UUID → token theo UUID → session theo UUID → flush receipt/audit/FK → journal; dùng helper/routine phù hợp. Recheck login operator, case expiry bằng clock mới, role, verifier/generation/heads và hold trước mutation, sau append/fsync và trước commit. Không lấy khóa User sau journal. Race đổi verifier/head/status/case/authority phải dừng, không hash/mutation retry tự động.
- Credential mới dùng generation/operation mới từ toàn chain, reason `verified_recovery`, verifier DB-only và receipt cùng transaction. Đóng mọi reset token/session cũ cùng batch bằng reason phù hợp; không làm token/session cũ sống lại. Nếu cần thêm reason/field/kind thì parser Python, SQL, migration và replay kiểm cùng hợp đồng, đọc được lịch sử cũ.
- Status dùng `verified_initial_status`, receipt và decision mới đúng case; không suy quyền từ credential receipt, snapshot active, journal intent hoặc receipt của User/domain khác. Nó không thay credential hoặc phát session.
- Audit/case binding chỉ giữ ID, code, time và operation cần kiểm, không secret hay text chứng cứ. Không đưa verifier vào journal, object storage, case file, exception hoặc output.

### 3. Journal, outcome và replay

- Giữ một batch auth → invitation/reset/session closure theo hợp đồng hiện có; mirror toàn batch `false` và force binding trước byte đầu; append/fsync thành công mới confirm toàn batch. Không commit riêng từng event.
- Phân biệt known precommit rollback, known commit, uncertain commit và lỗi release. Outcome commit không bị rollback/close ghi đè. Uncertain hoặc postcommit proof/release lỗi trả lỗi rõ, không báo credential/status thành công, không tự gửi lại; luôn trả connection an toàn. Operation ID không phải bằng chứng commit đã thành công.
- Chỉ báo thành công sau known commit, fsync và **proof mới trong transaction mới**, kiểm lại authority/case binding/receipt/latest heads/caller time tương ứng. Sau commit, action đã consumed vẫn có thể chứng minh đúng outcome qua receipt; không chạy lại mutation để tạo proof.
- Replay chỉ hạn chế: thiếu receipt thì quarantine/held, không dựng verifier/User/receipt/active từ journal. Restore snapshot trước C3 + journal mới nhất không nhận credential cũ; operator phải lập quyết định mới nếu receipt mới đã mất. Giữ bảo vệ review/session/reset/invitation/common revocation và hold tương thích snapshot cũ.
- CLI trong maintenance không mở readiness hoặc bypass integrity bằng GUC/environment/boolean. `recover` vẫn báo reconciliation và safety pending riêng. Runbook chỉ mô tả lệnh thực sự chạy, phân biệt tin cậy người vận hành với guard phần mềm.

## Kiểm chứng bắt buộc

Dùng Conda `ras`, PostgreSQL/journal/roles và nếu cần MinIO/Redis/Compose **cô lập**, secrets tổng hợp ngoài repo. Không fallback `.env`; chặn network ngoài loopback. Không thao tác stack dev, gọi provider/arXiv, đổi pricing hoặc mở safety.

1. **Authority/DB:** login operator thật hợp lệ và runtime/recovery/owner không được dùng nhầm CLI; membership/SET ROLE giả operator, tự khai UUID, thiếu grant, probe/release lỗi đều đóng. Test SQL trực tiếp binding/null/cross-user/cross-domain/action/head và raw mutation; ca hợp lệ phải đi tới commit thật. Mô tả đúng trust boundary của người provision, không tuyên bố chống được owner độc hại.
2. **T07–T10:** User/Admin quarantine active/proven phục hồi credential; held giữ held tới case status riêng; disabled không enable; mọi Admin quarantine; Admin duy nhất legacy active/held; Admin duy nhất disabled; User absent/cùng email UUID khác; bootstrap singleton lệch; case hết hạn/sai/đã dùng; mật khẩu trùng snapshot bị từ chối. Kiểm login/session predicate thật sau outcome hợp lệ và từ chối credential/cookie/token cũ.
3. **Race/clock/release:** expiry trong hash, chờ khóa hoặc fsync; head/role/status/authority đổi giữa proof và commit; hai operator cùng target/case; lỗi rollback/close/invalidate ở từng pha. Không còn pool leak, không giữ DB qua TTY/hash; final proof không dùng `now` lúc bắt đầu.
4. **T15/journal/restore:** none/torn/prefix/full-write-fsync-error × rollback/commit/uncertain; confirm-only/binding trước append; replay file ahead và record đã present; hai vòng dump/restore/recover với journal giữ ngoài backup. Chứng minh không tái dùng case/receipt cũ, không dựng active/verifier/receipt và hồi quy các domain cũ.
5. **CLI thật:** chạy inspect và mutation trên login operator đã provision; ConPTY/TTY không echo, nhập hai lần, sai policy, EOF/Ctrl-C, không TTY. Capture/log scan không chứa password/verifier/token. Không tính test helper getpass giả là kiểm TTY thật.
6. **Pending HTTP thật riêng:** `/healthz` sống; `/readyz` và route được bảo vệ vẫn 503 qua API/proxy; không có bypass sản phẩm. Test nghiệp vụ nếu cần chỉ patch kết quả cutover trong fixture theo hợp đồng C1–C3; không patch authority/case/receipt/session/integrity/hold. Ghi đúng phân biệt này.
7. Chạy hồi quy auth/receipt/provisioning/rehash/migration/ORM và các journal liên quan; web checks nếu sửa web. Countercheck vài guard quan trọng bằng assertion nghiệp vụ, khôi phục bytes/hash trước suite cuối. Không chạy mutant khi suite đang chạy.

Không yêu cầu chạy toàn bộ inventory mỗi PR. Thu thập ổn định node IDs, selection/JUnit unique, source hash trước/sau; ghi fail/error/skip/not-run và debt cụ thể, giữ các lượt lỗi chuẩn bị. Không coi baseline fail là đã được miễn nghiệm thu; gói B/C4 nhận toàn inventory và debt còn lại. Không xây thêm bộ seal/hạ tầng test nếu công cụ hiện có dùng được.

## Hoàn thành và bàn giao

- Một luồng C3 dùng được bằng role thật, hồ sơ tổng hợp hợp lệ, nhập an toàn, journal/receipt/outcome đầy đủ; case status riêng không mở disabled; restore không cấp lại credential/quyền đã mất. Giữ safety pending.
- Handoff/inventory tập trung ghi schema/routine/grants mới, trust boundary/hồ sơ, lệnh đã chạy, source/selection/evidence, lỗi/giới hạn, operator provisioning và runbook maintenance. Cập nhật ngắn kế hoạch MVP, decisions, auth restore, mục lục và AGENTS; không nhân bản toàn lịch sử kiểm.
- Chốt bản đồ C4: writer/consumer đã chuyển, debt theo node không đếm trùng, yêu cầu image/roles/journal/storage, trình tự hold → migrate → replay/backfill → operator → hậu kiểm → ready. Đây là bàn giao, chưa triển khai ready hoặc cutover dev.
- Commit/push và mở **PR tiếng Việt**, không merge. Không thêm AI vào tác giả/committer/trailer hoặc dòng quảng bá. Báo source/head, file thay đổi, kiểm đã chạy và chưa chạy, cleanup tài nguyên thử và mọi drift có chứng cứ. Giữ docs review/task có sẵn trong PR.

Giới hạn giữ nguyên: case/inventory do người có thẩm quyền xác minh ngoài app; journal phải độc lập, hiện hành. DB và journal cùng mất/rollback có thể không phát hiện; fault injection/fsync không chứng minh chịu mất điện. C4 và các gói MVP C–E chưa được nghiệm thu bằng C3.
