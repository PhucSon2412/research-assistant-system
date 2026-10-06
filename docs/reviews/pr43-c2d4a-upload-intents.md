# Review PR #43: upload notice và intent

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2d4a-upload-notice-intent.md) · [Handoff](../c2d4a-upload-notice-intent.md) · [Inventory agent](../c2d4a-test-inventory.json)

## Kết luận

**Cập nhật sau review:** PR #43 đã merge ngày 2026-10-06 tại `632ec683940a8bccc711c8a55743b0a3992c1d03`. Trạng thái OPEN và số kiểm bên dưới giữ đúng thời điểm/source review; không phải kiểm lại merge commit. [C2d-4b PUT original](../tasks/m6c2b2c2d4b-upload-original.md) đã giao, chưa triển khai/chưa kiểm; safety pending tới C4.

Ngày 2026-10-06, GitHub CLI/fetch xác minh PR #43 **OPEN, chưa merge**, head `f60645edf01d01794590db256d0ea4b5b1ef5c5e`, base/main `99db5285dfa59dd3d26e86345e849393ed88ce33` (PR #42). Code/test cuối `610e52d22292e16534c7c1771ca40fe6528b467a`; head thêm docs/inventory. Workspace sạch trước review; source migration `0039`, không query DB dev.

**Không phát hiện điểm chặn merge trong phạm vi hai JSON route; có thể merge PR #43.** Kiểm độc lập bên dưới xác nhận notice/intent dùng caller/binding/clock thật, token không trả ở nhánh từ chối, commit uncertain hoặc hậu kiểm lỗi. Không kết luận toàn upload, C2d/T14 hoặc restore/production an toàn. Safety **pending tới C4**.

Reviewer chỉ thêm docs, không sửa code ứng dụng hoặc commit/push/merge/comment PR. Giữ các cập nhật review này vào PR phù hợp khi coding agent làm lượt tiếp.

## Đối chiếu code và task

- Đúng hai route ở `api/app/api/routes/uploads.py`: notice dùng final caller hiện hành, intent chuyển vào helper hẹp `api/app/api/upload_intents.py`. `StrictBool` + validator chỉ nhận JSON true; Origin dùng helper trusted, giữ CSRF thật. Business metadata validation sau current User guard giữ Admin `403` khi notice/filename/size sai; schema validation vẫn trước handler.
- Writer lấy User NO KEY UPDATE, flush acknowledgement/intent/audit và FK trước journal exclusive; không shared upgrade. Kiểm scalar binding, exact session/User, current role/CSRF, latest confirmed heads/receipts/generation và clock sau SQL trước commit. Ack/intent mốc mới theo config, không gia hạn TTL khi SQL/khóa chậm.
- Known commit mới mở transaction response, intent SHARE → acknowledgement SHARE → journal shared; đọc lại binding/hash/unused/metadata và caller/TTL sau SQL. Không dùng ORM/proof trước commit, lấy User/row lock mới sau journal hoặc tạo auth event cho consent.
- Commit exception không trả token/retry và không suy mutation đã rollback. Known commit rồi hậu kiểm bị từ chối giữ mã tương ứng, không token; lỗi DB hậu kiểm báo response unavailable. UI messages phân biệt chưa biết outcome với đã lưu nhưng không trả token, form không PUT/retry khi intent lỗi.
- AST đối chiếu base xác nhận `upload_original`, `download_original`, parser và orphan helper giữ nguyên. Auth predicates/final helper/resource writer/authz/config/models/migrations, web form/proxy và Compose không đổi. Web production chỉ thêm error mapping. PUT claim trong test là SQL runtime mô phỏng đúng khóa intent, không phải kiểm toàn PUT/storage.

## Kiểm độc lập thực chạy

