# Review PR #66 — HTTPS, SMTP TLS, limiter và CI

## Review lại tại `5ca3e4c` — ba P2 đã sửa

Ngày **2026-10-20**, head **`5ca3e4c93a8f9a6e07da4afbe1cdf82ac01448fd`**, code sửa **`96bd196d7f46df153c8d49b0eb7eb5392138a74c`**, source **0052**. PR **OPEN/chưa merge**, đã kiểm GitHub. **Không phát hiện điểm chặn merge mới trong phạm vi M8a đã kiểm.** Ba P2 tại `c0b20aa` bên dưới là lịch sử trước sửa.

### Kiểm lại từng P2

- **P2-1 đã đóng:** parent `SmtpDelivery` giữ deadline chung, gửi job qua stdin tới `app.smtp_transport`, kill/reap đúng child khi hết hạn, không nhận kết quả thành công muộn. Child dùng TLS bắt buộc verify và environment tối thiểu, không nhận DB/service secret. Bốn repro SMTP cũ giữ nguyên: control success **0,125 s**, SMTPS/STARTTLS nhỏ giọt và lệnh cộng dồn đều `smtp_timeout` **0,609–0,610 s** với deadline 0,6 s. Repro DNS cũ patch parent không còn kiểm được đường gửi; reviewer thay bằng hook trong **child thật**, có marker PID khớp `last_child.pid` và khác PID parent. DNS chậm 2 s và treo 3.600 s đều timeout **0,609 s**, child đã reap, **0 kết nối SMTP**. Đây là lỗi DNS tiêm tổng hợp, không sự cố DNS thật. Không có retry; server có thể đã nhận DATA trước timeout, compensation vẫn áp dụng.
- **P2-2 đã đóng trong scope deploy pilot:** deploy/test harness dùng cùng digest Chainguard đã chọn; health `mc ready local` có thật và `/minio/health/ready` đạt. Reviewer kiểm registry cả **4 image resolve**. Trên container/volume thử: **7 kiểm tra storage đạt**, gồm put/readback SHA-256, list/delete/restart và upgrade từ image cũ. Volume root cũ thực sự `not_ready` với image UID 65532; bước chown một lần trong runbook khôi phục đọc object cũ và ghi mới. Không chạm volume dev. Reviewer **không** xóa cache/fresh-pull hoặc rerun toàn image/Chrome; các phần đó chỉ đối chiếu artifact agent. Dev compose cố ý giữ default tag cũ cho dữ liệu dev có sẵn, máy dev mới cần override digest theo hướng dẫn; không coi default đó là đường pilot.
- **P2-3 đã đóng:** scanner giữ đúng bytes vừa đọc và quét, chỉ ghi vào publish directory mới khi mọi input/context hợp lệ; thiếu file không còn clean. Workflow chỉ upload publish copy khi `steps.scan.outputs.clean == 'true'`, không upload raw. `!cancelled()` giữ khả năng publish JUnit sạch khi pytest thất bại; cleanup vẫn `always()`. Bốn repro CI cũ đạt. JUnit thật của reviewer cũng scan sạch, output **`clean=true`**, publish copy có **cùng SHA-256** với input. Không upload dữ liệu thử lên GitHub.

### Reviewer thực chạy

| Nhóm | Kết quả tại head review lại |
| --- | --- |
| Bảy file API: SMTP/deadline/CI guard/limiter/delivery + cookies/CSRF/Origin | **156/156 pass**, 0 fail/error/skip, exit 0, **160,09 s**; runtime/PG/Redis/MinIO/journal cô lập |
| Repro SMTP cũ | **4/4 pass** |
| DNS độc lập, hook PID trong child | **2/2 pass**, chậm và treo đều bị thu hồi |
| Repro CI cũ | **4/4 pass** |
| Storage new install/restart + legacy upgrade trên volume riêng | **7/7 checks pass**, có control upgrade thiếu chown không ready |
| Deploy checker + registry | Exit 0, **4 registry resolve**; **Settings trong image NOT-RUN** trên Windows, checker ghi rõ |

Tổng **156 repo + 10 test độc lập pass**, storage 7 checks ghi riêng, không cộng số request/check vào số test. Web không đổi trong bản sửa nên không chạy lại; kết quả reviewer trước là 79/79 + typecheck ở `c0b20aa`, không gọi đó là pass mới. Không rerun full inventory/restore, 12 countercheck, toàn image/Chrome hoặc fresh-pull. Phạm vi kiểm đủ cho ba bản sửa này; chưa là nghiệm thu triển khai thật.

### Đối chiếu source, CI và artifact

