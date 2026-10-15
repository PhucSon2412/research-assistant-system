# B1 — provenance initial receipt và hồi quy trước C4

**Sau merge, 2026-10-15:** PR #60 đã merge `44633b99`, tree khớp head review `c6f200e`; source `0050`, chưa rerun tại merge. [Review độc lập](reviews/pr60-pre-c4-auth-receipts-regression.md) giữ kết quả/giới hạn trước merge. Đã giao [C4 tích hợp](tasks/m6c2b2c4-integrated-cutover.md), mới giao/chưa triển khai hoặc kiểm; mã hiện vẫn pending. Các kết quả B1 dưới đây giữ đúng source/lượt chạy, không là readiness C4 hoặc trạng thái DB dev.

[Task](tasks/pre-c4-auth-receipts-and-regression.md) · [Inventory](pre-c4-test-inventory.json) · [Kế hoạch MVP](mvp-delivery-plan.md)

**Đã triển khai và kiểm ngày 2026-10-15 trên nhánh `fix/pre-c4-auth-receipts-regression`, base merge PR #59 `041f77ef1c12b6277e7a5f46aaf32155b3673063`. Safety pending tới C4.** Docs orchestrator/reviewer có sẵn được giữ trong commit `1e7a0c0`. Chỉ Conda ras, PostgreSQL/MinIO/Redis/journal/hồ sơ tổng hợp trên project cô lập; không đọc `.env`, thao tác stack dev, provider thật hoặc pricing.

## Writer và bằng chứng bền

Revision `0050` sau `0049`, giữ nguyên các revision đã merge. Receipt `bootstrap`/`invitation_accept` của **cả credential và status** phải có parent tạo account mới trong transaction đang chạy. Trigger BEFORE kiểm parent được routine bảo vệ tạo, đúng User/role/thời điểm/top transaction và transaction tạo tuple còn live. Invitation prepare có subtransaction của khối `EXCEPTION`; kiểm transaction live nhận cả nhánh đó, xử lý epoch của xid 32 bit. Kiểm này chỉ dùng cho INSERT mới, không dùng `xmin` lịch sử sau restore.

Constraint receipt deferred kiểm chiều ngược bằng `ras_auth_initial_pair`: registration/genesis đúng installation; User/creation time; parent bootstrap hoặc accepted invitation; hai receipt đúng domain/decision/reason/time/transaction; operation/generation khớp toàn bộ typed event; credential → status liên tiếp và là auth event đầu của User; invitation thêm closure kế tiếp; audit đúng hành động/target/time. `record_auth_endings` vẫn ép toàn bộ constraints trước append. Thiếu một receipt, parent, closure hoặc binding không thể commit hoặc append thành quyết định mới hợp lệ. Không có primitive tạo Admin thứ hai, rescue hoặc raw auth grant mới.

Provenance bền dùng transaction marker được lưu trong parent và receipt, các operation/generation và nội dung mirror, không đối chiếu xmin hiện tại với transaction lịch sử. Upgrade không sửa User/receipt/journal, không I/O journal và không backfill chứng cứ.

Role/email phải đúng **tại lúc tạo account/receipt**, kiểm trong trigger parent live và routine tạo được bảo vệ. Predicate bền không so role/email hiện tại với profile lúc tạo: singleton Admin và accepted User ID là evidence bất biến của luồng đã chạy, còn mọi consumer cấp quyền phải reload role hiện tại. Lượt toàn inventory đầu tại `1bdd06e` đã phát hiện bảy regression do B1 ban đầu đặt kiểm role/email vào predicate bền (role denial thành session-expired; profile reload mất phiên). Lượt đó được dừng, giữ JUnit/source trước-sau và không coi là lượt cuối. B1 sửa phân biệt thời điểm này, thêm hai ca bootstrap/invitation đổi profile mà giữ nguyên pointers/receipt, đồng thời vẫn từ chối generic/reuse parent cũ.

## Consumer và dữ liệu cũ

`ras_auth_receipt_provenance` và `ras_auth_initial_status_verified` là predicate chung cho account/session/login, status/reset, enable và operator inspect/resolve. Cặp khởi tạo phải được confirm đầy đủ; `admin_enable` không thay bằng chứng initial. Initial status và credential `verified_*` cần case C3 đã dùng live, đúng operation/User/installation/action/thời điểm; thời hạn/thu hồi operator **hiện tại** không hủy quyết định đã commit hợp lệ trước đó. CAS rehash đã gọi account predicate từ `0035`, tiếp tục giữ generation.

