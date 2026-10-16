# C4 — runtime tích hợp, readiness và restore

Base: PR #60, `44633b99f8407c5b15a8afb967afcd3c4d135018`. Revision source `0051`.
Nhánh `feat/c4-integrated-runtime-cutover`; chờ review, chưa merge. Đã kiểm trên môi trường cô lập;
không cutover dev hoặc chứng nhận production bằng kết quả này.

## Hợp đồng và topology

`auth_operational_state` là checkpoint sau reconciliation, không phải cờ bật service.
Recovery kiểm registration, chain/mirror, replay, backfill, projection, receipt binding và
provenance trước khi gọi `ras_auth_complete_contract`. Runtime không có quyền gọi routine
này hoặc ghi bảng checkpoint. Trigger chỉ tiến checkpoint **đã tồn tại** qua chuỗi record
confirmed liên tục. Nó không tạo checkpoint, không thay hậu điều kiện integrity và không
tiến foundation progress qua một `recovery_begin` chưa được recover kiểm lại.

Mỗi gate đọc journal hiện hành dưới journal advisory lock theo hợp đồng hiện có; final
boundary giữ khóa đến commit/rollback. Không cache ready lúc startup. Thiếu checkpoint,
schema/provisioning sai, journal thiếu/torn/unconfirmed, DB lệch journal, hold hoặc binding
sai đều đóng. `/healthz` là liveness; `/readyz` kiểm gate runtime. Account held/quarantined/
disabled vẫn bị chặn riêng dù service ready. Pricing là gate riêng, mặc định false.

API/worker/beat/business CLI dùng LOGIN runtime. Migrate/provision và init/recovery quản trị
dùng migrator; recovery LOGIN có các routine hạn chế; definer NOLOGIN; operator LOGIN và
case C3 riêng. Administrative inspection không kiểm identity runtime; đây không phải owner
fallback của HTTP/worker. Runtime service không nhận mật khẩu migrator/recovery/operator.
Profile runtime kiểm cả quyền replication hiện hành, không chỉ profile lúc provision.
Các bảng auth/control, gồm registration, checkpoint và mapping/case operator, được kiểm
cả ACL bảng lẫn ACL cột. Reprovision thu hồi ACL cột riêng; chỉ REVOKE ở cấp bảng chưa đủ.
Runtime chỉ SELECT registration; recovery có INSERT registration cho đường quản trị.
Definer không LOGIN/replication/membership hoặc sở hữu table/schema/DB phục vụ.
Tất cả Python service dùng cùng `RAS_API_IMAGE`, chỉ migrate định nghĩa build để tránh
export đồng thời nhiều build vào một tag. Chốt digest trước khi vận hành.

Compose chạy migrate → provision → initialize-installation → API/worker/beat. Init chỉ
đăng ký DB mới được xác minh rỗng, reconcile rồi cho runtime bootstrap một lần. DB đã đăng
ký chỉ được kiểm và báo maintenance; không tự recover/adopt/register-existing. DB cũ có
content/journal bị từ chối init. Không có account/password mặc định hay bootstrap rescue.

`provision_auth_roles --create-logins` lấy mật khẩu từ environment deployment, không argv.
Role mới có dấu sở hữu theo DB; role tồn tại không mang dấu đúng bị từ chối. Rerun không
xoay mật khẩu. Với role có sẵn do operator quản lý, dùng provision tường minh không có
`--create-logins`, kiểm profile/grant và mapping trước traffic.

## Runbook cutover có dữ liệu — chưa chạy trên dev

1. Chốt image digest/source và credentials riêng cho migrator/runtime/recovery/operator.
   Giữ pricing false. Chuẩn bị journal hiện hành ngoài DB/object backup và quyền mount.
   Kiểm installation ID, journal ID/head, volume/mount, checksum backup và một writer DB.
2. Dừng API/worker/beat và mọi business CLI; chặn traffic. Dùng binary đang phục vụ để
   `python -m app.cli.recovery hold` **trước migrate**. Kiểm hold với journal hiện hành.
   Không dùng snapshot journal cũ làm nguồn hiện hành, không tự register-existing.
3. Chạy image mới bằng kết nối migrator: `alembic upgrade head`, rồi
   `python -m app.cli.provision_auth_roles --runtime <runtime> --recovery <recovery> --definer <definer>`.
   Kiểm revision `0051`, LOGIN/NOLOGIN, quyền âm và digest mọi service/CLI.
4. Chạy `python -m app.cli.recovery recover --hold-for-operator`. Nếu Admin cần khôi phục,
   provision operator/case bằng migrator và thực hiện CLI C3 trong hold, đúng installation,
   User UUID, action và head. Credential và initial status là hai quyết định riêng; không
   mở latest disabled. Nhập credential qua TTY, không password argv/fixture trên dữ liệu thật.
