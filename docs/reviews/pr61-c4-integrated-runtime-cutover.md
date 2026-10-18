# Review PR #61 — C4 tích hợp runtime, readiness và restore

Ngày **2026-10-16**, PR **OPEN/chưa merge**, base `44633b99f8407c5b15a8afb967afcd3c4d135018`, head `3f07901dc5c55c614ef374da0f572db9f225f49f`, source migration **0051**. **Không phát hiện điểm chặn merge mới trong phạm vi C4 đã review; có thể merge theo phạm vi này.** C4 đã chứng minh operational readiness có điều kiện trên môi trường cô lập. Chưa cutover dev, bật provider thật hoặc nghiệm thu production.

[Task](../tasks/m6c2b2c4-integrated-cutover.md) · [Handoff/runbook](../c4-integrated-runtime-cutover.md) · [Inventory agent](../c4-test-inventory.json) · [Kế hoạch MVP](../mvp-delivery-plan.md)

## Cơ chế đã đối chiếu

- `0051` tạo checkpoint riêng, không có hàng mặc định mở quyền. Chỉ recovery được gọi routine hoàn tất contract. Runtime không được sửa checkpoint/registration/operator map/case, kể cả qua column ACL; quyền TEMP/DDL/membership/replication hoặc identity owner làm gate đóng. Definer phải NOLOGIN và không có quyền sở hữu/đặc quyền ngoài hợp đồng. Reprovision gỡ cả column ACL trước khi cấp lại quyền.
- Gate đọc lại journal dưới shared lock, đối chiếu installation/genesis/head, checkpoint, projection và receipt/provenance; không dùng kết quả startup được cache hay hằng `ready`. Chế độ administrative chỉ bỏ kiểm identity runtime cho công cụ vận hành, vẫn kiểm integrity và contract. Business entry vẫn yêu cầu login runtime thật.
- Trigger chỉ tiến checkpoint qua các dòng liên tục đã xác nhận sau append/fsync. Nó không tạo checkpoint đầu tiên. Marker `recovery_begin` đã mirror không tự tiến bằng chứng reconciliation của C3; recover kiểm hậu điều kiện rồi mới ghi progress. `recover --hold-for-operator` vẫn đóng readiness; recover thường mới hoàn tất contract.
- Compose dùng cùng image Python cho migrate/provision/init/API/worker/beat; migrator, recovery và runtime có kết nối riêng. New-install rỗng được init/recover rồi cho bootstrap một lần qua runtime. Installation đã đăng ký không bị tự adopt/recover khi startup; runbook bảo trì vẫn phải thực hiện tường minh.
- CLI evaluation dùng context cô lập tường minh, provisioning riêng và runtime cho business; không fallback `.env`. Provider fixture chỉ ở test, không phải cấu hình provider production.