| Consumer | Đường kiểm provenance ở `0050` |
|---|---|
| Login, session create/live/touch, CAS rehash | `ras_auth_account_live` kiểm cả credential và status pointer qua `ras_auth_receipt_provenance`; giữ binding/head/session/TTL cũ |
| Reset thường | `ras_auth_reset_eligible` → status live + account live; token live tiếp tục dùng eligibility và fence cũ |
| Reset account quarantine | Status phải live có provenance; nhánh quarantine và credential fence giữ hợp đồng C2b, reset không tự enable |
| Admin enable | `_initial_status_verified` → `ras_auth_initial_status_verified`; `admin_enable` mới vẫn cần lịch sử initial thật khi consumer đọc |
| Operator inspect/resolve | Credential proof qua predicate chung; initial-status boolean qua `ras_auth_initial_status_verified`, không còn reason allowlist riêng |
| Recover | `ras_auth_restrict` chỉ quarantine/hold; `auth_safety.verify` kiểm không còn pointer effective mang provenance sai |

Receipt cũ không chứng minh được vẫn append-only, không xóa/sửa hoặc nâng thành hợp lệ. Predicate từ chối ngay sau migrate; `recover` đưa credential về quarantined và active status về held. Disabled vẫn là hạn chế. Sau hold → migrate → recover đối chiếu sạch, lập case C3 mới cho từng quyết định cần phục hồi; credential và initial status độc lập, không mở business/latest disabled. Replay không tạo User, verifier hoặc receipt. Cùng rollback DB và journal vẫn có thể không phát hiện.

## Fixture và đối chiếu hồi quy

- Runtime generic cho User cũ và consumer enable: trước sửa 6 assertion fail tại base `0049`, không setup error; log/XML ngoài repo.
- Nhóm provenance/upgrade/hai vòng dump-restore, ba luồng disable/enable với initial proof thật, C3 credential: lượt `prep-provenance-4` 13 pass. Đây là kiểm chuẩn bị, không là kết quả toàn suite cuối.
- Fixture account hợp lệ dùng bootstrap/invitation thật hoặc credential + case initial status C3 thật. Fixture legacy/corruption giữ quyền migrator riêng, không biến thành account đã xác minh bằng reason.
- Snapshot trước `0039` chỉ chạy `hold` bằng binary hiện hành trước migrate; business recovery hiện hành chạy **sau** migrate. So sánh dữ liệu lịch sử phải kiểm các cột mới là NULL, không giả schema head làm snapshot cũ.
- Restore invitation lấy container/project/port từ context được kiểm labels, không pin `58442` hoặc label của lượt cũ.

Lượt chuẩn bị toàn API đầu: 139 pass, 16 fail, 14 setup error; dừng tại ngưỡng 30, các node chưa tới không phải lỗi. Lượt core tiếp theo và rà fixture từng file giữ log/XML riêng. Các lượt chuẩn bị được giữ riêng trong inventory; không cộng pass của chúng vào số node cuối.

Một lỗi provisioning thật được tái hiện riêng: `provision` lần hai thu hồi EXECUTE khỏi chính NOLOGIN definer; `ALTER OWNER` về cùng role không tự cấp lại quyền đã thu hồi. Reset của account hợp lệ lỗi `permission denied` ở `ras_auth_reset_eligible`. B1 cấp lại EXECUTE cho definer trên đúng tập routine đã chọn; `0050` sửa ACL tương ứng cho installation đã provision. Không cấp quyền ghi auth thô cho runtime. Regression kiểm reprovision hai lần → request/confirm reset → login và kiểm ACL âm.

Các fixture account C1/C2/C3 có legacy PHC để bắt đầu nay lấy **cả credential mới và initial status qua hai case C3 thật**, mỗi case có hold/registration/operator/receipt riêng. Password cũ khác password đã xác minh; không dùng reason generic làm chứng cứ cho account positive. Các ca legacy/quarantine/held giữ setup và kỳ vọng riêng. Vì baseline đã có quyết định hợp lệ, kiểm số journal/audit dùng mốc trước thao tác hoặc operation đang kiểm; `present` khi recover kiểm lại mirror sẵn có là bookkeeping, các quyết định replay và hậu điều kiện vẫn kiểm đầy đủ.