5. Chạy `python -m app.cli.recovery recover` thường bằng kết nối quản trị đã provision;
   yêu cầu `reconciliation_complete=true`, `safety_ready=true`, gate open. Kiểm status,
   current journal và runtime `/readyz` 200; các account bị hạn chế vẫn không login được.
6. Khởi động cùng digest API/worker/beat; kiểm login/logout/reset, queue và readiness sau
   mutation. Mở traffic chỉ sau review vận hành. Nếu bất kỳ hậu điều kiện nào sai, giữ
   maintenance; không xóa checkpoint/hold, sửa journal hay dùng ENV/force để bỏ gate.

Lệnh trên là runbook review, **không phải lịch sử đã thực thi trên dev**. Compose env file
phải được cấp bởi deployment. Thử C4 dùng env file synthetic ngoài repo, không đọc `.env`.
Restore dùng `ras_backup.py restore`, hold trước migrate, reprovision sau `pg_restore
--no-owner --no-privileges`, recover với journal hiện hành, rồi mới start traffic. Không
đổi DB phục vụ khi writer cũ vẫn chạy; không có rollback nguyên tử DB/S3.

Với topology Compose trong PR, đặt tiền tố `docker compose --env-file <file deployment>
-p <project>` cho từng lệnh. Sau khi dừng writers, service `recovery-init` có mount journal,
storage và kết nối migrator nên chạy quản trị bằng `run --rm --no-deps recovery-init python
-m app.cli.recovery hold` (binary cũ trước migration), rồi `run --rm --no-deps migrate`,
`run --rm --no-deps provision`, `run --rm --no-deps recovery-init python -m app.cli.recovery
recover --hold-for-operator`, và cuối cùng `… recovery recover`/`… recovery status`.
Operator dùng LOGIN/environment riêng theo [runbook C3](c3-operator-auth-recovery.md), không
gắn secret đó vào API. Bootstrap cài mới dùng `exec api python -m app.cli.bootstrap_admin
--email <email>` với TTY an toàn; `exec api` và business CLI nhận runtime identity. Các lệnh
trong đoạn này là hướng dẫn vận hành cần review, chưa thực thi trên dev.
Service `provision` mặc định tạo LOGIN có dấu sở hữu. Với role đã có do deployment quản lý
nhưng chưa mang dấu này, operator phải đối chiếu ownership và override command bằng
`python -m app.cli.provision_auth_roles --runtime <runtime> --recovery <recovery> --definer
<definer>` không có `--create-logins`; không sửa password/comment của role lạ để vượt guard.

## Cách chạy kiểm cô lập

Python duy nhất: `C:\Users\ADMIN\.conda\envs\ras\python.exe`. Các script `c4_stack.py`,
`c4_http_flow.py`, `c4_browser.py`, `c4_full_drills.py` yêu cầu context ngoài repo, project
prefix C4, credentials synthetic. Network app internal; relay TCP chỉ expose các target
cố định trên loopback để Docker Desktop publish được port. App không có interface egress.
Không dùng relay như proxy HTTP tùy ý.

Provider fixture là transport test-only mount ngoài image, key sentinel, response identity/
dimension/usage theo adapter thật. Nó không patch auth/gate/ACL/outcome/accounting. Default
pricing false phải có zero paid call; paid fixture chỉ bật trong context synthetic. CLI
evaluation dùng context tường minh, DB/roles/bucket/journal mới; business qua runtime, local
embedder và generation capture, chặn HTTP ngoài context; nhãn `pending_human_review` giữ nguyên.

Shared fixture nghiệp vụ API/upload/resource và C2d dùng contract thật. Fixture lịch sử
C1–C3/pre-0051 có seam để tạo lịch sử hoặc isolate fault được giữ riêng, không tính là
nghiệm thu image C4. Ca pending mới xóa checkpoint bằng trusted setup, không trả ready giả.
Snapshot TEMPLATE giữ table ACL nhưng không giữ database ACL: phải reprovision clone.
Hồi quy API dùng PostgreSQL tmpfs riêng để giảm chi phí tạo/xóa DB; worker/CLI SIGKILL
chỉ giết process nghiệp vụ, DB vẫn chạy. Năm full restore drill dùng volume thật. Không
dùng tmpfs làm chứng cứ về durability hoặc mất điện của PostgreSQL.

## Chứng cứ và inventory