Đã trực tiếp kiểm GitHub run **39372151281** tại `96bd196`: **ba job success**. **11 hash artifact** khớp (các JUnit trước/sau/local/GitHub, storage, smoke và counterchecks metadata). Local và GitHub đều **1.122 pass**; khác đúng **hai parameter ID** của `test_leak_publishes_nothing` đã đổi từ payload sang ID tường minh để không lộ fixture token trong JUnit, không phải thêm/bỏ hai test. 156 ID reviewer là subset đúng của JUnit GitHub cuối. Không cộng các kết quả agent thành test reviewer.

**54/55 changed-source hash** trong inventory khớp head hiện tại. Ngoại lệ duy nhất `AGENTS.md` khớp bytes Git tại `96bd196`, đã được sửa docs tiếp trong `5ca3e4c`; không phải source production lệch. Head cuối còn nâng timeout job CI 100→130 phút và cập nhật docs; không sửa code transport/guard/storage sau commit CI đã kiểm. Không chờ/rerun CI trên head docs/timeout cuối. **645 tracked source/data hash reviewer trước/sau trùng**, gồm phiếu/report/evaluation thật.

### Tài nguyên, giới hạn và kết luận

Evidence: `C:/Users/ADMIN/AppData/Local/Temp/ras-pr66-rereview-20261020`, gồm JUnit/log, repro cũ và hook DNS-child, registry/deploy output, `minio-check/minio-check.json`, publish copy/scanner output, audit và source/baseline/cleanup.

Đã dọn **3 container infra reviewer** theo đúng project/label, cùng network, credential/config và pytest fixture dưới context riêng. Script storage dọn container/2 volume thử theo label. Không còn container mới; **24 container có sẵn giữ ID/name/image/mount/label**. Beat/worker đã restarting trước review, RestartCount **195→200**; không can thiệp. Không dừng process theo tên, không đọc key/`.env`, query/migrate/restart dev/Dify, provider/arXiv/email thật, đổi pricing hoặc nhập phiếu human.

Có thể merge PR #66 trong scope M8a đã review. Các giới hạn còn nguyên: chưa domain/ACME/SMTP/account/operator/journal độc lập thật; chưa cutover, timing enumeration và benchmark chưa đo. Digest public cần kế hoạch mirror/cập nhật trước vận hành; chown volume cũ là bước operator có kiểm soát, không tự chạy trên dev. M8b journal/backup/alert/diễn tập vận hành và các điều kiện D/M7b còn riêng; **0 verdict human thật**, không mở pricing hoặc ngân sách live.

Report, prompt đánh dấu đã xử lý và trạng thái docs để **chưa commit** cho PR kế tiếp; reviewer không merge hoặc gửi comment GitHub.

## Lịch sử review tại `c0b20aa`

Ngày **2026-10-20**, base `3b59a1db4e784df0891a844ba968ba85e2797746`, head **`c0b20aa201c52820a7921534ab68620fe4597e16`**, source **0052**, PR **OPEN/chưa merge** (đã kiểm GitHub). **Có ba P2 cần sửa trong cùng PR trước merge.** Không đổi production/migration/grants hoặc bật pricing trong review này.

[Handoff agent](../m8a-pilot-entry-hardening.md) · [Inventory agent](../m8a-test-inventory.json) · [Prompt sửa cùng PR](../tasks/fix-pr66-smtp-deadline-deploy-image-ci-artifacts.md)

## P2-1 — SMTP không được giới hạn bởi deadline tổng

Vị trí: [`SmtpDelivery.send`](../../api/app/delivery.py), đặc biệt `SMTP_SSL/SMTP` ở dòng 205, `step()` và `send_message` dòng 228, cùng nhánh QUIT dòng 256.

`remaining()` chỉ được dùng để đặt timeout socket trước các thao tác cấp cao. `smtplib` có nhiều lần đọc bên trong một thao tác; timeout socket là thời gian chờ từng lần I/O, không phải tổng thời gian cho dòng trả lời hoặc toàn bộ các lệnh MAIL/RCPT/DATA. Không có kiểm deadline sau `send_message` trước khi trả thành công. DNS trong constructor cũng không chịu socket timeout đó.

Reviewer chạy **SMTP loopback thật với TLS được xác minh bằng CA thử**, message và recipient tổng hợp, deadline 0,6 s:

| Ca độc lập | Kết quả thực | Kỳ vọng |
| --- | --- | --- |
| Server nhanh, implicit TLS (control) | success, **0,031 s** | success |
| DATA reply nhỏ giọt, implicit TLS | **success, 2,016 s** | timeout trong deadline + biên scheduling nhỏ |
| DATA reply nhỏ giọt, STARTTLS | **success, 2,047 s** | timeout |
| MAIL/RCPT/DATA mỗi bước chậm 0,18 s | **success, 0,718 s** | một deadline dùng chung, không cộng thành nhiều timeout |
| DNS được tiêm chậm 2 s rồi resolve về loopback | `smtp_timeout`, nhưng mất **2,031 s** | DNS cũng bị giới hạn bởi deadline 0,6 s |