Fault test lịch sử được đối chiếu theo hợp đồng runtime: commit uncertain có auth change không tự replay rồi báo thành công. Test kiểm không delivery/cookie/thành công, gate đóng khi DB sau journal, explicit recover chỉ hạn chế, rồi kiểm token/session/quyền. Các luồng known commit và nghiệp vụ bình thường vẫn phải thành công. Test batch lấy actor/session/CSRF thật, khóa User trước email/invitation rồi journal; SQL tạo corruption chỉ dùng kết nối setup privileged được ghi rõ. Outbox cache được xóa giữa các DB thử, tránh link của test trước lẫn vào assertion.

Debt `test_worker_advance_and_intent_expiry_before_response_keep_receipt` fail tại `c203b38`: callback cũ tự đặt `indexed` trong checkpoint validate, trái hợp đồng extraction `metadata_only` của C2d-5d2a. Kiểm chuẩn bị ngoài repo đã đi qua worker extraction → document index runtime với transport/provider giả, có index entries thật, rồi hết hạn intent trước response: receipt vẫn `201 received`, job đã `ready`, item `indexed`. Fixture repo đã chuyển sang chính luồng đó tại `143347f`; lượt kiểm có mục tiêu đạt. Kết quả cuối lấy từ inventory, không cộng ca chuẩn bị vào pass.

Debt `test_audit_resource_locks_queue_writer_response_proof_sees_winner[common]` dùng observer login owner dù revoke đã chuyển runtime. Downloader và revoker còn dùng cùng Admin, khiến writer có thể chờ User trước item. Kiểm chuẩn bị riêng đã dùng User downloader được mời thật, giữ Admin revoker riêng, rồi quan sát login runtime **và SQL của item** đang chờ khóa. Writer thắng trước response proof, response vẫn phải `404`; cả common và review variant đạt trong kiểm chuẩn bị. Fixture repo đã sửa tại `143347f`; kết quả chuẩn bị không cộng thêm vào API pass.

Các fixture còn lại tại `143347f` giữ kỳ vọng nghiệp vụ: thiếu Origin được gửi thực sự thiếu header rồi khôi phục default trong `finally`; invitation dùng baseline trước thao tác; reset kiểm mọi session của đúng User, không đếm session Admin dựng fixture. Hai job có cùng fake clock được nhận diện bằng UUID trả về khi kiểm suffix `:2`, vẫn giữ số lần tái dùng extraction/embedding. Collection bỏ membership qua API runtime tại checkpoint reservation sau commit/release, vẫn phải `409 search_scope_changed` và không thêm payload provider. Schema corruption dùng `setup_engine` có nhãn, các User đó không đăng nhập.

Runner đánh giá tạo Admin bằng bootstrap và User bằng invitation accept với clock tường minh; synthetic common metadata dùng kết nối setup riêng. Ma trận DOI/hash A/B/Admin, canary và citation/payload trái quyền giữ nguyên. Full CLI evaluation có environment owner/đọc `.env` lịch sử chưa được chạy trong B1; C4 phải thay wiring bằng context cô lập, roles đã provision và cutover thực trước khi dùng lệnh đó. B1 không đọc `.env` qua environment này.

## Kết quả cuối theo identity

| Phạm vi | Pass | Fail | Error | Skip | Not-run | Deferred C4 |
|---|---:|---:|---:|---:|---:|---:|
| API, 5,137 node | 5,136 | 0 | 0 | 0 | 0 | 1 |
| Web unit | 70 | 0 | 0 | 0 | 0 | 0 |

Web typecheck/build đạt. Kết quả API nối `final-all-3`, `final-continue-15` và ca đối chứng riêng `final-discovery-15` trên cùng 417 source hashes; mỗi node có đúng một trạng thái cuối. Không tuyên bố full suite pass: readiness 200 còn deferred và giữ nguyên expectation. Năm full restore drill và CLI evaluation lịch sử chưa chạy trong B1, ghi riêng ngoài mẫu số pytest.

Debt lịch sử là union **298 identity**, không cộng các số cũ. Bảng từng node gồm nguồn, nguyên nhân, thay đổi và trạng thái cuối nằm tại `historical_debt.nodes` trong [inventory](pre-c4-test-inventory.json). `current_observed_debt.nodes` giữ riêng **184 node thực có failure/error/skip** trong các lượt đã chạy; lượt lỗi rồi pass vẫn giữ observation cũ. Các node chưa từng chạy không bị gọi là lỗi.

