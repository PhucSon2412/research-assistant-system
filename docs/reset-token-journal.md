# Journal kết thúc reset token (M6c-2b2b1)

[Mục lục](README.md) · [Quyết định](decisions.md#quyết-định-triển-khai-m6c-2b2b1-2026-10-03) · [Runbook](dev-setup.md#journal-reset-token-m6c-2b2b1)

**Đã xác nhận bởi chủ dự án:** với journal độc lập, hiện hành, reset token đã dùng/thu hồi không dùng lại được sau restore/recover, kể cả link còn trong TTL cấu hình 30 phút. PR #29 đã merge `d38c6b8`; nhánh `feat/m6c2b2b1-reset-token-journal` từ `origin/main` tại commit đó. Chỉ vòng đời reset token; vẫn kiểm TTL phía server. Hết hạn đơn thuần không phát sự kiện consumed/revoked.

**Lựa chọn triển khai của agent:** cùng journal/chuỗi hash M6c-1, kind `reset_token_closure`, revision `0031` sau `0030`. Body đúng năm trường bắt buộc, không null:

| Trường | Giá trị |
| --- | --- |
| `token_id`, `user_id` | UUID chuẩn |
| `end_kind` | `consumed` hoặc `revoked` |
| `reason_code` | `password_reset` (consumed); `reset_replaced`, `reset_completed`, `user_disabled` (revoked); `legacy_consumed`/`legacy_revoked` cho lịch sử không có nguyên nhân |
| `ended_at` | ISO có timezone, so như một instant |

Không raw token/hash, email, mật khẩu/verifier, cookie/CSRF hay paper. Parser từ chối thiếu/null/thừa, sai ID, loại/mã không khớp và thời điểm sai. SQL độc lập cũng kiểm các lỗi này và trường thừa chứa secret trong body/envelope. Mirror thêm `reset_token_id` unique, `reset_token_user_id`, `reset_end_kind`, `reset_reason_code`, `reset_ended_at`; **không FK** vì snapshot có thể thiếu token/User. CHECK khớp tất cả trường với body ngay INSERT, dùng `IS NOT DISTINCT FROM` cùng kiểm không null; trigger kiểm định dạng/body và owner trước byte journal đầu tiên. Append-only và false → true của `journal_confirmed` so cả cột mới.

`password_reset_tokens.closure_reason` giữ mã writer mới. Token cũ đã kết thúc còn null ở cột này được đọc là `legacy_consumed`/`legacy_revoked`; không bịa nguyên nhân cũ. Trigger hoãn buộc mọi INSERT/UPDATE kết thúc có mirror đúng ID/User/loại/mã/**thời điểm** trước commit. Trigger BEFORE giữ ID/User/hash/created_at/expires_at bất biến; đã kết thúc không mở lại/đổi loại/mã/time; INSERT không tái tạo token mở có ID đã journal. Downgrade bị chặn khi đã đăng ký hoặc có journal records. Không sửa revision đã merge.

## Writer và transaction

Khóa **User theo UUID (`FOR NO KEY UPDATE`) → token theo UUID (`FOR UPDATE`) → session theo UUID (`FOR UPDATE`) → journal advisory lock**. Khóa User tương thích KEY SHARE của FK audit như PR #29. Băm mật khẩu ngoài khóa, kiểm lại active/token/hash/TTL dưới khóa. Không lấy thêm khóa auth sau journal.

`identity.reset_tokens` khóa/tập hợp token closure; `identity.sessions` tập hợp session revocation; `identity.session_commit.commit_auth` chuyển cả hai vào `recovery.record_auth_endings`. Token đứng trước session trong batch; tất cả mirror false được INSERT/flush trước **một** append toàn batch + fsync. Chỉ sau thành công mới xác nhận **toàn batch**. User không có session vẫn journal. Không biến helper thành framework tổng quát.

| Thao tác | Cùng một transaction DB live |
| --- | --- |
| Request | Thu hồi token cũ (`reset_replaced`) + tạo token mới + audit |
| Confirm | Consume token (`password_reset`) + đổi verifier + thu hồi token khác (`reset_completed`) + session + audit |
| Disable | Đổi status + thu hồi token (`user_disabled`) + session + audit |

Partial unique hiện chỉ cho một token mở/User. Nhánh thu hồi các token khác sau confirm thường rỗng; vẫn xử lý mọi hàng mở theo UUID. Test `reset_completed` dùng helper dưới đúng khóa, không bỏ partial unique để tạo trạng thái không hợp lệ. Đây là giới hạn test; không bịa đường UI tạo nhiều token mở.

## Cửa sổ lỗi

| Cửa sổ | Batch chỉ token | Batch token + session |
| --- | --- | --- |
| Append/fsync + commit trả về thành công | Token đóng, phần auth/audit commit; kết quả xác định | Cả token/session và phần auth/audit commit nguyên tử |
| Append không ghi/torn/prefix hoặc dòng đủ nhưng fsync lỗi; DB commit | Auth commit, mọi mirror false; confirm/disable 503 `auth_journal_unavailable`, gate đóng | Cả batch commit nguyên tử, mọi mirror false; cùng 503/gate đóng |
| Journal bền nhưng commit rollback/mất phản hồi | Replay closure, **trả lỗi**; không suy verifier/status/token mới/link thành công | Replay token rồi session theo seq; **trả lỗi**, không suy reset/disable thành công |
| Prefix/torn + DB rollback/process chết | Recover fsync/replay chỉ dòng hoàn tất; không suy phần chưa ghi | Có thể chỉ token trong prefix được áp; session chưa ghi còn sống. Chưa báo thành công, thực hiện lại phần còn lại |
| Không journal, DB rollback | Không đóng, không thành công, không delivery | Không đóng token/session, không báo thành công |
| Replay tiếp tục lỗi | `auth_closure_pending_recovery`, gate đóng | Cùng mã, không tuyên bố session đã thu hồi khi chưa biết outcome |

DB live có toàn batch nhưng file thiếu đuôi: chỉ `recover --confirm-db-is-current` trên **DB live đã xác minh** sửa từ mirror; không dùng trên snapshot restore để suy quyết định file chưa nhận. Dòng đủ nhưng fsync lỗi: recover tự fsync và xác nhận, không cần cờ. Fault injection không chứng minh độ bền mất điện.

Public request-reset giữ `202 {"status":"accepted"}` cho email có/không tài khoản, kể cả lỗi nội bộ nhánh có tài khoản. Chỉ xác nhận tiếp nhận, **không bảo đảm link đã phát**. Middleware vẫn trả 503 cho mọi request mới khi gate đóng. Không log exception/recipient/secret. Adapter chỉ nhận link sau commit trả về kết quả xác định; rollback/lỗi journal/mất phản hồi không delivery. Không tuyên bố chống timing enumeration.

## Replay, gate và nâng cấp

Token snapshot còn mở → đúng trạng thái/mã/time journal; đã đóng đúng → idempotent. Sai User hoặc trạng thái/mã/time xung đột → từ chối, gate không mở. Token/User vắng → `recorded_reset_token_absent`; không tạo token/User/link. Replay không đổi verifier/status hay tạo session. Recover kiểm hậu điều kiện **mọi sự kiện**, kể cả mirror đã có.

Gate/status thêm `reset_tokens_not_journaled`, `unjournaled_reset_tokens`; report thêm `reset_tokens_imported`. Token kết thúc trước migration còn trong DB phải journal trước phục vụ. Migration không I/O file hoặc tự backfill. `init` từ chối DB có **bất kỳ token**, không coi DB có lịch sử auth là cài mới. `register-existing` chỉ DB live và `recover` lấy đủ User → token → session trước journal. Recover replay file trước, rồi import token lịch sử bằng batch: không tạo gap seq khi file ahead. Register-existing INSERT/flush mọi mirror false trước create/fsync, rồi xác nhận cả batch. `hold` chỉ đọc seq/hash, chạy trên snapshot `0030` trước migrate. Giữ các gate common revocation/review closure/session.

**Trạng thái sau PR #30:** đã merge tại `ce67ce8` (`0031`); M6c-2b2b2 mở rộng riêng [invitation](invitation-journal.md), giữ các bảo vệ reset/session trên đây.

**Hiện hành:** PR #31 đã merge `3f21794` (`0032`); [C1 `0033`](c1-auth-safety-foundation.md) thêm reset fence/purpose, receipt/generation và backfill NULL bằng `auth_safety_backfill`, chỉ thử cô lập/pending. Writer/predicate reset mới thuộc C2; closure reset vẫn không chứng minh verifier mới hoặc enable/disable đã commit. Receipt trong transaction là bằng chứng riêng; replay không dựng nó.

[C2b reset/status](c2b-reset-status.md) chuyển normal reset/consume/disable/enable sang runtime routines, latest head/fence/receipt, clock và actor cuối trên source `0036` (nhánh review, chưa merge). Quarantine staging chỉ direct fixture; public delivery kiểm chứng và operator thuộc C3. Safety pending tới C4.

## Giới hạn và việc kế tiếp

- Tại PR #30 invitation chưa bảo vệ (M6c-2b2b2 mở rộng sau đó); membership/xóa paper riêng là task sau.
- Mật khẩu/account status vẫn rollback; credential cũ có thể tạo **session mới**. Auth sau restore chưa an toàn hoàn toàn.
- Journal cùng mất/rollback với DB không phát hiện; cần bản độc lập, hiện hành. Lịch sử mất không backfill; backup cũ chỉ được bảo vệ bởi sự kiện journal đã nhận.
- Lỗi chỉ tiêm, chưa mất điện thật; diễn tập một máy không chứng minh độ bền production hay SLO/RTO.
- Nơi lưu journal production, scheduler/retention/mã hóa và vận hành backup chưa chốt với chủ dự án. Không provider/production, không đổi `.env`/`pricing_verified`.

**Task kế tiếp chủ dự án đã nêu:** journal invitation; sau đó thiết kế chống rollback credential/account status. Storage/epoch/khôi phục credential cần chủ dự án xác nhận trước coding phụ thuộc.
