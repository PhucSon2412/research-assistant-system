# C2c-1: bootstrap runtime và initial receipt

[Mục lục](README.md) · [Task](tasks/m6c2b2c2c1-bootstrap-initial-receipts.md) · [C2b](c2b-reset-status.md) · [Inventory](c2c1-test-inventory.json)

**PR #36 đã merge tại `d1b53e653197af0d11f79cbdda0df514cb0af69a`; safety pending tới C4.** Base triển khai `1d3e55825901dc81feb892f27a436bbb57b511f2` là merge PR #35, chứa `f2ba336`. Head source `0036` → `0037` → `0038` (sửa P2 SQL installation); không suy revision DB dev. Task tiếp theo: [C2c-2 invitation lifecycle](tasks/m6c2b2c2c2-invitation-lifecycle.md). Ghi chú review độc lập chưa commit được giữ để đưa vào PR tiếp theo; không coi chúng đã nằm trong merge #36. Chỉ thử PostgreSQL pgvector 16/journal tổng hợp cô lập, Conda `ras`; không đọc/sửa `.env`, dùng dữ liệu hay tác động stack dev, gọi provider hoặc đổi pricing.

## Sửa P2 installation tại SQL creation

[Review độc lập tại `64cf342`](reviews/pr36-c2c1-bootstrap.md) chứng minh CLI guard chưa đủ: runtime gọi prepare trực tiếp trên installation `existing_db` rồi `commit_auth` vẫn tạo Admin usable/proven. Thêm `0038`, giữ nguyên `0037` vì không có chứng cứ chắc chắn revision đó chưa áp vào DB lâu dài. Không mở pending, không chuyển invitation.

Sau bootstrap advisory và trước mọi INSERT, SQL prepare buộc có registration `id=1`, mode `new_install`, journal UUID/genesis hash không NULL; mirror genesis `seq=1`, kind `genesis`, confirmed. Hash mirror phải khớp registration; envelope JSON có seq **number 1**, kind/genesis hash/prev 64 số 0/body journal UUID/mode khớp registration và `new_install`. Dùng `NOT EXISTS`, `IS NOT DISTINCT FROM` và kiểm non-NULL tường minh: missing/null/mismatch hoặc registration existing/adopted/rebuilt đều từ chối với mã lỗi SQL chung, không tạo User/singleton/receipt/audit hay append.

Đây là kiểm binding DB, không xác minh độ mới/canonical chain của file journal độc lập. Writer vẫn kiểm file ở entry và dưới journal lock cuối; integrity/hold/pending/batch/final checks không đổi. Routine không nhận mode từ caller.

`CREATE OR REPLACE` giữ owner NOLOGIN, fixed search path và EXECUTE ACL đã provision. Chỉ thêm **SELECT recovery_registrations cho definer** qua migration/provisioning; không thêm creation EXEC hoặc User/receipt writes cho recovery/runtime. Upgrade giữ nguyên rows/journal bytes/owner/ACL, không backfill account. Downgrade về `0037` chỉ pristine; có registration/auth/journal history thì chặn. Pristine downgrade phục hồi định nghĩa prepare của `0037`, giữ SELECT tối thiểu đã thêm; không thay creation grants.

Test mới gọi prepare **trực tiếp bằng runtime LOGIN**, không wrapper/history gate Python: existing registration/recover thật; fixture migrator riêng tạo missing/null/mismatch và các mode không hợp lệ; kiểm nguyên rows/mirrors/audit và journal bytes sau refusal. Đối chứng new-install trực tiếp tạo cặp receipt thật rồi login. Kiểm populated/provisioned `0037` cả cặp receipt thật và existing registration, quyền đọc definer thiếu trước upgrade, cùng pristine round-trip và history downgrade fence. Kết quả lượt P2 tách khỏi số `0037` ở phần lịch sử dưới.