| Nhóm debt lịch sử | Node | Pass cuối | Deferred C4 |
|---|---:|---:|---:|
| C4 unpatched readiness | 1 | 0 | 1 |
| corrupt version/file negative fixture versus legitimate revoke | 1 | 1 | 0 |
| delimiter password / DB identity | 1 | 1 | 0 |
| historical snapshot/current recovery | 5 | 5 | 0 |
| initial proof / runtime fixture | 2 | 2 | 0 |
| legacy journal/recovery fixture | 284 | 284 | 0 |
| observer login and actor lock scope | 1 | 1 | 0 |
| obsolete synthetic worker progression | 1 | 1 | 0 |
| privileged corruption setup | 2 | 2 | 0 |

Các ca browser/proxy/process/restore đã pass ở B1 vẫn có giới hạn cutover seam; danh sách ID để chạy lại không seam nằm ở `C4.reference_nodes_to_reexercise_without_cutover_seam`. Đây không phải thêm lỗi hoặc thêm deferred vào mẫu số API.

Ca `test_discovery.py::test_persistent_oai_deleted_record_is_hidden_and_checkpointed` từng fail trong `final-continue-15`: runtime trả 0 record, inline assertion không giữ outcome flags nên chưa xác lập nguyên nhân. Chạy lại riêng cùng source/image và giữ expectation đạt; observer chỉ đọc ghi hai lượt checkpoint đều 1 record, không thêm retry/fallback. JUnit/log lỗi ban đầu, kết quả chạy lại và observer được giữ riêng trong inventory; không quy lỗi này cho Windows hoặc dùng nhãn baseline để miễn.

## Source, kết quả và lệnh kiểm

Base `041f77ef1c12b6277e7a5f46aaf32155b3673063`; docs orchestrator/reviewer được giữ trong commit `1e7a0c0`; mã kiểm cuối `143347fc0f1f9e3ce25c2450da9e56dda5eadf67`. Revision `0049 → 0050`; mọi migration đã merge giữ nguyên. Inventory tập trung collection, mỗi node một trạng thái cuối, debt lịch sử theo union identity, các lượt lỗi/gián đoạn và hash JUnit/log/source. Các mốc PR cũ không được cộng thành lỗi hoặc pass mới. Collection base `041f77ef` được collect-only bằng snapshot `git archive` ngoài repo (chỉ api/scripts, xác minh không có đường dẫn `.env`): 5.118 node, tất cả còn nguyên identity ở source cuối. Source cuối có 5.137 node, thêm đúng 19 node B1, không xóa/đổi tên node cũ. Đây chỉ là đối chiếu collection, không là tuyên bố suite base pass.