Kết quả cuối ở [inventory C4](c4-test-inventory.json). Chứng cứ thô nằm ngoài repo,
không chứa trong build context. Mỗi lượt fail/ngắt/rerun được giữ riêng, không cộng dồn
dấu chấm thành pass. Image/HTTP/browser/full drill và API regression là các lớp chứng cứ
riêng; countercheck chạy trên bản sao và khôi phục bytes trước suite cuối.

Image nghiệm thu được đóng băng ở
`sha256:cd745165f34a737b7e0fa74658bb0433363b2e12ffc484f1c294242f52e07de9`
(`rasc40b438343-api:local`); 202 file code/migration/cấu hình API trong image khớp bytes host.
API/worker/beat/migrate/provision/init cùng digest, user `ras` UID 10001; runtime
không nhận secret migrator/recovery, không có TEMP/DDL/raw auth/checkpoint/operator quyền thừa.
Web image `rasc43d894bce-web:local`. Cached build cũng có thể đổi manifest attestation;
không rebuild hoặc retag image trong lượt nghiệm thu.

HTTP/proxy mặc định **7/7** (gồm zero provider call), paid fixture **38/38**, Redis
outage/resume **3/3**. Chrome 155.0.8059.79 **7/7**, không request ngoài origin được cho phép.
Khi Redis dừng, API vẫn ready và nhận upload bền vào DB/object, job ở `received`; worker
tiếp tục extract/index sau khi broker phục hồi. Readiness không cam kết queue xử lý ngay.
Web **70 test pass**, typecheck/build exit 0, source trước/sau khớp. Native provisioning
CLI **5/5**: hold đóng, sửa ACL cột, idempotence giữ password, từ chối login lạ, recover mở
lại sau kiểm hậu điều kiện. Đây là business qua image thật; host suite lịch sử là lớp riêng.

Năm full drill đều exit 0: backup/restore **41 bước**, session revocation **35**, reset
token **54**, review closure **34**, invitation **59** (tổng 223 kiểm). Mỗi lượt có log/result/artifact checksum riêng;
DB và object thật được restore, đọc lại SHA-256, giữ journal hiện hành ngoài artifact.
Bốn drill đầu dùng bản sao harness ngoài repo; helper chỉ đổi deadline hàng đợi từ 180
lên 600 giây và ROOT trỏ cùng repo. Đã đưa deadline vào helper gốc và đối chiếu normalized
bytes; ba script phụ thuộc giữ nguyên bytes. Invitation chạy wrapper/script gốc.
Không đổi workload 20 job, TTL, gate hoặc assertion nghiệp vụ để xử lý timeout.

CLI `python -m app.evaluation validate` đạt **80 câu hỏi / 16 holdout / 15 paper**.
CLI `run --context <stack5/evaluation.json> --output <stack5/evaluation-output>` thực chạy:
**400 record audited**, 0 ACL violation, 261 viewer request/87 chunk với 0 violation,
0 gold label trong payload, 0 external call/blocked attempt; 80 nhãn vẫn
`pending_human_review`. Embedding local 239 call; generation chỉ capture, không chấm
semantic. PowerShell ghi warning native stderr làm wrapper báo exit 1 dù CLI đã ghi
report và thông báo 0 ACL violation; outcome CLI được đối chiếu với report và nhánh
return của CLI, không coi shell warning là ca ACL fail hoặc tự bỏ qua report lỗi.

Lệnh thực chạy dùng Conda ở trên: `scripts/c4_stack.py --context <stack5> setup|fake-off|fake-on|compose|evaluation`,
`scripts/c4_http_flow.py --context <stack5> default|paid|outage`,
`scripts/c4_browser.py --context <stack5>`, và
`scripts/c4_full_drills.py --context <stack5> --output <external> --image <frozen-tag> --web-image <web-tag>`
(invitation thêm `--only invitation_restore`). Suite API dùng runner hiện có qua
external `run_inventory.py`, từng file chọn bằng `@args`, sáu shard DB/journal riêng,
browser/proxy/process/C3 chạy tuần tự; lượt sửa/rerun giữ riêng.

Các lỗi chuẩn bị được giữ: reset request đến trước startup 0,834 giây; backup deadline
180 giây hết khi queue lớn (20/20 job sau đó tới awaiting_index, không job error, queue
tối đa đo 430,40 giây); staged invitation thiếu import/path trước nghiệp vụ; lỗi bytes
của countercheck trước lượt đối chứng đúng. Approval 503 ở một lượt chuẩn bị chưa rõ
nguyên nhân và không tái hiện trong stack nghiệm thu mới; không quy kết đã sửa nguyên nhân.
Discovery có observer chỉ đọc outcome/lease/gate, không retry hoặc sửa expectation.

