# Journal kết thúc invitation (M6c-2b2b2)

[Mục lục](README.md) · [Quyết định](decisions.md#quyết-định-triển-khai-m6c-2b2b2-2026-10-03) · [Runbook](dev-setup.md#journal-invitation-m6c-2b2b2)

**Yêu cầu của chủ dự án:** bảo vệ link đã nhận/thu hồi trước restore backup cũ, với journal độc lập hiện hành; giữ bảo vệ session/reset của PR #29/#30. PR #30 đã merge tại `ce67ce8`, revision `0031`. Nhánh `feat/m6c2b2b2-invitation-journal` từ đúng base đó; thêm `0032`, không sửa revision đã merge. Chưa merge PR này.

**Lựa chọn triển khai của coding agent:** dùng cùng hash chain, mirror, `journal_confirmed`, recovery gate và `identity.session_commit.commit_auth`. Event `invitation_closure` đúng bốn trường bắt buộc, không null/thừa:

| Trường | Hợp đồng |
| --- | --- |
| `invitation_id` | UUID chuẩn, duy nhất trong mirror; không FK |
| `end_kind` | `accepted` hoặc `revoked` |
| `reason_code` | accepted: `invitation_accepted`, `legacy_accepted`; revoked: `admin_revoked`, `invitation_replaced`, `delivery_failed`, `legacy_revoked` |
| `ended_at` | ISO có timezone; so theo instant |

Không User UUID: replay không cần account, không được suy tài khoản đã commit từ closure. Không email/tên/link/token/hash/verifier/mật khẩu/payload delivery trong journal, audit hay log. DB token hash chỉ ở bảng invitation như trước. Parser và SQL độc lập từ chối thiếu/null/thừa, UUID/loại/mã/time sai. CHECK mirror/body dùng `IS NOT DISTINCT FROM`, kiểm cột bắt buộc không null; trigger INSERT kiểm dạng JSON/string/time trước append. Mirror thêm `invitation_id`, `invitation_end_kind`, `invitation_reason_code`, `invitation_ended_at`, không FK vì invitation/User có thể vắng trong snapshot.

## Trạng thái và replay

Invitation mở = `accepted_at`, `revoked_at`, `closed_by_recovery_at` đều null. Link dùng được còn cần `now < expires_at`, hash đúng. TTL tự nhiên không phát quyết định mới. Partial unique email áp đúng ba cột mở, kể cả lời mời hết TTL chưa có quyết định kết thúc. Mời lại vẫn kết thúc hàng cũ trước tạo hàng mới.

| DB khi replay | Kết quả |
| --- | --- |
| Link còn mở | `closed_by_recovery_at=ended_at`, `recovery_end_kind=end_kind`, `closure_reason=reason_code`; kết quả `invitation_closed_by_recovery` |
| Đã đóng cùng loại/mã/instant | Giữ nguyên, `invitation_already_ended` |
| Đã đóng khác quyết định/instant | `invitation_closure_conflict`, giữ gate đóng |
| Invitation vắng | Mirror `recorded_invitation_absent`; không dựng invitation/User |
| Mirror đã có | Vẫn kiểm hậu điều kiện từng closure; `invitation_not_ended`/conflict ngăn kết thúc recover |

Trạng thái API/UI là `closed_by_recovery`: “Link đã đóng khi khôi phục; không khôi phục tài khoản”. `accepted_at`/`accepted_user_id` vẫn null khi đóng qua replay, kể cả event gốc accepted. Không tạo User, verifier, session, link hay gán FK giả. Lý do chọn một trạng thái riêng cho cả accepted/revoked: journal chỉ chứng minh quyết định kết thúc link, không chứng minh tạo account/delivery commit. Trạng thái này cũng xuất hiện trên DB live khi append bền nhưng commit rollback rồi replay.

Inspect và accept đều từ chối trạng thái recovery trong TTL; Admin không thấy “accepted” trên account bị mất. Recovery không giữ slot email pending; mời lại được. Nếu account thật còn tồn tại thì vẫn `409 account_exists` như trước. Accept bình thường tạo đúng User và giữ cặp accepted timestamp/FK nhất quán, không tự đăng nhập.

DB giữ bất biến ID/email/hash/created/expires/inviter; trạng thái kết thúc, FK accepted, loại/mã/time không đổi lại được. Trigger hoãn buộc mọi INSERT/UPDATE kết thúc có mirror cùng quyết định/instant trong transaction; SQL thẳng cũng phải qua guard. Đã kết thúc không DELETE được; INSERT ID đã có journal không được mở lại (kể cả event vắng đối tượng). Mirror append-only; chỉ false → true khi toàn bộ trường khác giữ nguyên. Downgrade bị chặn ở DB đã đăng ký hoặc có journal.

## Transaction và khóa

Nhánh invitation: **advisory email chuẩn hóa → invitation UUID FOR UPDATE → INSERT/flush User mới và audit → journal advisory**. Khóa email dùng namespace riêng + digest 32 bit, không ghi email vào journal; collision chỉ tuần tự hóa thêm email, không đổi quyền. Khóa email giải quyết cả khi chưa có User/invitation, nên không đòi khóa User không tồn tại. Các hàng cũ khi mời lại khóa theo UUID. Băm mật khẩu ngoài khóa; API truyền đồng hồ callable, kiểm lại invitation và TTL dưới khóa sau băm/chờ khóa.

Không khóa User hiện có bằng FOR UPDATE trong nhánh này. Audit/inviter/accepted FK chỉ lấy KEY SHARE qua flush **trước** journal; tương thích khóa NO KEY UPDATE của auth. Không lấy khóa hàng cần thiết sau journal. Nhánh reset/session giữ nguyên **User UUID NO KEY UPDATE → reset token UUID → session UUID → journal**. Backfill dừng writer và khóa **email theo thứ tự → User → reset token → session → invitation → journal**; replay từng invitation lấy email nếu tồn tại rồi invitation rồi journal. Event vắng không cần dựng khóa email từ dữ liệu định danh không có.

| Thao tác | Transaction DB live |
| --- | --- |
| Accept | Tạo User + accepted closure + audit |
| Reinvite | Kết thúc invitation cũ (`invitation_replaced`) + tạo invitation mới + audit |
| Admin revoke | revoked closure (`admin_revoked`) + audit |
| Adapter báo lỗi | Transaction riêng thu hồi bù (`delivery_failed`) + audit nếu link còn mở |

Adapter chỉ nhận link sau transaction tạo/mời lại được xác nhận commit thành công. Adapter có thể đã gửi rồi mới báo lỗi: không gọi link là “chưa gửi”. Bù khóa email/hàng lại; nếu accept hoặc một closure khác đã thắng, giữ nguyên account/quyết định, không chuyển accepted → revoked và không xóa account. API vẫn `503 delivery_failed` vì delivery outcome không được xác nhận. Hợp đồng Origin/CSRF/session/Admin giữ nguyên.

Batch invitation/reset/session dùng một append/fsync chung: flush **toàn bộ** mirror false trước byte đầu; chỉ xác nhận **toàn batch** sau thành công. Mỗi luồng hiện tại thường chỉ có một invitation cũ do partial unique; test batch hai invitation khác email kiểm cả prefix, không nới constraint để dựng nhiều pending cùng email.

## Cửa sổ lỗi và kết quả API

| Cửa sổ | DB/journal và API |
| --- | --- |
| Append/fsync và commit thành công | Accept/create `201`, revoke `204`; account/link/audit nguyên tử. Create mới không có closure cũ vẫn phải commit xác định trước delivery |
| Mirror/flush/gate lỗi trước append | Rollback; `503 auth_unavailable` hoặc `service_recovering`; không delivery/không báo thành công |
| Chưa ghi, torn/prefix, hoặc dòng đủ nhưng fsync lỗi; DB commit | Toàn transaction commit, mọi mirror false; `503 auth_journal_unavailable`, gate đóng. Account có thể đã tạo, chưa báo accept thành công |
| Journal bền; commit rollback hoặc mất phản hồi | Replay quyết định sau rollback; `503 auth_outcome_uncertain`. Không suy account/link/delivery thành công; không delivery |
| Như trên, replay cũng lỗi | `503 auth_closure_pending_recovery`, gate đóng tới recover |
| Prefix/torn và DB rollback/process chết | Recover chỉ fsync/replay prefix hoàn tất; phần chưa vào journal chưa có bằng chứng đóng. Không tạo account/phần transaction còn lại |
| Journal và DB đều không giữ quyết định | Link có thể vẫn mở, gate có thể mở sau xử lý torn; trả lỗi, không cam kết đã đóng. Phải kiểm lại/thực hiện lại thao tác |
| Adapter đã nhận link rồi báo lỗi | Bù closure journal riêng; bù thành công → `503 delivery_failed`; bù lỗi → mã auth 503/gate theo cửa sổ tương ứng; accepted thắng được giữ nguyên |

Journal đủ dòng nhưng fsync lỗi: recover tự fsync rồi xác nhận. DB live có mirror ahead/file thiếu đuôi: chỉ `recover --confirm-db-is-current` sau xác minh **DB live** để sửa file; không dùng cờ trên snapshot restore. Prefix không chứng minh phần còn lại hoàn tất. Replay không chứng minh account đã tạo, link mới đã commit hoặc delivery đã gửi. Fault injection không chứng minh mất điện.

## Backfill và nâng cấp

Gate thêm `invitations_not_journaled`, status `unjournaled_invitations`, report `invitations_imported`. Accepted/revoked lịch sử còn trong DB chưa có mirror đóng gate. Migration không I/O journal; recover replay file trước, rồi import lịch sử theo batch chung, không tạo gap seq khi file ahead. `closure_reason=NULL` lịch sử dùng `legacy_accepted`/`legacy_revoked`, không suy admin/reinvite/delivery từ schema cũ. Account/FK lịch sử còn nguyên; không sửa chúng để import.

`init` từ chối DB có bất kỳ invitation; `register-existing` chỉ DB live đã xác minh và cũng import invitation. `hold` vẫn chỉ đọc seq/hash, chạy được trên `0031` trước migrate. Recover kiểm mọi closure kể cả mirror đã có; chạy lại không tạo account hoặc đổi quyết định. Gate common/review/session/reset không bị bỏ hay nới.

Lịch sử mất khỏi DB trước backfill không tái tạo được; backup cũ chỉ được bảo vệ bởi các quyết định journal đã nhận. Journal cùng mất/rollback với DB không phát hiện được. Credential/account status còn rollback: mật khẩu cũ có thể tạo session **mới**. Chưa có nơi lưu journal production, scheduler/membership/private deletion; độ bền mất điện chưa chứng minh. Task kế tiếp: thiết kế chống rollback credential/account status; lựa chọn storage/epoch/recovery cần được chủ dự án chốt trước phần coding phụ thuộc.

## Kiểm chứng

Xem [testing](testing.md#invitation-journal-m6c-2b2b2) và [runbook](dev-setup.md#journal-invitation-m6c-2b2b2): full API cuối **901/901**, web **42/42** cùng typecheck/build, đối chứng **8/8** bị test bắt, drill **59/59** với hai restore/recover và **0 provider call**. Lượt full đầu **899 passed, 2 failed** đã được ghi nguyên nhân/sửa fixture và chạy lại toàn inventory; JUnit cuối không thiếu/trùng/skip. Không gọi OpenRouter, không đổi pricing hay `.env`, không migrate/restart stack dev. Journal/artifact/log ở ngoài repo; tài nguyên test/drill đã được dọn đúng project.