Evidence ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-b1-20261013`. Conda ras interpreter/pip đã kiểm. Project thử `rasc3c87b976d`; PostgreSQL `35778`, MinIO `39180`, Redis `34889`, đều riêng và kiểm labels/ports. HTTP ngoài loopback/container thử bị chặn. Image thử `rasb1-api:20261013` có 201 product hashes host=image và Alembic `0050 (head)`; ID/digest trong inventory. Build context là whitelist app/migrations/metadata, không gồm `.env` hoặc credentials.

Các lệnh dưới đã thực gọi, artifacts riêng theo tên lượt; chúng là lệnh kiểm evidence, không là lệnh vận hành dev:

```powershell
$py = 'C:\Users\ADMIN\.conda\envs\ras\python.exe'
$evidence = 'C:\Users\ADMIN\AppData\Local\Temp\ras-b1-20261013'
$env:RAS_TEST_PROCESS_IMAGE = 'rasb1-api:20261013'
$env:NEXT_TELEMETRY_DISABLED = '1'
& $py "$evidence\run_inventory.py" prep-failures-4 --failed-from final-all-2 --defer-ready --jobs 1
& $py "$evidence\run_inventory.py" prep-common-search-4 --files test_common_search --jobs 1
& $py "$evidence\run_inventory.py" final-all-3 --defer-ready --jobs 3
& $py "$evidence\run_inventory.py" final-continue-15 --failed-from final-all-3 --defer-ready --jobs 3
& $py "$evidence\run_inventory.py" final-discovery-15 --failed-from final-continue-15 --files test_discovery --defer-ready --jobs 1 --diagnostic-discovery
```

Orchestration ngoài repo chỉ ghép `scripts/c3_test_host.py` và utility collect/hash có sẵn `c2d7_seal`; selection qua pytest `@args`. C3 và process/proxy/browser smoke chạy tuần tự để tránh đổi membership role cluster chéo batch. Readiness 200 được deselect đúng node, không thay expectation. Lượt `final-all-2` truyền sai prefix deselect nên đã chạy node này và fail 503; lỗi orchestration được giữ riêng, đã sửa selection cho lượt cuối.

Lượt `final-all-3` tại `143347f` bị gián đoạn trước khi hoàn tất: 28 nhóm JUnit, 2.810 pass; ba nhóm đang chạy không có JUnit hoàn chỉnh và các nhóm sau chưa bắt đầu, tổng 2.326 not-run, thêm một readiness deferred. Khi tiếp tục ngày 2026-10-15, không còn Python test đang chạy; source-after được chụp và khớp source-before. Summary gốc giữ riêng cùng hash; metadata phục hồi không suy pass từ dấu chấm và không tự gán exit code cho process thiếu kết quả. `final-continue-15` chỉ chọn 2.326 node thiếu kết quả trên cùng source/image; các node đã pass được giữ, không chạy lại. Các trạng thái cuối được nối theo identity trong inventory.

Lượt `final-all-2` tại `c203b38`: 5.102 pass, 23 fail, không setup error/skip; 12 node common search không có JUnit vì process exit `4294967295`, không được tính pass từ dấu chấm log. Lượt riêng `prep-common-search-4` giữ diagnostic 300 giây và source hash; process được theo dõi cùng PostgreSQL thử để phân biệt còn tiến triển với chờ khóa. Lượt cuối tắt timed traceback dump trên Windows; các timeout barrier/lock/subprocess trong test vẫn giữ. Không khẳng định nguyên nhân native crash chỉ từ traceback. Lỗi kết nối Windows `10048` được giữ và chạy lại cùng source; không thêm retry sản phẩm hoặc fallback owner.

Đối chứng DB-only trước suite: bỏ hai guard writer mới làm 4 assertion nghiệp vụ fail; cho phép durable pair giả làm 1 assertion account-live fail; cả hai không setup error, source trước/sau khớp. Các mutant không chạy trong suite và không cộng vào pass API. Web không đổi: 70/70 pass, typecheck/build pass; browser/proxy/process ở cùng inventory cuối. Không tuyên bố full suite pass khi readiness vẫn deferred.

## Cleanup và drift quan sát

Đã dọn đúng project `rasc3c87b976d`: 3 container, 3 volume, 1 network và các image B1 được ghi nhận. Không prune. Chín container baseline giữ nguyên ID/image/mount; trạng thái/StartedAt có drift được ghi trong inventory, không quy nguyên nhân hoặc sửa dev. Baseline có 155 volume và 127 image; số baseline thiếu ở lần đối chiếu cuối lần lượt 0/0. Không có inventory network đầy đủ lúc đầu, nên kiểm network theo label project riêng.

Đã kiểm diff/untracked và chứng cứ với bảy credentials tổng hợp của context, không có giá trị đó ngoài cấu hình riêng. Sau khi dọn Docker, đã xóa `credentials.json`, `compose.json`, `empty.env` tổng hợp ngoài repo; giữ JUnit/log/hash/source archive và journal/case tổng hợp làm chứng cứ. `secret_cleanup` trong inventory ghi phạm vi kiểm, không chứa credentials. Không đọc `.env` dự án hoặc secret thật.

## Gói tích hợp C4

Một PR C4 cần triển khai bản cài/chạy đầy đủ, không chia theo endpoint:

1. **Image/revision/roles:** build cùng source `0050` cho migrate/API/worker/beat/CLI; migrator cho DDL/provision/hold/recover, runtime thật cho mọi business writer, recovery chỉ các routine hạn chế và operator login riêng qua case. Không owner fallback khi runtime probe lỗi. Kiểm ACL/owner/fixed search_path sau upgrade và reprovision.
2. **Cutover:** `auth_safety.cutover` vẫn trả pending. C4 thiết kế hậu điều kiện phiên bản/progress và đồng bộ writer/consumer trước ready; bỏ toàn bộ patch pending trong luồng nghiệm thu HTTP. Không chỉ đổi hằng pending thành ready hoặc dùng toàn 503 làm thành công.
3. **Cài mới:** resources/project/credentials/journal mới cô lập → migrate/provision → genesis `new_install` → bootstrap runtime một lần → invite/accept/login/reset/logout → hậu kiểm readiness. **Upgrade:** writers dừng → hold trên schema cũ → migrate → provision đúng roles → recover/kiểm quyền; account held/quarantine phục hồi qua case C3 sau `recover --hold-for-operator`, rồi recover đóng. Không gán lại initial provenance bằng timestamp/audit đơn lẻ.
4. **Restore hai vòng:** dump PostgreSQL/object thật, journal hiện hành nằm ngoài dump; hold trước migrate, đối chiếu/recover lặp, operator g3, restore lại snapshot g1 với journal g3 rồi case mới g4. Account không rollback vẫn dùng được; mất receipt không dựng lại; bad provenance không cấp quyền; case đã dùng không tái dùng. Giữ common/review/session/reset/invitation và delete fences. Chỉ một DB writer cho mỗi journal trong từng bước.
5. **HTTP/proxy/browser:** giữ expectation nghiệp vụ A/B/Admin; private upload có consent, index keyword/vector, original checksum, library/chunk/collection, search keyword/hybrid, RAG ba scope với provider giả, citation mở đúng version/file/locator, approve/publish/revoke và session/CSRF/Origin. Trước recover 503 thật; sau ready có cả được phép và từ chối đúng quyền, không còn gate 503 che lỗi. Chạy worker/beat/Redis/MinIO và crash/resume thật ở image mới.
6. **Runner/seam:** fixture B1 chỉ thay `auth_safety.cutover` trong test scope; SQL role, receipts, journal integrity/hold, ACL và outcomes vẫn thật. Pending HTTP riêng không patch. Inventory liệt kê từng node phải chạy lại không seam, gồm readiness 200 và full Compose/drill. Inventory còn liệt kê chính xác các node browser/proxy/process, upgrade/restore và operator restore làm coverage để C4 chạy lại hành vi không cutover seam; pass B1 của chúng vẫn là kết quả tầng dưới, không tự trở thành E2E C4. B1 thay account seed owner/raw trong năm drill bằng bootstrap/invitation qua runtime riêng; recovery CLI truyền identity migrator tường minh bằng env, không đưa mật khẩu vào argv. Đây chưa là bằng chứng chạy full drill.

### Phụ thuộc chạy drill đầy đủ

`scripts/drill_backup_restore.py`, `drill_session_revocation_restore.py`, `drill_reset_token_restore.py`, `drill_review_closure_restore.py` và `drill_invitation_restore.py` phải chạy không patch trong C4. Helper `drill_accounts.seed_accounts` đã được kiểm bằng login runtime thật; owner bị từ chối trước tạo User/receipt/journal. `RAS_DRILL_RUNTIME_USER`/`RAS_DRILL_RUNTIME_PASSWORD` bắt buộc, khác migrator, kiểm trước mutation. Các drill dùng chung còn yêu cầu toàn bộ biến synthetic POSTGRES/MINIO và `RAS_COMPOSE_ENV_FILE` ngoài repo; không fallback đọc `.env`. Image head lấy từ Alembic, không pin `0032`.

Compose hiện vẫn cấp identity POSTGRES owner cho API/worker/beat, còn cutover production pending. C4 phải tạo LOGIN runtime/recovery riêng, NOLOGIN definer, chạy `app.cli.provision_auth_roles` bằng migrator trên từng DB cài mới/restore, rồi nối API/worker/beat tới runtime trước seed. API dùng để chạy one-off recovery vẫn phải được override về migrator; operator dùng login đã provision riêng. Provision không phải bootstrap và không mở ready. Không coi việc đổi helper seed là đã sửa xong topology Compose; năm full drill và readiness 200 được bàn giao như phụ thuộc tích hợp cụ thể, không như test B1 đã đạt.

Hồi quy dưới tầng đã kiểm PostgreSQL/role/journal thật, bootstrap/invitation/enable/reset/session/review/common và DB dump/restore. C4 còn ghép object artifact, image migrate/API/worker/beat, Redis, proxy và browser thành cùng một lần chạy không seam, giữ consent/ACL/checksum/citation và hậu điều kiện thu hồi. Không có blocker cần quyết định storage production để chạy gói cô lập này.

Môi trường thử có thể dùng journal tổng hợp ngoài dump và provider giả. Production vẫn cần storage journal độc lập, retention/độ bền và inventory/ủy quyền operator người thật; không cần giả các lựa chọn đó để chạy C4 cô lập. Fsync injection/SIGKILL không chứng minh mất điện, charge, SLO hoặc production durability. Không cutover dev trong B1/C4 cô lập.