Đây là **4 repro fail + 1 control pass**. Hai ca nhỏ giọt có thể kéo dài hơn nữa nếu server tiếp tục gửi; DNS chậm đã tái hiện bằng fault injection, không phải sự cố DNS thật. Các test đang có chỉ trì hoãn nguyên reply nên vẫn pass. Chưa chứng minh process thoát khi DNS treo vô hạn; cần bao nhánh đó trong bản sửa thay vì chỉ thêm kiểm thời gian sau một lời gọi blocking.

Hệ quả: timeout tổng 10 s được công bố không giữ được, request invite có thể vượt proxy 15 s, hoặc background reset giữ thread lâu hơn dự kiến; kết quả muộn còn có thể bị báo thành công. Cần deadline thực cho toàn send, kể cả DNS/greeting/TLS/AUTH/mọi reply/DATA và cleanup. Không chỉ kiểm thời gian sau khi đã đợi xong; không để thread gửi tiếp ở nền sau khi host báo timeout. SMTP có thể đã nhận DATA trước khi timeout, nên giữ outcome/compensation và không tự retry như hợp đồng hiện hành. Không tắt TLS verify để sửa timeout.

## P2-2 — đường cài mới của deploy mới vẫn phụ thuộc image cache cũ

Vị trí: [`deploy/compose.yaml`](../../deploy/compose.yaml) dùng cứng `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z`; healthcheck dòng 148 dùng `mc ready local`. Dev compose và harness mặc định cũng còn tag này.

Reviewer chạy metadata registry từ máy hiện tại: `docker manifest inspect` tag trên trả **exit 1, `no such manifest`**. Trong khi digest Chainguard hiện dùng trong CI trả **exit 0**. Không xóa cache, volume hay image có sẵn để kiểm. Infra reviewer chạy được từ cache hiện có, vì vậy kết quả đó **không** chứng minh cài mới tải được dependency.

Đây là phụ thuộc cũ nhưng được đưa nguyên vào **bộ deploy mới của M8a**. Tiêu chí new-install/runbook không được đáp ứng trên máy sạch; báo là quyết định mở cho triển khai sau sẽ để gói này không có đường cài chạy được. CI đã thay image test nhưng deploy contract chỉ render config nên không bắt được vấn đề. Sửa baseline image và health/readiness phù hợp, kiểm pull từ registry và cài mới có volume riêng; không chỉ thay docs hoặc dùng cache/override chỉ dành cho test.

