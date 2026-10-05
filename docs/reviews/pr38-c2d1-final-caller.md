# Review PR #38: C2d-1 final caller reads

## Trạng thái sau review (lịch sử giao task)

Docs review/orchestrator được giữ và đưa cùng PR C2d-2a. Triển khai mới xem [bàn giao C2d-2a](../c2d2a-resource-json-reads.md); phần bên dưới ghi nguyên trạng khảo sát trước triển khai, không cộng kết quả review vào suite mới.

Chủ dự án báo đã merge; fetch ngày 2026-10-05 xác minh `origin/main=d06664e53383071237c21e802f439fda348a1afd`, chứa source review `97c49d8`; head mã nguồn `0039`. Kết luận/phép đo bên dưới là lịch sử tại thời điểm review, không phải lượt kiểm lại sau merge. Review và các cập nhật docs của orchestrator vẫn chưa commit, coding agent tiếp giữ và đưa vào PR. [Task C2d-2a](../tasks/m6c2b2c2d2a-resource-json-reads.md) mới giao, chưa triển khai; safety pending tới C4.

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2d1-final-caller-reads.md) · [Bàn giao](../c2d1-final-caller-reads.md) · [Inventory agent](../c2d1-test-inventory.json)

## Kết luận

Ngày 2026-10-05, review source `97c49d84561306fa0f4688149a4d61b559f9bf35`, base `origin/main=ca4a623ea9cd4d2f8454659a1d66c48dbf8093d0` (merge PR #37), head source `0039`, không migration. Code/test API giống `13af731` mà agent dùng cho suite cuối; commit sau chỉ cập nhật docs. **Không phát hiện điểm chặn merge trong phạm vi C2d-1; có thể merge PR #38.** Reviewer không merge, push hoặc đăng comment PR; safety pending tới C4, chưa hoàn tất T14 toàn hệ thống.

Reviewer chạy **182/182 test chọn pass**, 0 failure/error/skip, **410,748 s**, 3 cảnh báo hiện có. Đây là lượt độc lập với **490/490** của coding agent; không cộng các lượt chồng nhau thành full suite. Không chạy lại full inventory, C2b/C2c-2 toàn nhóm, web, proxy/browser, object store, backup/restore, worker drill hoặc cutover C4. Các số not-run trong inventory agent vẫn là báo cáo của agent, không đổi thành pass từ review này.

## Phạm vi đã đối chiếu

- `identity.final_caller`: exact session–User binding; shared journal lock trên transaction hiện hành; scalar profile/session fields bỏ ORM identity map cũ; clock sau lock wait/profile query; SQL predicate latest confirmed head/receipt/generation và idle/absolute expiry. Không touch session, lấy khóa hàng auth, issue, commit hoặc rollback trong helper.
- `auth.current_session`: profile/role/expiry DB hiện hành, allowlist và shape cũ; idle expiry giữ phép min với absolute expiry.
- `admin.list_users`: dựng DTO sau query rồi kiểm Admin hiện hành; target held/legacy vẫn hiện để quản trị, không dùng account_live của target thay quyền caller.
- `admin.dev_outbox`: chỉ development + DevOutbox; flush audit FK trước journal lock, final Admin trước audit commit, proof transaction mới trước entries và proof/clock sau snapshot. Lỗi flush/commit chưa rõ không trả link; audit chỉ ID/mã và có thể đã commit khi hậu kiểm từ chối.
- Không mở thêm ACL kho chung/riêng, quyền operator hoặc provider; `/csrf` và `/auth/sessions` P2 giữ nguyên. Helper đọc shared không dùng để nâng khóa sang journal writer hoặc thêm User lock sau resource.
- Inventory AST khớp **63/63 route method/path/function/source**, không thiếu/thừa; bảng bàn giao phân loại read/write/storage/provider/durable job và các caller còn mở.

## Kiểm độc lập thực chạy

| Nhóm | Kết quả |
| --- | --- |
| `api/tests/test_c2d1_final_caller.py` | **93 pass**, gồm 5 HTTP pending không patch cutover |
| `api/tests/test_c2a_login_sessions.py` | **68 pass**, hồi quy login/session và listing P2 |
| `api/tests/test_invitations.py` | **20 pass**, HTTP invitation/outbox với runtime/receipt thật |
| `api/tests/test_migrations.py::test_orm_models_match_migrated_schema` | **1 pass** |
| Tổng lượt `review.xml` | **182 pass**, 0 failure/error/skip; **410,748 s** |

Các ca mới kiểm revoke/disable/quarantine/held/generation/receipt/head sau dependency, binding owner, stale Admin role/profile/expiry, các biên idle/absolute, gate/hold/journal ahead/unconfirmed, auth đổi sau audit commit, commit landed/rollback mất reply, TTL và expiry sau snapshot. Race có Event/deadline, quan sát advisory wait thật và kiểm reader/writer thắng; không chỉ dùng sleep. Helper chạy sau resource lock, không thêm User/session row lock; proof không tồn tại qua transaction. Pending thật vẫn chặn route bảo vệ/ready, health sống.

3 cảnh báo: Starlette testclient deprecation và hai computed-default warnings của ORM/schema hiện có. Runner giữ SHA-256 **210 file Python/SQL API** trước/sau suite, không file nào thay đổi trong lúc chạy; không mutant hoặc thay code ứng dụng ở lượt review. Bốn đối chứng trong inventory là phép thử của agent, reviewer không chạy lại.

Lệnh thực chạy từ repo, PowerShell, với runner ngoài repo đã cấp explicit URL thử riêng trước import fixture:

```powershell
$py = 'C:\Users\ADMIN\.conda\envs\ras\python.exe'
$reviewEvidence = Join-Path $env:TEMP 'ras-pr38-review-20261005'
& $py (Join-Path $reviewEvidence 'runner.py') `
  api/tests/test_c2d1_final_caller.py api/tests/test_c2a_login_sessions.py `
  api/tests/test_invitations.py `
  api/tests/test_migrations.py::test_orm_models_match_migrated_schema `
  -q --tb=short "--junitxml=$reviewEvidence\review.xml"
```

Evidence tại `%TEMP%/ras-pr38-review-20261005`: runner, `review.xml`, `review-summary.json`, hai source manifests, ownership/cleanup JSON. SHA-256 JUnit: `29748ecb8ab3b3c8b9668aa8eb99df96c7ecaea3a36fff0f8883f832ca0bc3e0`. Không commit artifact, credentials hoặc journal.

## Môi trường và giới hạn

Conda ras interpreter/pip đã xác minh. Chỉ container standalone `ras-pr38-review-db-20261005`, label `ras.review=pr38-20261005` / `ras.task=c2d1-review-20261005`, pgvector/pg16, bind `127.0.0.1:58444`. Runtime/recovery LOGIN và definer/migrator NOLOGIN thật; fixture chỉ scoped cutover ở nghiệp vụ, integrity/head/receipt/SQL/clock/commit không bypass. Explicit test URL/DB parts có trước pytest, không fallback `.env`; inherited RAS/key env bị bỏ khỏi process thử và sync/async HTTP transports bị chặn.

Sau suite, kiểm đúng ID/name/labels/image/port/mount ownership rồi xóa container và một anonymous volume; xóa hai file credentials tổng hợp dùng một lần, giữ evidence. Không đọc/sửa `.env`, truy vấn/migrate/restart dữ liệu dev, gọi provider hoặc đổi pricing flag.

Validator inventory AST lượt đầu dùng một prefix cho file nhiều router và bỏ qua health/ready, nên báo chênh giả; sửa validator ngoài repo để map từng router và main, kết quả 63/63. Không có lượt test API bị dừng hoặc failure trong review. Kiểm link/control characters và diff PR đạt trước viết báo cáo; kiểm lại tài liệu sau khi thêm link review.

Final boundary chỉ bảo vệ tới lúc transaction nhả journal lock; DTO/response đã được cho phép có thể tiếp tục gửi, bytes đã gửi không thu hồi được. Không chống DBA sửa trực tiếp ngoài trust boundary hoặc chứng minh độ bền khi mất điện. C2d JSON resource/file/review/publication/search/RAG/provider/durable-job integration, C3 operator/delivery và C4 storage/cutover còn riêng; không dùng kết luận merge này để mở readiness hoặc provider thật.