Conda interpreter và pip xác minh tại `C:\Users\ADMIN\.conda\envs\ras`. PostgreSQL standalone `ras-pr43-review-db-20261006`, label `ras.task=pr43-review-20261006`, bind `127.0.0.1:58455`. Explicit test admin URL/DB parts và runtime/recovery passwords riêng trước import fixture; journal thật ngoài repo. Runner chặn external HTTP sync/async, không đọc `.env` hoặc key provider. Nghiệp vụ dùng scoped cutover fixture; pending HTTP không patch.

| Nhóm reviewer | Pass |
| --- | --- |
| Toàn file `test_c2d4a_upload_intents.py` mới | 155 |
| Sáu C2d-3 writer success/transaction/flush/cleanup | 6 |
| C2d-1 HTTP pending/health/readiness thật | 5 |
| ORM khớp schema migrate | 1 |
| **Tổng selection API** | **167** |

**167/167 pass, 0 failure/error/skip, 413,422 s**, 3 warning thư viện/autogenerate hiện có. JUnit có 167 node unique, đều thuộc selection agent; 220 API Python/SQL hash trước/sau không đổi. JUnit SHA-256 `2ae4b069a168ad235c4ee8212f3307778db5c581ffef527fffca50055596fea8`.

Các ca gồm contract/strict consent, config/binding từng trường, A/B/Admin và session khác, thiếu/sai/lặp Origin, current role, revoke/disable/head/receipt/quarantine/held, TTL idle/absolute/intent tại/quá/sát hạn trong chính predicate SQL, rollback trước commit, commit landed/lost reply, hậu kiểm DB/actor lỗi và các thứ tự race User/journal/intent claim. Kiểm pool/transaction/SQL order và không tạo file/job/derived rows. Fault injection là giả lập Python với SQL thật, không crash/mạng/mất điện thật.

**Web chạy lại độc lập 47/47 pass, typecheck đạt.** Hai test mới kiểm messages và callbacks form thật qua `api()`/proxy tới HTTP upstream tổng hợp: chưa xác nhận không POST, intent lỗi không PUT/retry và checkbox reset. Đây không phải smoke trình duyệt DOM hoặc FastAPI end-to-end; không chạy lại build, artifact build của agent được giữ theo source/hashes đã đối chiếu.

## Đối chiếu chứng cứ agent và giới hạn

Đã đọc hai JUnit cuối của agent, kiểm SHA-256 và exact node set: **579 pass unique**, không failure/error/skip, hợp đúng selection. Collection 2685 unique, 2106 not-run. **259 source hash** API/web trước/sau khớp source hiện tại; code/test không đổi giữa commit kiểm `610e52d` và head. Đây là xác minh artifact, không phải reviewer chạy lại 579.

Reviewer chưa chạy 412 node khác trong suite agent, 2518 ngoài selection reviewer; không cộng các nhóm chồng nhau thành tổng mới. Không chạy lại mutant, full browser/product HTTP, PUT/download/parser/storage/provider, review/publication writers, restore/drill hoặc C3/C4. Các giới hạn trong handoff/inventory agent vẫn giữ, gồm vòng đời intent chưa có journal xuyên restore và response có thể tiếp tục gửi sau final boundary.

Đã dọn đúng container và sole anonymous volume thử sau khi kiểm ID/label/mount/consumer, xóa credentials thử. **24 container có trước review giữ ID/image/StartedAt nguyên trạng.** Evidence/JUnit/source/selection/scripts/cleanup metadata giữ ngoài repo tại `%TEMP%/ras-pr43-review-20261006`. Không query/migrate/restart dev, đổi `.env`, pricing/safety gate hoặc gọi provider thật.

Phần tiếp sau merge: giao PUT file và original download bằng task riêng, theo lock/I/O/boundary trong handoff. Review này không mở readiness hoặc ủy quyền triển khai các nhóm đó ngay.

Kiểm tài liệu sau cập nhật: 64 Markdown, 1006 liên kết nội bộ, 0 lỗi target/anchor/control character; `git diff --check` đạt. Workspace chỉ có docs review chưa commit, PR vẫn OPEN tại head nêu trên.