**Lựa chọn orchestrator cho bản sửa pilot:** dùng baseline **`cgr.dev/chainguard/minio@sha256:709099319add48dfb44aa596a7070d3ddd8dd04d7ae881765719aec29df9e510`** đã dùng ở CI, sau khi kiểm provenance/version/license/runtime compatibility và health phù hợp. Không cần hỏi chủ dự án chọn registry lại. Digest hiện tra được từ registry, chưa được reviewer kiểm cài bộ deploy/health/data compatibility. Đây là lựa chọn cho nhóm thử, không chứng nhận support/retention image hoặc production. [Tài liệu chính thức Chainguard](https://edu.chainguard.dev/chainguard/containers/getting-started/web-and-data-services/minio/) nêu đường pull công khai; [provenance](https://images.chainguard.dev/directory/image/minio/provenance) phân biệt registry public và production. Không giả định image tối giản có `mc`, shell hay curl.

## P2-3 — CI vẫn upload file bị scanner từ chối

Vị trí: [workflow](../../.github/workflows/ci.yml), bước Scan dòng 56 và Upload dòng 59–65; [`m8a_ci_scan.py`](../../scripts/m8a_ci_scan.py) xử lý file thiếu.

Upload dùng **`if: always()`**, không phụ thuộc kết quả scan và trỏ đúng file JUnit thô đã scan. Scanner báo lỗi nhưng không xóa hoặc chuyển file sang vùng không được publish. Theo [ngữ nghĩa chính thức của GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#always), `always()` vẫn cho bước chạy sau một bước thất bại; job bị đỏ không thu hồi artifact đã upload.

Reviewer dùng file XML và password **fixture tổng hợp ngoài repo**, không upload gì lên GitHub:

- Control sạch: scanner exit 0.
- Có fixture password: scanner exit 1, **file vẫn tồn tại và còn password**, condition upload của workflow là `always()`; trace theo workflow cho thấy upload vẫn được cho chạy.
- Thiếu XML: scanner in `MISSING` nhưng **exit 0**, trái mô tả “không coi thiếu file là sạch”.

Hai regression fail và hai control pass. Trace upload là kiểm cấu hình theo semantics Actions, **không phải** thử xuất secret lên GitHub. Chưa có bằng chứng artifact của ba run thực đã rò secret; finding nằm ở đường lỗi guard, không suy có lộ credential thật.

Cần ràng buộc publish vào bằng chứng scan thành công và đúng file được scan (hoặc output sanitized riêng). Vẫn có thể upload **JUnit sạch khi pytest fail**, nhưng không upload raw khi scan lỗi/mất context/file thiếu/unreadable. Cleanup vẫn chạy dù test hoặc scan lỗi; job phải giữ failure, không `continue-on-error` hay sửa exit để xanh.

## Reviewer đã chạy

| Nhóm | Kết quả |
| --- | --- |
| API: ba file M8a + cookie/CSRF + Origin | **136/136 pass**, 0 fail/error/skip, exit 0, **151,70 s**; PostgreSQL/Redis/MinIO/journal cô lập, runtime thật |
| SMTP độc lập | **1 control pass / 4 repro fail** như P2-1 |
| CI guard độc lập | **2 control pass / 2 regression fail** như P2-3 |
| Web | **79/79 pass**, typecheck exit 0 |
| Deploy checker | Exit 0 cho render/static contract; **Settings trong image NOT-RUN trên Windows**, checker hiện báo rõ |
| Registry metadata | Tag deploy exit 1; digest candidate exit 0, không chạy container candidate |

Tổng test độc lập **3 control pass / 6 regression fail**, gom ba P2 (MinIO là kiểm registry/deploy riêng, không cộng thành unit test). Mỗi failure được giữ trong evidence; không sửa source để che lỗi và không dùng test repo pass để bác repro.

## Đối chiếu chứng cứ agent và giới hạn review

- Trực tiếp kiểm GitHub run **39284225688** tại `6cb5253`: ba job success. **5 artifact hash** khớp (JUnit local/GitHub cuối và HTTP/Chrome/countercheck metadata); hai JUnit khớp đủ **1.102 ID pass**. 136 node reviewer là subset đúng của chúng. **49 hash changed source** trong inventory khớp head. Không tính các kết quả agent thành test reviewer.
- **641 tracked source/data hash reviewer trước/sau trùng**, gồm evaluation/phiếu thật. Không chạy full API, web build, Chrome/image full smoke, 7 countercheck, upgrade/restore hoặc lại toàn selection 1.102; lỗi cụ thể đã tái hiện và không có thay đổi journal/schema. Chỉ audit metadata/hashes của các phần không rerun. CI của docs-only head `c0b20aa` không được dùng làm kết quả kiểm mới.
- SMTP TLS/Origin/trusted-peer/limiter có hồi quy đúng phạm vi; chưa phát hiện bypass mới ở các phần đó trong review này. Domain/ACME/SMTP/operator/journal độc lập thật, timing enumeration, benchmark và chất lượng RAG vẫn chưa nghiệm thu. Không mở pricing hoặc thêm live inference.
- Sự cố `taskkill` theo tên `python.exe` của lượt agent là lịch sử, chưa biết tác động ngoài task; không xem container giữ nguyên là bằng chứng mọi host process không bị ảnh hưởng. Reviewer không dừng process theo tên.

## Tài nguyên và bàn giao

Evidence: `C:/Users/ADMIN/AppData/Local/Temp/ras-pr66-review-20261020`, gồm `selection.xml/log`, `test_smtp_deadline.py`/`smtp-deadline.log`, `test_smtp_dns.py`/`smtp-dns.log`, `test_ci_guard.py`/`ci-guard.log`, registry logs, audit/source/baseline/cleanup.

Đã kiểm label và dọn đúng **3 container** infra reviewer cùng network; xóa hai file credential/config infra. **24 container có sẵn giữ ID/name/image/mount/label** (mount được đối chiếu như tập, không dựa thứ tự Docker trả về). Beat/worker đã restarting trước review, RestartCount **17→24**; không can thiệp. Không đọc key/`.env`, query/migrate/restart dev/Dify, provider/arXiv/email thật hoặc nhập phiếu human. Không có verdict thật mới.

Checker cleanup đầu của reviewer exit 1 vì so thứ tự list `Mounts` ở ba container Dify; đối chiếu lại theo tập xác nhận **chỉ khác thứ tự, không đổi mount hay container**. Giữ nguyên log/result lỗi và `cleanup-verified.json` sửa cách đối chiếu; không thao tác lại Dify. Không coi lỗi checker này là finding của sản phẩm.

Giữ report/prompt/trạng thái docs **chưa commit** cho agent sửa cùng PR #66. Reviewer không merge, gửi comment GitHub hoặc sửa production. Chưa chuyển M8b; M7b vẫn `awaiting_human_review`, D còn các điều kiện live/policy/paper thật riêng.