Suite tổng đầu ở source C4 đóng băng có 5.163 node, trạng thái theo node là 5.134 pass,
27 failure và 2 error. Không miễn lỗi bằng nhãn baseline. Các sửa sau đó chỉ ở test/harness:
giữ runtime identity khi làm mới settings; provision snapshot đúng schema; import thiếu;
expectation hậu recover `open`; seed có cặp recovery thật; corruption receipt/head trả 503
và không settlement sau global gate đóng. Các kiểm clock/không commit/không touch session,
quyền âm, không gọi provider tiếp và không lộ nội dung vẫn được giữ.

Diagnostic Chrome lịch sử tìm được assertion của MockTransport đếm toàn pool, trong khi
UI có request khác đồng thời. Fixture mới theo dõi checkout/checkin và yêu cầu **thread
gọi provider giữ 0 connection**; request khác được ghi riêng, kiểm pool rỗng cuối attempt
vẫn giữ. Không sửa code ứng dụng, retry hoặc kéo dài session policy. Ca lỗi setup C3 ghi
`Address already in use (10048)` được giữ riêng; không quy nguyên nhân cho Windows hoặc
đổi logic sản phẩm bằng một rerun. Lượt rerun đầy đủ 16 file liên quan đã pass, source trước/sau từng lượt khớp. Đối chiếu
5.163 node cuối đều pass, 0 fail/error/skip/deferred/not-run, không thiếu/trùng: toàn bộ
5.137 ID B1 được giữ, thêm 26; 298 debt đều pass. Đây là kết quả hợp nhất các nhóm không
đổi từ suite đầu với toàn file chạy lại, không phải một invocation duy nhất pass cả suite.
200 file Python/SQL ứng dụng và hai file cấu hình image không đổi trong các lượt này;
các delta sau suite đầu chỉ ở test và deadline helper. Inventory giữ từng hash, JUnit,
source và seam lịch sử riêng; không tuyên bố suite đầu đã pass.

Chrome lịch sử sau sửa đã pass: 17 phép đo checkout/checkin có `own_connections` tối đa
0 và `other_connections` tối đa 1. Nó xác nhận connection của request UI khác là nguyên
nhân assertion đếm toàn pool báo sai; các 503/409 còn trong bản ghi là các fault accounting,
pricing và revoke được kiểm riêng, không phải business success bị bỏ qua.

## Cleanup và baseline

Lượt dọn cuối kiểm 48 project đã ghi trong context; ba project còn tài nguyên được dọn
17 container, 11 volume và 5 image tag của task. Không còn container ngoài baseline.
Giữ đủ 24 container ID và 166 volume có sẵn; kiểm label, bind mount và consumer trước
khi xóa. Không prune hoặc xóa tài nguyên không chứng minh được ownership. Các lượt dọn
chuẩn bị trước đó giữ report riêng, không cộng số ở đây thành tổng tích lũy.

Baseline có drift được ghi riêng: API/DB/Redis/MinIO của `ras` quan sát exit 255 trong
khoảng gián đoạn usage; worker từ restarting sang running ở snapshot cuối, không suy
ra đã khỏe hoặc nguyên nhân. Baseline ban đầu thiếu StartedAt chính xác. Không query,
migrate, restart hoặc sửa dev để xử lý drift này; chỉ project task được resume/dọn.

Đã xóa 23 file cấu hình credential tổng hợp ngoài repo sau khi dừng tài nguyên;
giữ raw JUnit/log/journal/backup/report. Scan cuối so sánh 39 credential tổng hợp
với toàn bộ file Git và không có match, không mở `.env` thật. Inventory giữ metadata,
hash và đường dẫn report cleanup/scan, không giữ giá trị credential.

## Giới hạn

Không đọc `.env`, query/migrate/restart dev, gọi arXiv/provider thật hoặc bật pricing thật.
Fake provider/capture không xác minh charge, routing, giá hay chất lượng semantic production.
Transport embedding giả trả vector 3 chiều để kiểm đồng nhất document/query và adapter;
không xác minh khả dụng hoặc số chiều thực của model đang chọn trên provider.
Dev outbox/HTTP không chứng minh delivery/HTTPS production. Journal host directory và volume
khác trên cùng máy không chứng minh storage production độc lập. DB và journal cùng rollback
vẫn nằm ngoài khả năng phát hiện; SIGKILL/fault injection không chứng minh chịu mất điện.
Restore chỉ bảo vệ các đối tượng thuộc hợp đồng hiện có, không mở rộng thành private deletion
replay hoặc S3 exactly-once. Storage, retention/scheduler/alerts, người vận hành thật và các
yêu cầu production khác còn thuộc gói sau.
