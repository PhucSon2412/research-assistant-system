# Review PR #60 — initial receipt và hồi quy trước C4

Ngày **2026-10-15**, PR **OPEN/chưa merge**, base `041f77ef1c12b6277e7a5f46aaf32155b3673063`, head `c6f200e7b93ec44b345ac90063a05a6d947ff9ae`; source kiểm của agent `143347fc0f1f9e3ce25c2450da9e56dda5eadf67`, migration **0050**. **Không phát hiện điểm chặn merge mới trong phạm vi B1 đã review; có thể merge theo phạm vi này. Safety vẫn pending tới C4.** Kết luận không chứng nhận readiness, topology Compose, provider thật hoặc toàn bộ MVP.

[Task](../tasks/pre-c4-auth-receipts-and-regression.md) · [Handoff](../pre-c4-auth-receipts-regression.md) · [Inventory agent](../pre-c4-test-inventory.json) · [Kế hoạch MVP](../mvp-delivery-plan.md)

## Cơ chế đã đối chiếu

- Migration `0050` giữ các revision cũ. BEFORE receipt guard kiểm parent/User của lần tạo mới còn trong transaction hiện hành; kiểm deferred từ receipt trở về parent/cặp typed auth event, installation/genesis, thứ tự event, closure và audit. Generic initial reason cho User cũ hoặc reuse parent đã commit bị từ chối. Kiểm live xid phục vụ INSERT mới, không dùng `xmin` lịch sử làm bằng chứng qua restore.
- `ras_auth_initial_pair`, `ras_auth_initial_status_verified` và `ras_auth_receipt_provenance` được dùng chung trong account/session/login, status/reset, enable, operator inspect và recovery. Receipt cũ không có provenance bị đóng quyền rồi quarantine/held khi recover; không sửa/xóa receipt hoặc dựng chứng cứ. Profile/role hiện tại không thay bằng chứng creation-time; consumer vẫn kiểm role hiện tại riêng.
- `verified_*` giữ case C3, operation/User/action/installation/time và trạng thái journal phù hợp. Receipt/credential mất sau restore không được dựng lại. Các outcome append/fsync, authority cuối, release lỗi/Ctrl-C của C3 vẫn giữ qua hồi quy.
- Reprovision cấp lại EXECUTE cho NOLOGIN definer trên tập helper cần thiết; không cấp raw auth writes cho runtime. Hai lần provision rồi reset/login được kiểm bằng login runtime thật, kèm các quyền âm.
- Fixture positive chuyển sang bootstrap/invitation hoặc hai quyết định C3 thật. Chỉ cutover result được thay tại seam test nghiệp vụ; integrity, hold, SQL privilege, receipt, ACL và outcome vẫn thật. Negative/snapshot setup dùng kết nối privileged riêng. Các assertion legacy đã được đối chiếu với hợp đồng runtime; readiness 200 vẫn giữ expectation và deferred, không đổi thành pass 503.