**P2 tại code `d222fb1`: 626/626 API pass**, 0 failure/error/skip, 3 warning hiện có, **834,11 s**; **202** file API giữ SHA-256 trong suite cuối. Gồm C2c-1/bootstrap 125 + nền 477 + schema 24; collect **1402**, **776 not-run**. Bỏ riêng guard SQL mới → **1 expected failure / 0 errors**, khôi phục bytes trước suite cuối. Compileall/diff check pass; [inventory P2 theo source/node](c2c1-p2-test-inventory.json), [lệnh thực chạy](dev-setup.md#sửa-p2-installation-pr-36-cô-lập). Đã dọn project/volume/credentials thử sau kiểm ownership, giữ evidence ngoài repo. Không chạy lại web, ConPTY hay dump/restore drill trong P2; các số `0037` dưới đây là lịch sử. PR sau đó đã review lại và merge; [kết quả reviewer](reviews/pr36-c2c1-bootstrap.md) tách khỏi số của coding agent; safety pending tới C4.

## Boundary SQL và schema

`ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz) -> void` nhận `(user_id, normalized_email, display_name, PHC, policy_version, at)`. Chỉ mở bootstrap: UUID v4 mới, email chuẩn hóa hợp lệ, role cố định `admin`, PHC/profile hợp lệ theo policy DB; không nhận role/reason/verified flag, không cập nhật User cũ. Routine từ chối User/singleton/receipt/head hoặc lịch sử nghiệp vụ journal đã có, kể cả đối tượng absent. Không có primitive invitation/general registration trong PR này.

Routine từ chối nếu connection đã giữ shared hoặc exclusive journal lock, rồi lấy advisory bootstrap `0x524153424F4F5431`, kiểm lại một lần và INSERT User quarantine/held, singleton, audit system `admin.bootstrap/success` cùng transaction. Audit chỉ mang UUID và `{"via":"cli"}`; không email/PHC/password/name. PHC ở `users.password_verifier`, không ở journal/receipt/audit hay fingerprint.

`bootstrap_state.creation_transaction_id` nullable **chỉ để giữ legacy**. Upgrade không đổi row cũ, verifier, role, receipt/head, journal bytes hoặc nâng legacy thành usable/proven. INSERT mới buộc marker bằng `txid_current()`; UPDATE/DELETE singleton luôn bị từ chối, gồm đổi owner/time/marker, không thể xóa để bootstrap lại. Constraint deferred `trg_auth_bootstrap_pair` buộc:

- User đúng singleton, Admin active/usable/proven, thời điểm creation/password/update bằng `completed_at`.
- Hai receipt credential/status đúng User/domain/generation/decision/reason `bootstrap`/time; cùng transaction của singleton. Credential event đứng ngay trước status event; initial status là `active`.
- Audit success đúng UUID/time/allowlist. Existing C1 guards còn kiểm receipt/mirror/head và mutation binding, UUID/generation, PHC/policy.

Routine chuẩn bị cả User và singleton nguyên tử. Không commit account trung gian nếu thiếu một receipt, intent-only, sai binding/reason/time hay thiếu audit. `record_auth_endings` ép **ALL constraints IMMEDIATE trước byte đầu**, rồi trả về DEFERRED. Constraint pair kiểm lúc creation, không chặn disable/reset/rehash hợp lệ về sau.

| Role | Quyền C2c-1 |
| --- | --- |
| Runtime LOGIN | SELECT protected tables, EXECUTE prepare + routines C1/C2a/C2b; không raw User/singleton/receipt/head/mirror write, DDL/TEMP/membership/owner/BYPASSRLS |
| Recovery LOGIN | Không EXECUTE prepare, không INSERT User/singleton/receipt; giữ routines hạn chế/replay hiện hành |
| NOLOGIN definer | INSERT User/singleton/audit tối thiểu cho prepare; quyền auth hiện hành cho stage; không sở hữu table/schema hoặc DDL/membership |
| Migrator | Schema/provisioning tách biệt, không fallback từ CLI/runtime |

Prepare có fixed `search_path=pg_catalog,public,pg_temp`, revoke PUBLIC. Provisioning thêm singleton vào `PROTECTED`. Upgrade mang grants từ ACL session-create của runtime đã provision; không lấy ACL recovery-restrict để cấp creation. Downgrade chỉ pristine; có registration/User/singleton/auth/journal/progress thì từ chối. Mọi revision `0033`–`0036` giữ nguyên.

## Writer, khóa và commit

`bootstrap_admin(engine, *, email, password, policy, display_name=None, clock=utcnow) -> BootstrapResult` giữ interface cũ, thêm clock callable. Chỉ dùng engine runtime đã cấu hình. Entry kiểm integrity/pending thật, không init/register/recover journal. Đọc DB hash policy mới nhất và đóng transaction đọc trước Argon2. Không khóa/transaction lúc hash hay chờ input.

Thứ tự: **bootstrap advisory → kiểm lại gate/history → prepare User/singleton/audit/FK → journal exclusive cuối trong `commit_auth`**. Guard history cuối kiểm file độc lập dưới journal lock trước stage; không lấy thêm User/FK/audit row lock sau journal, không nâng shared thành exclusive. Hai Change cùng UUID, credential `bootstrap` rồi status `active/bootstrap`, một batch append/fsync. IDs operation/generation do stage cấp từ toàn chain và reserved batch, không từ snapshot.

Writer yêu cầu journal genesis `new_install`, không User/singleton và không record nghiệp vụ chứng minh installation đã có account; chỉ genesis/recovery bookkeeping được phép. **Bất kỳ complete bootstrap intent, kể cả credential prefix một mình, đều chặn retry** khi DB rollback/restore làm mất User. Không dựa riêng vào status event, mirror hoặc singleton. Missing/corrupt/ahead/unconfirmed/hold vẫn bị gate thật chặn. Existing/rebuilt genesis không là cài mới dù DB trống.

Mirror cả batch false + pair/binding/flush trước append. Fsync thành công mới confirm cả batch. User, verifier, hai receipt, singleton và success audit một transaction. Sau commit xác định, chọn operations theo **đúng UUID/domain** từ batch; transaction mới lấy bootstrap → User NO KEY UPDATE → journal shared, kiểm final integrity, account live, đúng operations/receipts/singleton/time/creation transaction. Không SELECT verifier/email trong hậu kiểm, tránh cả SQL DEBUG result logging. Không phát session/cookie/link.

Từ chối một lần là read-only: không còn transaction riêng ghi denial audit của bootstrap M1a. Success audit vẫn được DB buộc trong transaction/batch bootstrap. Đây là thay đổi hành vi được ghi rõ, tránh thêm commit độc lập vào writer mới; không diễn giải refusal là một bootstrap đã commit.

## CLI và cửa sổ lỗi

Giữ `python -m app.cli.bootstrap_admin --email …`, getpass hai lần và `--password-stdin` đọc dòng đầu. Không mật khẩu argv/env/default; thiếu TTY không có stdin mode thì từ chối. Chuyển `GetPassWarning` thành lỗi trước fallback echo. Argparse không abbreviation và không chuyển diagnostic có argument value ra stderr, kể cả `--password`, `--pass`, `--password=…`, `--password-stdin=…`. Stdout success chỉ UUID; repr result giấu email; exception chỉ type, không payload.

| Exit | Ý nghĩa |
| --- | --- |
| 0 | Known commit + batch confirmed + final gate/account/receipt/singleton đạt |
| 2 | Input/email/password/confirmation/TTY/arguments không hợp lệ |
| 3 | Account/singleton/history khiến bootstrap bị từ chối |
| 1 | Pending/integrity/DB/journal/failure hoặc outcome uncertain; **không kết luận account đã hoặc chưa được tạo** |

| Cửa sổ | Kết quả |
| --- | --- |
| Input/prepare/binding/gate lỗi trước append | Không success; rollback account/singleton/receipt/audit của transaction |
| Append none/torn/prefix/full nhưng fsync lỗi, DB known commit | Toàn batch mirror false; account/receipt có thể đã commit, gate đóng; `auth_journal_unavailable`, CLI 1 |
| Journal đã nhận intent, DB rollback | Không account/verifier/receipt/singleton; recover chỉ mirror/fold/restrict, không dựng lại chúng; bootstrap sau vẫn refuse nếu còn complete intent |
| Commit mất reply, thực sự landed hoặc rollback | Luôn `auth_outcome_uncertain`, CLI 1; không tự replay để suy bootstrap success |
| Journal/hold/head đổi sau known commit trước hậu kiểm | Không stdout success/exit 0; account có thể còn trong DB; cần inspect/recover theo hợp đồng thật |
| Không complete intent và DB rollback | Không hứa thành công; sau gate hợp lệ có thể thử lại với UUID mới; torn tail phải được recover xử lý trước |

Đánh đổi có chủ ý: complete intent + rollback mất Admin có thể làm installation chưa có account nhưng không bootstrap lại được. PR này **không** có force/recreate/enable/bypass hay operator rescue. C3–C4 phải có quy trình được duyệt riêng; mirror confirmed/reconciliation complete không chứng minh bootstrap thành công. Restore snapshot trước bootstrap với journal mới, recover hai lần vẫn không tạo User/verifier/receipt/singleton. Fsync/fault injection không chứng minh durability mất điện.

## Kiểm chứng và phần tiếp theo

Code được kiểm tại `51f5714`: **597/597 API pass**, 0 failure/error/skip, 3 warning hiện có, **764,18 s**; **199** file API giữ nguyên SHA-256 trong lượt cuối. Windows ConPTY **3/3**, dump/restore thật **1/1**; đối chứng **3/3** guard bị test phát hiện (4 expected failures). Collect toàn API **1373**, còn **776 chưa chạy** trên source này. Compileall và diff check pass; web không đổi/không chạy lại. Đã kiểm label rồi dọn container/network/volume thử, xóa credentials dùng một lần; evidence giữ ngoài repo.

Kết quả từng lượt, commands và node pass/fail/not-run tại [inventory](c2c1-test-inventory.json), [testing](testing.md#c2c-1-bootstrap-runtime) và [dev-setup](dev-setup.md#c2c-1-bootstrap-cô-lập). Writer fixture chỉ thay `auth_safety.cutover` ở từng test; integrity/hold/SQL/receipt/commit giữ thật. CLI stdin và Windows ConPTY thật chạy bằng runtime LOGIN; pending CLI/HTTP không patch là nhóm riêng. Đối chứng tuần tự history guard/pair constraint/singleton grants được khôi phục bytes trước suite cuối; suite giữ hash API source cố định và chặn external sync/async HTTP transport.

Đã kiểm cả dump/restore DB thật: snapshot trước bootstrap → runtime tạo Admin → restore snapshot vào DB mới với journal hiện hành → hold/recover hai lần bằng recovery LOGIN. Không User/verifier/receipt/singleton được dựng lại, recovery không có EXEC creation, report vẫn safety pending và retry runtime bị history guard từ chối. Chỉ một DB writer active vào file tại mỗi bước; không thay drill backup/object-store/proxy C4.

**C2c-2 chưa triển khai:** create/reinvite/inspect/accept/revoke invitation và delivery compensation vẫn caller cũ, bị pending đóng. Cần routine tạo User mới role **user**, chuẩn bị FK/audit trước journal, initial credential/status `invitation_accept` cùng một batch accepted closure; không dùng `admin_enable` làm initial proof. Gather actor/session/role/CSRF cuối đúng lock order, tránh User → email → User; hash/delivery ngoài khóa. Compensation giữ accepted khi accept thắng, không đảo closure/xóa account. Bootstrap prepare chỉ dành Admin đầu tiên, không tái dùng để nhận invitation.

C2d final resource callers/integration, C3 operator/approval/initial status/delivery, C4 full HTTP/proxy/cutover/restore drill còn mở. Không đổi readiness hoặc mở nghiệp vụ, không chạy web khi web không đổi, không đo provider/cost/performance hay production storage durability.