## Kiểm độc lập của reviewer

Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe`; hai project riêng `rasc39ea66da5` và `rasc48f09eac6`. PostgreSQL/MinIO/Redis/journal thật, runtime login thật. Không patch cutover/evaluate/ready trong các kiểm dưới đây; pytest dùng đồng hồ kiểm soát, còn image HTTP/Chrome dùng đồng hồ thực. Không đọc `.env` hoặc gọi arXiv/provider thật.

| Nhóm | Kết quả thực chạy |
| --- | --- |
| Contract C4, health/readiness, provenance B1, ORM/schema | **49/49 pass**, 179,45 s |
| Hai ca reviewer ngoài repo | **2/2 pass**, 17,69 s |
| Build và startup Compose từ source PR | Migrate/provision/init thành công; API/worker/beat/web healthy |
| HTTP qua image/proxy, pricing mặc định tắt | **7/7 kiểm đạt**, zero provider transport call |
| HTTP qua image/proxy với provider giả | **38/38 kiểm đạt** |
| Chrome thật qua Next proxy | **7/7 kiểm đạt**, 0 request ngoài origin |
| Hold/recover CLI trên cùng image và HTTP không seam | **5 kiểm đạt** |

**51 test pytest**, không failure/error/skip. Hai ca ngoài repo kiểm độc lập:

1. Disable User khi dòng journal ghi đủ nhưng `os.fsync` ném lỗi: API trả 503; mirror chưa xác nhận, checkpoint không tiến, readiness và business đóng. Sau recover với fsync hoạt động, gate mở nhưng User vẫn disabled; Admin còn dùng được. Đây là fault injection, không phải lỗi thiết bị thật.
2. Replay riêng marker hold: checkpoint đã thấy marker nhưng projection progress chưa vượt hold. Reconciliation cho operator tiến progress sau kiểm hậu điều kiện, vẫn không mở HTTP; recover thường mới mở.

Luồng image gồm consent riêng từng upload; Markdown/PDF/partial/no-OCR; worker extract/index; original đúng bytes và ACL A/B/Admin; keyword/hybrid/rerank và RAG ở item/collection/all; citation đúng version/file; approve/publish/index/revoke bản chung; quyền download/fulltext/external-processing riêng; membership, logout và reset. Request mới bị chặn sau revoke trong khi worker/cleanup đã dừng; private cùng bytes còn dùng được. Chrome kiểm form consent, download, RAG và mở nguồn, cùng owner denial. Provider transport giả không chứng minh HTTP/TLS hoặc charge của upstream thật.

Reviewer cũng chạy CLI hold → recover giữ operator hold → recover thường trên image: `/healthz` sống, readiness/protected route qua API và proxy đóng trong hold, rồi mở sau hoàn tất hậu điều kiện. Worker/beat của project thử được khởi động lại sau đó. Không chạy lại riêng Redis outage, năm full restore drill, CLI evaluation hoặc toàn bộ API/web suite.

**428 tracked source hashes trước/sau trùng**, khớp inventory agent tại head. **203 file API/migration/cấu hình được kiểm trong image khớp bytes host**; sáu service Python dùng cùng digest `sha256:3db66b2d5231fae4464934575d56c21440da413f9d592d9a73c5d6fec81fdefb`, user `ras`. Đây là image reviewer tự build bằng cache, không phải digest image agent.

## Đối chiếu chứng cứ agent

- **789 artifact SHA-256** đều hiện hữu và khớp; **428 source hash** khớp head. Collection và inventory có đúng **5.163 ID unique**; từng trạng thái cuối khớp JUnit trong **102 nhóm**, không thiếu/trùng: **5.163 pass**, 0 failure/error/skip/deferred/not-run. B1 giữ 298 debt trong inventory lịch sử, không miễn các node đó khỏi kết quả cuối.
- Đây là kết quả **hợp nhất nhiều lượt**, không phải một invocation toàn suite pass. Suite đầu có 27 failure và hai error; các file liên quan được chạy lại đầy đủ. Delta sau suite đầu chỉ ở test/harness, production API/image không đổi. Không cộng 5.163 vào số test reviewer tự chạy.
- Năm full drill có result/log/hash riêng: backup 41, session 35, reset 54, review closure 34, invitation 59, tổng **223 kiểm**. Reviewer kiểm artifact và hợp đồng script, không tự chạy lại năm drill. Bốn drill dùng harness ngoài repo với deadline queue 600 s và ROOT sửa về cùng repo; không giảm workload/assertion. Đây không phải số đo SLO.
- Evaluation artifact ghi 400 record audited, 261 lượt viewer/87 chunk, 0 ACL violation và 0 external attempt; 80 nhãn vẫn `pending_human_review`. Không có kết quả grounded answer/citation accuracy/abstention production. Shell exit 1 do warning native được giữ trong handoff; không đổi nó thành một lượt shell exit 0.
- Bốn countercheck có đúng assertion failure, không setup error; reviewer đối chiếu artifact/hash, không chạy lại mutant. Web 70/70, typecheck/build là kết quả agent; reviewer tự build web image và chạy Chrome nhưng không chạy lại riêng `npm test`/typecheck.

## Giới hạn và phần tiếp theo

- C4 khép bằng chứng tích hợp/readiness cô lập; không chứng nhận dữ liệu dev hiện có, HTTPS/delivery thật, người vận hành/UUID thật hoặc storage journal production độc lập. Mọi cutover môi trường thật cần task/runbook riêng với backup và kiểm hậu điều kiện.
- Chưa đối soát charge, routing/retention, khả dụng/số chiều model thật hay chất lượng trả lời ngữ nghĩa. Pricing thật vẫn chưa bật. Không có số đo p95 hoặc chứng nhận quy mô 1.000 paper.
- DB và journal cùng rollback/mất vẫn có thể không phát hiện. Các drill/fault/SIGKILL không chứng minh chịu mất điện. Private deletion replay, lịch backup/retention/alert và bảo vệ artifact nằm ngoài C4 hiện tại.
- Discovery failure lịch sử và một approval 503 trong chuẩn bị agent chưa được giải thích tận gốc; các lượt sau pass không chứng minh nguyên nhân đã sửa. Không thấy chúng tái diễn trong mẫu kiểm reviewer; giữ nguyên bằng chứng, không thêm retry sản phẩm hoặc miễn lỗi bằng nhãn baseline.

## Evidence, cleanup và tài liệu

Evidence reviewer: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr61-review-20261016`, gồm JUnit/log, hai test ngoài repo, HTTP/Chrome, image manifest, artifact audit và baseline/cleanup. Lỗi chuẩn bị của helper reviewer (quoting, đường dẫn/format Docker và phép đếm kiểu dữ liệu inventory) được sửa trước khi ghi kết quả tương ứng, không tính là pass sản phẩm. Không sửa production/test/migration/grants trong review.

Đã kiểm ownership/label và consumer volume trước cleanup; gỡ đúng **14 container thử**, tài nguyên Compose của hai project, **hai image tag** và **bảy file cấu hình/credentials thử**. **24 container có sẵn giữ nguyên ID/name/image/mount/label**, không còn container mới của task. Worker/beat dev đã Restarting ở baseline; RestartCount cùng tăng 16→34 và StartedAt đổi do vòng restart quan sát được. Không tác động hoặc suy nguyên nhân từ drift này. Scan 15 secret context thử trên Git/log/JUnit: 0 hit.

Report và các cập nhật mục lục/trạng thái được giữ **chưa commit** để đưa vào PR kế tiếp theo quy ước orchestrator. Không merge hoặc đăng comment GitHub trong lượt review này.