## Kiểm độc lập của reviewer

Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe`, project cô lập `rasc3e52eb333`, PostgreSQL/roles/journal thật; runner dựng MinIO/Redis riêng và chặn HTTP ngoài test. Không dùng `.env`, stack dev, key hoặc provider thật.

| Nhóm | Kết quả | Thời gian pytest |
| --- | --- | --- |
| Provenance mới, C3 core, disable/enable, ORM/schema | **119/119 pass** | 529,64 s |
| Bootstrap pair/fault matrix, discovery tombstone, special-password DB/CLI | **29/29 pass** | 74,64 s |
| Ba ca reviewer ngoài repo: login/reset/session SQL trên forged history và allowed controls | **3/3 pass** | 13,09 s |

Tổng **148 repo + 3 ca ngoài repo pass**, 0 failure/error/skip; hai nhóm repo không trùng node. Nhóm đầu gồm 19 ca B1, 93 C3, sáu disable/enable và một ORM/schema. Nhóm sau gồm bảy pair guard, 20 cửa sổ append/commit, một discovery và một delimiter-password. Ba ca ngoài repo kiểm hai account có parent thật vẫn hoạt động, hai account mang receipt giả từ `0049` không login, không nhận reset token và không tạo session qua SQL sau upgrade. Không có session/token mới cho account bị từ chối.

Nhóm B1 có hai vòng `pg_dump`/`pg_restore` thực, journal giữ ngoài dump, recover lặp và kiểm valid/forged history. Đây là kiểm DB/provenance, không thay năm full Compose/object/browser restore drill của C4. Fault matrix dùng fault injection, không chứng minh mất điện. Reviewer không chạy lại toàn inventory, web/Chrome/ConPTY riêng, provider/charge hoặc readiness ready.

**417 tracked source hashes trước/sau trùng** và khớp source đã niêm của agent. Lượt gọi đầu sai tên node ORM, exit 4 ở collection, không có test sản phẩm chạy; giữ `collect-error.log/xml`, sửa tên rồi chạy hai nhóm như bảng. Không sửa source/test/migration/grants trong review.

## Đối chiếu inventory và artifact agent

- Kiểm **236 artifact SHA-256**, không thiếu/sai hash; **417 source hash khớp head**. Đối chiếu collection 5.137 ID unique với inventory và từng trạng thái cuối với JUnit tương ứng trong 102 nhóm có XML: **5.136 pass, một deferred C4**, không thiếu/trùng node cuối. Đây là kiểm artifact, không phải reviewer chạy thêm 5.136 test.
- Kết quả được nối từ nhiều lượt cùng source; lượt gián đoạn, fail và rerun giữ riêng. Không gọi full suite pass vì `test_health.py::test_readyz_ok_after_migration` chưa chạy đạt không seam. Năm full restore drill và CLI evaluation nằm ngoài mẫu số pytest, chưa được chứng nhận trong B1.
- Hai XML đối chứng cuối: writer có bốn assertion failure, consumer có một assertion failure, không setup error/skip; reviewer đọc artifact và kiểm hash, **không chạy lại mutant**. Các đối chứng không tính vào pass API.
- Web 70/70, typecheck/build là kết quả agent trong inventory; reviewer không rerun vì không đổi web. Các browser/proxy/process pass của agent vẫn mang giới hạn cutover seam được liệt kê để C4 chạy lại.

## Giới hạn và việc C4 phải tiếp nhận

1. **Discovery còn một hiện tượng chưa rõ nguyên nhân:** `test_persistent_oai_deleted_record_is_hidden_and_checkpointed` fail ở lượt agent rồi pass khi rerun cùng source. Reviewer chạy một lần và pass. Điều đó không giải thích failure cũ hoặc chứng minh hết flaky; C4 cần giữ kết quả này và ghi outcome/lease/gate nếu tái diễn, không che bằng retry sản phẩm hay nhãn baseline.
2. C4 phải tích hợp cùng image/revision, role runtime/recovery/definer/migrator/operator, provisioning và recovery/cutover predicate thật. API/worker/beat Compose còn identity owner như handoff mô tả; không chỉ đổi hằng pending thành ready.
3. Giữ expectation nghiệp vụ và chạy HTTP/browser, worker/beat cùng năm full restore drill **không cutover seam**. CLI evaluation environment còn owner/`.env` legacy, cần nối topology runtime cô lập trước khi chứng nhận; sửa seed không đồng nghĩa CLI hoàn chỉnh đã chạy.
4. Storage journal production, ủy quyền/inventory người thật, charge/routing/retention và độ bền chưa được xác minh bằng B1. DB/journal cùng rollback có thể không phát hiện; không mở pricing hoặc cutover dev bằng review này.

## Evidence và cleanup

Evidence reviewer ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr60-review-20261015`, gồm ba XML/log, test ngoài repo, artifact audit, source hashes, baseline/after Docker và `review-summary.json`. Artifact agent giữ đường dẫn riêng trong inventory.

Đã kiểm label/ownership và mount không chia với baseline trước khi gỡ đúng ba container thử cùng tài nguyên Compose của project; ba file cấu hình/credentials thử đã xóa. **24 container có sẵn giữ nguyên ID/name/image/mount/label** (mount đối chiếu theo tập, không theo thứ tự Docker trả về). Worker/beat dev đã Restarting từ baseline: RestartCount tăng 204→222 và 211→229, StartedAt đổi; chỉ ghi quan sát, không sửa hoặc suy nguyên nhân. Scan log reviewer không chứa bảy secret context thử. Không query/migrate/restart dev, đọc/sửa `.env`, gọi provider/arXiv hoặc đổi safety/pricing.

Docs review được giữ chưa commit để đưa vào PR kế tiếp theo quy ước orchestrator; PR #60 chưa merge trong lượt review này.
