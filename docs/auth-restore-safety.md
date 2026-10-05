# Chống rollback credential và trạng thái tài khoản (M6c-2b2c0)

**PR #38 đã merge `d06664e` (C2d-1, source `0039`).** [Helper ba route đọc ngắn](c2d1-final-caller-reads.md) · [Inventory](c2d1-test-inventory.json). Task tiếp [C2d-2a — 13 GET JSON library/collections/discovery](tasks/m6c2b2c2d2a-resource-json-reads.md) đã triển khai trên nhánh review, xem [bàn giao](c2d2a-resource-json-reads.md); [phạm vi đã chốt](decisions.md#giao-c2d-2a-sau-merge-pr-38-2026-10-05). Review/publication metadata C2d-2b và writer/file/provider còn riêng; C3/C4 chưa triển khai, safety pending tới C4. Bootstrap không dùng để rescue/enable hoặc tạo Admin thứ hai. C2c-2 accepted closure replay chỉ đóng link, không dựng User/receipt/accepted FK; prefix chưa có closure không chứng minh link đã đóng.

[Mục lục](README.md) · [Quyết định](decisions.md#m6c-2b2c0-thiết-kế-auth-sau-restore-2026-10-03) · [Task coding](#chia-pr-coding) · [Ma trận kiểm thử](#ma-trận-kiểm-thử)

**Trạng thái: PR #33 đã merge `c98bd33` (C1 `0034`), PR #34 đã merge `78e0880` (C2a `0035`), PR #35 đã merge `1d3e558`, chứa `f2ba336` (C2b `0036`). Safety vẫn pending tới C4, chỉ thử cô lập.** [Quyết định nền được ủy quyền](decisions.md#quyết-định-nền-c1-theo-ủy-quyền-2026-10-03) chọn A, baseline legacy có kiểm chứng, C1–C3 pending và tách quyền DB với trusted identity rehash. [Schema/quyền/replay/kiểm chứng C1 và bàn giao C2](c1-auth-safety-foundation.md) phân biệt phần đã triển khai với thiết kế C2–C4 dưới đây. [Origin và cách chia C2](decisions.md#chia-c2-và-chốt-origin-theo-ủy-quyền-2026-10-03) đã chốt; [C2a](c2a-login-session-predicates.md) và [C2b](c2b-reset-status.md) đã merge. [C2c được chia hai PR](decisions.md#chia-c2c-sau-merge-pr-35-2026-10-04), bắt đầu [C2c-1 bootstrap/initial receipt](tasks/m6c2b2c2c1-bootstrap-initial-receipts.md); invitation lifecycle C2c-2 và C2d còn mở. Operator/delivery/status recovery và cutover production còn mở theo milestone tương ứng; chưa CLI recovery User/Admin hoặc mở traffic. Khảo sát thiết kế gốc trên `3f2179487b1e7041af2b398f54c6ad4406510f96` (`0032`), base coding chứa PR #32 là `a5c75b88da72eaf3d8a28c0c00d82d12099f28a4`. PR #32 chỉ tài liệu; migration/kiểm trực tiếp thuộc PR C1.

**Baseline C2 trước task C2c-1:** PR #34/#35 đã merge (C2a `0035`, C2b `0036`, gồm các sửa P2). [Task C2c-1](tasks/m6c2b2c2c1-bootstrap-initial-receipts.md) khi đó chưa triển khai; safety vẫn pending tới C4, chưa mở delivery/operator C3 hoặc final callers C2d.

Mục lục thiết kế:

- [Hành vi và yêu cầu](#nguồn-yêu-cầu-và-hành-vi-hiện-có)
- [So sánh A/B/C](#so-sánh-và-đề-xuất-lựa-chọn)
- [Schema, event và trạng thái](#mô-hình-dữ-liệu-đề-xuất)
- [Luồng và tám tình huống](#quy-tắc-từng-luồng-và-tám-tình-huống)
- [Transaction, khóa và guard](#transaction-khóa-và-guard)
- [Cửa sổ lỗi](#cửa-sổ-lỗi-và-phản-hồi)
- [Recovery và backfill](#recovery-nâng-cấp-và-khả-năng-phục-vụ)
- [Khôi phục User/Admin](#khôi-phục-user-và-admin)
- [Ma trận kiểm thử](#ma-trận-kiểm-thử)
- [PR và prompt coding](#chia-pr-coding)
- [Quyết định mở và giới hạn](#quyết-định-cần-chủ-dự-án-chốt-và-giới-hạn)

## Nguồn yêu cầu và hành vi hiện có

**Đã xác nhận:** auth nội bộ invite-only, bootstrap Admin một lần bằng mật khẩu nhập an toàn, session/CSRF/ACL mỗi request; không tài khoản mặc định. Chủ dự án yêu cầu PR này thiết kế chống rollback credential/status, so sánh phương án, nêu kiểm thử và chia task. Việc yêu cầu thiết kế không xác nhận một giải pháp kỹ thuật hay quyền operator mới. Nguồn: [yêu cầu sản phẩm](product-requirements.md), [quyền và dữ liệu](access-and-data.md), [decisions](decisions.md).

**Đã triển khai ở PR #29–#31:** journal độc lập, hash chain, mirror, batch append/fsync trước commit, recovery gate/backfill cho session revocation, reset-token closure và invitation closure. `journal_confirmed=true` chỉ chứng minh append/fsync trả thành công; **không chứng minh transaction đổi verifier/status/tạo User đã commit**. Replay không tái tạo credential/account/session. Các giới hạn chi tiết ở [reset token](reset-token-journal.md) và [invitation](invitation-journal.md).

| Code đã khảo sát | Hành vi tại base; khoảng trống cần thiết kế |
| --- | --- |
| `api/app/identity/bootstrap.py` | Advisory bootstrap + kiểm singleton/không User + tạo Admin/audit trong transaction; chưa có credential/status event |
| `identity/invitations.py` | Accept băm ngoài khóa, email advisory → invitation, kiểm lại TTL bằng clock sau hash; INSERT User role user, closure/audit rồi `commit_auth`; không tự login |
| `identity/login.py` | Verify và có thể rehash ngoài khóa; khóa User A/B theo UUID NO KEY UPDATE, kiểm lại verifier/status, issue session + replace session + audit qua `commit_auth`; rehash chỉ thay verifier/updated_at, không đổi `password_changed_at` |
| `identity/password_reset.py` | Request chỉ active; confirm băm ngoài khóa, User → token → session; verifier/consume/revoke/audit cùng commit. Confirm hiện dùng `now` đầu request ở cả hai lần kiểm TTL, cần clock mới sau hash/chờ khóa khi coding |
| `identity/users.py` | Disable cập nhật status và đóng token/session; enable cập nhật status/audit. User không token/session thì batch journal rỗng, kể cả disable; cấm Admin tự đổi status |
| `identity/sessions.py`, `session_commit.py` | Resolve/touch và `session_is_live` yêu cầu active + TTL + không revoked; chưa generation/quarantine. Commit lỗi trả 503 và replay closure, không bảo đảm phần auth khác |
| `api/deps.py`, `api/csrf.py`, routes `auth.py`/`admin.py` | Dependency principal/session và CSRF bound; double-submit mọi POST, Origin hiện bị từ chối nếu có nhưng không trusted. `require_trusted_origin` có sẵn, auth route chưa đòi Origin bắt buộc. Outbox chỉ Admin, chỉ development; delivery disabled vẫn public reset 202 tiếp nhận, không phát link |
| `journal.py`, `recovery.py`, `api/gate.py` | Chuỗi seq/hash và schema body allowlist, advisory journal sau khóa hàng; toàn hệ thống 503 trừ health/ready khi hold/ahead/unconfirmed/lỗi. Recover replay → import closure → cleanup → hậu điều kiện → end; `hold` chỉ đọc seq/hash mirror để chạy trước migrate |
| `common_original.py`, `scripts/ras_backup.py`, `app/backup.py` | Original chung kiểm cuối bằng `session_is_live`; backup snapshot khi writer dừng, journal ngoài artifact, restore kiểm ownership project trước mutation, hold trước migrate, không tự start writer |

Mâu thuẫn tài liệu trạng thái: các đầu trang vẫn ghi PR #31 chưa merge; PR này cập nhật thành đã merge `3f21794`. Không có bảo đảm credential/status restore trong code để kế thừa; bảo vệ token/session không phải bằng chứng bảo vệ login mới. Các số test/drill cũ trong docs là chứng cứ PR trước, không là kết quả M6c-2b2c0.

## So sánh và đề xuất lựa chọn

| Tiêu chí | A — generation UUID/User + journal + cách ly | B — khôi phục credential mọi account mỗi restore | C — auth có thẩm quyền ở hệ thống độc lập |
| --- | --- | --- | --- |
| Mật khẩu cũ tạo session mới | Chặn khi generation/receipt không khớp quyết định mới nhất; không cần giữ verifier ngoài DB | Chặn nếu tất cả login/session đóng tới đặt mật khẩu mới | Có thể chặn qua authority hiện hành, nếu authority không cùng rollback |
| Disable/enable | Journal disable luôn hạn chế; enable chỉ dùng với receipt DB đúng, replay không tự cấp quyền | Reset toàn bộ **không đủ**: vẫn phải có journal/authority status | Authority quyết định status, cần consistency/availability rõ |
| User không rollback | Giữ credential và session đúng generation, vẫn áp closure/TTL | Mất truy cập cả User không thay đổi, tốn hỗ trợ mỗi restore | Ít gián đoạn nếu authority sống; mất dịch vụ khi dependency lỗi |
| Admin | CLI operator kiểm chứng đúng UUID đặt credential mới, không bootstrap lại | Cũng cần đường ngoài outbox cho mọi Admin | Cần recovery authority riêng, có thể mất cả quản trị authority |
| Nguyên tử/lỗi | DB + journal không atomic; dùng quyết định hạn chế và receipt DB, chấp nhận cách ly dù rollback | Ít đối chiếu credential, nhưng reset/status vẫn có cửa sổ commit/journal | Hai hệ thống/transaction phân tán, cấp session phải đối chiếu authority |
| Migration/backfill | Quarantine legacy chưa chứng minh; không bịa lịch sử; receipt và guard mới | Quarantine mọi account, cần đường khôi phục trước mở traffic | Đồng bộ UUID/role/status, chuyển nguồn chân lý và backfill khó |
| Vận hành/chi phí | Thêm schema, recovery CLI, hỗ trợ account bị ảnh hưởng; giữ storage journal hiện tại, phải độc lập thật | Hỗ trợ cả quần thể sau mọi restore, nguy cơ mất Admin cao | Dịch vụ/SSO/secret/monitoring/chi phí ngoài repo; chưa chốt nhà cung cấp |

**Đã chọn A theo ủy quyền cho C1:** generation UUID độc lập cho credential và status, journal ghi quyết định trước commit; generation credential bị rollback được cách ly và phải đặt mật khẩu mới qua kiểm chứng. B là biện pháp dự phòng thủ công khi không thể chứng minh baseline, không là chính sách mọi restore. C chưa phù hợp MVP nội bộ và phạm vi repo. Đánh đổi A: enable đã commit sau snapshot có thể phải được cho phép lại bằng quyết định mới; một reset rollback nhưng đã journal vẫn khiến credential cũ bị cách ly. Đây là gián đoạn có chủ ý để tránh mở quyền khi outcome chưa rõ.

Không đặt verifier, mật khẩu, hash/fingerprint dẫn xuất từ verifier, token/link, email hoặc payload delivery vào journal. SHA-256 của envelope journal chỉ bảo vệ chuỗi metadata, không là fingerprint credential. Timestamps chỉ phục vụ audit; thứ tự quyết định lấy từ **seq của journal độc lập đã xác minh**, không timestamp/counter trong DB restore.

## Mô hình dữ liệu đề xuất

### Schema và bằng chứng

| Đối tượng đề xuất | Trường/quan hệ | Ràng buộc |
| --- | --- | --- |
| `users` | `credential_generation UUID NULL` (chỉ legacy chưa chứng minh), `credential_state usable/quarantined`, `credential_reason_code`; `status_generation UUID NULL`, `status_safety proven/held`, `status_reason_code`; `password_hash_policy_version` DB-only cho PHC/rehash; giữ `status active/disabled`, verifier và role hiện tại | Không default usable/proven; legacy NULL phải quarantined/held. User UUID bất biến. Quarantine không đổi verifier, role, status hoặc tự gắn generation mới |
| `auth_change_receipts` | `operation_id PK UUID`, `user_id FK`, `domain credential/status`, `generation UUID`, `decision`, `reason_code`, `recorded_at`; unique `(domain,generation)` và khóa duy nhất đủ cho FK ghép | Receipt INSERT **cùng transaction** verifier/generation hoặc status/generation; append-only. Không secret; replay **không** tạo receipt. Có receipt ở snapshot chỉ chứng minh transaction đó commit vào snapshot, không rằng nó mới nhất |
| Con trỏ User | `credential_operation_id`, `status_operation_id`; FK ghép tới receipt đúng `(operation_id,user_id,domain,generation)` khi operation không NULL | usable/proven active bắt buộc đầy đủ; NULL chỉ legacy/cách ly/held hoặc **proven disabled do replay không receipt**. Receipt cũ không được dùng cho generation mới/User khác. Disabled replay đặt status operation NULL cùng generation journal; không bao giờ là bằng chứng active |
| `auth_safety_heads` | `user_id UUID PK`, latest credential/status event seq + operation/generation/decision; `projection_head_seq` | Projection DB của journal, **không FK User** (User có thể vắng). Rebuild từ journal và kiểm hậu điều kiện; không là nguồn độc lập; không xóa head để cho User dùng mật khẩu cũ |
| `revocation_journal_records` | Thêm typed cột auth user/op/domain/generation/decision/reason/time khớp body | Không FK User; unique op và generation trong event auth; NULL ở cột không thuộc kind, các cột kind auth không NULL. Append-only; confirm-only phải so mọi cột mới |
| `sessions` | `credential_generation UUID` ghim khi issue | Bất biến, trùng credential usable của đúng User; legacy NULL bị chặn. Không dùng `password_changed_at` để cấp quyền |
| `password_reset_tokens` | `credential_fence_operation_id` + `recovery_purpose normal/quarantine` | Token mới ghim latest credential operation đã đối chiếu; legacy không fence bị đóng trước mở traffic. Token/hash/link vẫn chỉ ở DB/delivery, không journal |
| Đăng ký recovery | `auth_safety_version`, mốc hoàn tất backfill/projection | Thiếu/phiên bản không tương thích → gate đóng; không gán ready chỉ bằng config. Migration không I/O journal |

Receipt là bằng chứng DB commit có điều kiện, **không phải commit acknowledgment ngoài DB**. Không thêm event hậu commit tự mở quyền: nếu DB transaction không còn trong snapshot thì receipt mất, replay không dựng lại. Với credential, chỉ generation và receipt chưa đủ chống người có quyền sửa cả verifier và receipt; threat model là restore/crash và lỗi writer, không DBA/host độc hại. Guard/privilege bên dưới ngăn ghi ngoài hợp đồng của role ứng dụng; journal hash chain không có chữ ký không bảo vệ đối thủ viết lại toàn bộ journal.

### Event và generation

Hai kind đề xuất, body **đúng** các trường sau, không thiếu/thừa:

| Kind | Body và giá trị |
| --- | --- |
| `credential_transition` | `user_id`, `operation_id`, `generation` (UUID chuẩn mới), `reason_code` ∈ `bootstrap`, `invitation_accept`, `password_reset`, `verified_recovery`; `recorded_at` có timezone. Ý nghĩa: credential trước event này không còn đủ chứng cứ; **không** mang nghĩa mật khẩu mới đã commit |
| `account_status_decision` | `user_id`, `operation_id`, `generation`, `decision active/disabled`, `reason_code`: active ∈ `bootstrap`, `invitation_accept`, `verified_initial_status`, `admin_enable`; disabled ∈ `admin_disable`, `legacy_disabled`; `recorded_at` có timezone. Active là ý định cho phép có điều kiện receipt, disabled là quyết định hạn chế |

Mỗi operation một domain, UUIDv4 sinh bằng CSPRNG OS ngoài dữ liệu snapshot; operation/generation không lấy `gen+1`, timestamp, email hay UUID sinh tất định từ DB. Dưới journal advisory kiểm toàn chain/head hiện hành: generation/op chưa xuất hiện ở **bất kỳ User/domain** mới được cấp. Collision → từ chối và thử UUID khác trước append. Seq mới = head độc lập + 1. Không tái dùng UUID đã ghi dù transaction rollback; retry quyết định mới dùng UUID mới, chỉ retry truyền tải/replay record cũ giữ seq/hash/op. DB unique là lớp thứ hai, không đủ khi restore.

`auth_safety_heads` lấy **event cuối của từng domain theo seq**. Event cũ không tiếp tục buộc User phải giữ generation cũ sau một thay đổi mới hợp lệ. Kiểm mọi event về shape/identity/chuỗi, rồi kiểm trạng thái account theo fold cuối; không so credential hiện tại bằng tất cả generation lịch sử. Snapshot có generation/receipt không thuộc chain đúng User hoặc không khớp nhau là corruption → gate toàn hệ thống đóng; generation cũ có receipt hợp lệ nhưng journal mới hơn là rollback có thể xử lý riêng account.

Replay auth có hai pha dưới hold: chèn/kiểm mirror và fold head theo seq, **chưa sửa verifier/status/con trỏ receipt hiện có**; khi đã đọc hết chain mới đối chiếu từng User với head cuối và áp hạn chế. Không replay disable d1 vào snapshot đã commit enable a2 rồi dùng status bị sửa đó để kết luận receipt a2 không hợp lệ. Snapshot sau enable đúng receipt/status/generation phải giữ active. Replay khẩn cấp sau commit lỗi cũng giữ gate đóng tới fold đầy đủ và hậu kiểm; không áp từng event cấp quyền ngay trong vòng replay. Closure/common vẫn áp như hợp đồng cũ. Quarantine chỉ được giải phóng bởi writer credential mới có receipt; replay không tự giải phóng quarantine đã được ghi, dù thấy receipt lịch sử.

### Trạng thái và quyền

Đặt `effective_active = status=active AND status_safety=proven AND status generation/receipt/head khớp`; `credential_usable` tương tự credential_state/generation/receipt/head. Cả hai là điều kiện login/session, không thay cho ACL tài liệu.

| Business status / safety / credential | Login và session | Reset/khôi phục |
| --- | --- | --- |
| active / proven / usable | Được khi đúng mật khẩu, generation session, TTL, closure và recovery gate | Reset thường bằng token mới một lần; đổi credential không đổi status |
| active / proven / quarantined | Login 401 chung; mọi cookie/session 401, kể cả đúng mật khẩu snapshot | Reset mới ghim fence + kiểm chứng delivery, hoặc operator; không yêu cầu biết mật khẩu cũ |
| active / held / bất kỳ | Hiệu lực bị khóa; login/session 401 | Public request 202 không hứa link; không cấp reset web. Operator có thể đặt credential, **không** làm status proven/active |
| disabled / proven hoặc held / bất kỳ | Không login/session | Reset web không cấp/confirm; operator chỉ đặt credential sau kiểm chứng nếu cần, giữ disabled/held. Enable là quyết định khác |
| User vắng | 401 chung, không account | Không tạo lại User/verifier/role/session/token. Ghi projection/mirror absent; khôi phục account/data là task riêng |

Replay disable đặt business status disabled và projection generation mới nhất, safety proven cho **hạn chế**, chặn mọi session dù không có session để journal. Replay active chỉ giữ active nếu receipt đúng event cuối **đã có trước replay** cùng status/generation; nếu thiếu/lệch do rollback thì giữ business status snapshot nhưng `status_safety=held`. Không ghi active từ journal, không dùng journal_confirmed để mở account. Snapshot disabled + event enable không receipt vẫn disabled/held. UI phải phân biệt “disabled” và “status cần xác minh”; held không được trình bày là Admin đã disable trong DB live.

Replay credential chỉ giữ usable khi receipt đúng event cuối + User đúng generation; không khớp → quarantined, để nguyên verifier/generation cũ. Chọn ghim generation vào session: ngăn session snapshot/SQL lỗi hoặc race dùng credential trước reset, và bảo vệ dù thiếu một closure prefix; vẫn giữ journal session/TTL/revocation như trước. Rehash giữ generation nên không làm mất session chỉ vì nâng tham số.

## Quy tắc từng luồng và tám tình huống

| Luồng | Thay đổi đề xuất |
| --- | --- |
| Bootstrap | Chỉ cài mới, vẫn singleton/không User; tạo credential/status event + receipt + User + bootstrap_state + audit cùng batch/transaction. Refuse nếu journal có lịch sử bootstrap dù DB rollback mất singleton; không bootstrap lại để cứu Admin. Journal không khôi phục bootstrap User vắng |
| Accept invitation | Tạo UUID User trước flush; credential/status event cùng accepted closure/User/audit, không tự session. Event accepted không chứng minh account tạo thành công. Invite role user giữ nguyên |
| Reset thường/cách ly | Token mới đúng UUID/fence/TTL; confirm tạo generation/receipt mới + verifier + consume/revoke token/session + audit. Credential quarantine hết khi commit xác định và projection khớp; status không đổi. Không tự login |
| Rehash | Verify thành công trên generation usable, compare-and-swap verifier dưới khóa; đổi PHC/updated_at, **không** credential event/generation/password_changed_at, không coi đổi password. DB routine riêng và hash policy version DB-only để kiểm tham số; không hash verifier vào journal |
| Disable | Event disabled ngay cả khi không token/session; status + receipt + đóng token/session + audit cùng transaction. Replay quyết định hạn chế dù DB rollback |
| Enable | Event active + receipt + status/audit cùng transaction; kiểm lại Admin actor dưới khóa. Không làm credential usable, không phục hồi session/token cũ. Replay thiếu receipt → held; cần quyết định enable mới từ Admin đang có quyền |

| Tình huống bắt buộc | Kết quả theo A đề xuất |
| --- | --- |
| 1. Backup g1 → reset g2 → restore | Journal g2, DB chỉ g1/receipt1: quarantine, mật khẩu cũ không tạo session mới; không có g2 verifier để replay. Cookie/token đóng bởi các journal hiện có; lấy credential g3 bằng kiểm chứng |
| 2. Backup active → disable, không session → restore | Status event disabled vẫn có; replay khóa User độc lập session; active snapshot không có hiệu lực |
| 3. Disable d1 → enable a2 → restore nhiều mốc | Trước disable hoặc giữa d1/a2: latest a2 nhưng thiếu receipt2 → held, không tự active. Snapshot sau enable đúng receipt2: active nếu credential usable; disable d3 sau đó thắng tất cả snapshot cũ. Không suy thứ tự từ updated_at |
| 4. Event vào journal, DB rollback/mất phản hồi | Credential event cách ly generation cũ; disable khóa; enable không receipt không mở. Mất phản hồi nhưng DB thật có receipt đúng có thể đối chiếu lại để xác định trạng thái; request gốc vẫn 503, không tự phát cookie/delivery |
| 5. Mọi Admin snapshot cần credential recovery | Recover có thể mở gate với Admin quarantined, mọi route Admin/outbox vẫn chặn. Operator được ủy quyền khôi phục đúng UUID bằng CLI đề xuất; không bootstrap/tạo Admin/đổi role |
| 6. User tạo sau backup | Event/head/mirror được giữ không FK; absent không là corruption. Không tái tạo account; invitation cũ vẫn closure. Email tái dùng không chuyển quyết định User cũ sang User UUID mới |
| 7. Restore/recover lặp | Fold chain quyết định cuối, quarantine/held lại idempotent; không receipt mới từ replay. Nếu đã recovery g3 rồi lại restore g1, journal g3 lại quarantine g1; phải recovery g4, không tự gán g3 cho verifier cũ |
| 8. DB hiện có/snapshot trước migration | Migrate schema với mặc định đóng; legacy không generation/receipt không được baseline usable từ snapshot. Backfill/replay theo quy trình dưới đây; giữ hold chạy trước migration |

## Transaction, khóa và guard

### Transaction và thứ tự

Mọi thay đổi credential/status, receipt, projection live, closure token/session/invitation và audit liên quan phải cùng transaction DB, cùng **một batch append/fsync**. Với bootstrap vẫn gộp bootstrap_state. Không delivery/cookie/thành công trước commit trả về xác định **và** batch confirmed **và** kiểm gate/account cuối đạt. `commit_auth` được mở rộng cụ thể cho hai kind này; không viết commit riêng trong route. Mirror toàn batch false + flush/validate trước byte đầu; chỉ cả batch true sau fsync. Trên lỗi append vẫn commit thay đổi với batch false như hợp đồng hiện có, trả 503/gate đóng; replay xử lý quyền bằng chứng, không tái tạo payload.

Batch ghi event credential/status hạn chế **trước** closure mới để prefix hoàn tất đã chặn login/session qua account fence; active event không tự mở quyền. Không đổi thứ tự các closure cũ tương đối với nhau (invitation → reset → session); số seq cấp liên tục từ head. Nếu prefix không tới event account, không tuyên bố đã hạn chế account; bảng lỗi nêu giới hạn.

| Nhánh | Thứ tự khóa trước journal advisory |
| --- | --- |
| Login/replace, reset, disable/enable, operator credential | User actor/target/previous-owner NO KEY UPDATE theo UUID tăng dần → reset token UUID → session UUID → flush receipt/audit/FK → journal |
| Rehash | Cùng khóa User như login, CAS verifier/generation/state; issue/replace sessions theo thứ tự trên |
| Accept/create/revoke invitation | Advisory email chuẩn hóa → invitation UUID; User mới INSERT/flush receipt/audit trước journal. Không thêm khóa User actor hiện hữu sau khóa invitation/journal; nếu cần kiểm actor đang sống dùng kiểm đọc ở transaction và kiểm cuối, không chèn chu trình User/email |
| Bootstrap mới | Advisory bootstrap → INSERT User/receipt/singleton/audit → journal |
| Recovery/backfill | Dừng mọi writer; recovery mutex như hiện có. Xử lý từng event theo seq với khóa đúng domain; auth User → token → session → journal, invitation email → invitation → journal; không giữ journal advisory khi đi lấy khóa User/item ở record tiếp theo |

Không lấy thêm khóa hàng auth sau journal advisory. Gate đọc shared journal có thể chạy trước khóa hàng ở transaction chỉ đọc không mutation; final check writer dùng head/gate dưới **journal lock cuối**, không giữ shared lock rồi lấy User. Projection cập nhật qua hàng User đã khóa; đối tượng absent chỉ insert/upsert projection không FK trước journal. Khóa nhiều User gồm cả Admin actor để disable actor cạnh tranh không cấp quyền sau disable. Việc accept kiểm actor không áp cho người nhận anonymous; create/revoke của Admin phải recheck session và effective_active ngay trước quyết định/delivery, như các route ghi khác.

Login băm ngoài khóa phải ghi nhận `(user UUID, verifier, credential generation/op, credential state, status generation/op/safety)`, đóng transaction đọc, rồi khóa Users thứ tự cố định và tải lại với populate_existing. Kiểm tất cả bộ trên, head độc lập, recovery gate và đồng hồ **mới** sau hash/chờ khóa; mismatch → từ chối/retry bounded, không dùng verifier đã xác thực để gắn generation mới. Session expiry của previous cookie và TTL/token đều dùng clock mới. Hold/recovery thắng trước final check → không issue cookie; login thắng trước reset/disable → session mới bị thu hồi/bị fence trong transaction tiếp theo. Recovery thực chạy writer dừng; test race hold vẫn phải bắt request đang hash/request đã qua middleware.

Các dependency `resolve_session`, `session_is_live`, `get_session_context`, auth `/csrf`, Admin, file/chunk/citation/search/RAG/collection dùng cùng predicate effective_active + credential_usable + session generation. Thêm lần kiểm cuối cho công việc chậm, trước provider và response/byte đầu; không chỉ kiểm middleware đầu request. Giữ khóa tài nguyên item/request hiện có, dùng kiểm đọc session/account/gate dưới journal shared cuối; không thêm khóa User sau khóa item. Session touch hiện không khóa User sau session; giữ nguyên thứ tự, bổ sung predicate. RAG chưa streaming: final response check; streaming sau này phải kiểm các điểm phát. Bytes đã qua kiểm cuối trước thu hồi không thể lấy lại.

### DB và parser độc lập

Đề xuất migration mới sau `0032`, không ấn định số revision trước review, không sửa revision đã merge. BEFORE/CHECK kiểm kind, đúng bộ field, UUID chuẩn, JSON type/time, missing/NULL/extra, reason/decision; mirror/body dùng `IS NOT DISTINCT FROM` kèm NOT NULL cho kind auth. Kiểm User/generation/op/receipt ghép trước append; không dùng constraint SQL ba giá trị có thể nhận UNKNOWN. Event User absent hợp lệ nhưng mọi ID mirror/body vẫn khớp.

Deferred trigger buộc INSERT User và mọi đổi verifier/generation/status/con trỏ/quarantine release phải có receipt + mirror/projection đúng User/domain/op/generation/reason/time trong transaction. Disable replay không receipt chỉ qua routine recovery hạn chế; enable/usable **không** được dùng routine này. Trigger giữ User ID bất biến, receipt/head append/monotonic, cấm DELETE/reinsert User UUID có auth history để vượt fence, cấm generation lùi/tái dùng/cross-user và downgrade khi đăng ký/có record. Head cập nhật mới phải có mirror seq/hash đúng; không cấp quyền từ con trỏ NULL/default. Restore/fixture dùng role migration/operator trong hold và gate vẫn đóng; app role không có DDL/owner/BYPASSRLS/tắt trigger.

Để “rehash” không trở thành cờ SQL bỏ qua guard: tách routine CAS rehash và routine đổi credential, không tin custom GUC `is_rehash=true` hay caller-supplied reason. Thu hồi quyền UPDATE trực tiếp các cột auth/receipt/head của role API, chỉ cho EXECUTE các routine cố định, SECURITY DEFINER với search_path cố định/quyền tối thiểu; thu hồi EXECUTE từ PUBLIC. Routine rehash chỉ đổi verifier khi old verifier/generation/op và trạng thái usable còn đúng, không đổi credential generation/password_changed_at, kiểm PHC/policy. **DB không chứng minh hai PHC biểu diễn cùng mật khẩu**: verify/rehash cùng input là trách nhiệm trusted identity code; SQL caller có quyền thực thi routine vẫn là trust boundary cần review. Không hứa chống DBA hoặc service bị chiếm quyền. Test raw UPDATE/INSERT receipt của role runtime phải bị từ chối; test routine credential thiếu mirror cũng phải bị trigger chặn. Tách runtime/migrator trong config/Compose cô lập và ranh giới trusted identity đã được orchestrator chốt theo ủy quyền cho C1; chuyển writer verify/login thuộc C2.

## Cửa sổ lỗi và phản hồi

Các mã auth dưới đây là đề xuất; public reset-request giữ 202 tiếp nhận với body chung khi middleware cho qua, không suy delivery từ 202. Middleware gate đóng vẫn 503. “Gate mở sau recover” chỉ khi chain/schema/hậu điều kiện và các bảo vệ cũ đều đạt.

| Cửa sổ | DB và journal | Gate / account | Phản hồi và xử lý |
| --- | --- | --- | --- |
| Trước append: validate/flush/lock lỗi | DB rollback, journal không quyết định | Trạng thái cũ còn hiệu lực nếu gate vốn mở | 503 `auth_unavailable`, không cookie/link/thành công; retry sau kiểm lại |
| Append chưa ghi/torn/prefix; DB commit | Toàn thay đổi + receipt/mirror batch false ở DB; file thiếu đuôi hoặc torn | Gate đóng, chưa phục vụ dù DB có credential mới | 503 `auth_journal_unavailable`; repair từ mirror chỉ DB live đã xác minh; snapshot restore không hợp thức hóa thiếu quyết định |
| Append prefix/torn; DB rollback/process chết | DB cũ; journal chỉ prefix hoàn tất có thể sync | Gate đóng đến repair/replay; event credential prefix → quarantine, disable prefix → khóa, enable prefix → held; phần chưa ghi không chứng minh gì | 503 nếu còn phản hồi; recover sync prefix/bỏ torn theo hợp đồng, không suy cả batch; quyết định mới khi retry |
| Dòng đủ nhưng fsync lỗi; DB commit | Receipt/thay đổi commit; tất cả mirror false dù file đủ | Gate đóng `journal_unconfirmed` | 503; recover tự fsync rồi confirm toàn record/batch giữ lại; usable/active chỉ nếu receipt thật khớp |
| Dòng đủ nhưng fsync lỗi; DB rollback | DB cũ; file có thể còn đủ/prefix/không còn sau crash | Gate đóng khi thấy file ahead/torn; recover sync rồi fold phần hoàn tất | Không thành công; quarantine/disable/held theo evidence có thật |
| Append/fsync + DB commit trả thành công | Receipt/verifier hoặc status + closure + audit nguyên tử DB, mirror confirmed | Gate/account khớp; session issue theo predicate | Thành công sau kiểm cuối, delivery/cookie mới được gửi; reset không enable |
| Journal bền; DB rollback | Không receipt mới; quyết định còn journal | Gate đóng khi ahead; replay credential quarantine, disabled khóa, active held | 503 `auth_outcome_uncertain`; replay không báo reset/enable thành công, không delivery |
| Commit không rõ kết quả (connection mất/lost reply) | Có thể có hoặc không receipt; journal bền | Đóng gate tới đối chiếu mới; receipt đúng còn DB thì giữ trạng thái, thiếu thì hạn chế | Request gốc 503, không cookie/delivery; reconnect đọc outcome bằng op, không resubmit verifier từ journal. Retry reset token đã consume thì từ chối, operator có thể cấp quyết định mới |
| Replay/hậu điều kiện lỗi | DB có thể đã áp prefix, journal nguyên chain | Gate toàn hệ thống đóng, không coi prefix là ready | 503 `auth_safety_pending_recovery`; sửa nguyên nhân và recover lại theo seq |
| Journal và DB cùng không giữ quyết định | DB cũ + không có event/mirror hoặc cả hai rollback đồng bộ | Gate có thể không nhận biết; mật khẩu/status cũ có thể sống | Không từng bảo đảm thao tác thành công; cần quyết định lại qua kiểm chứng. Không tuyên bố thiết kế cứu được bằng seq/timestamp/hash chain |

`journal_confirmed` không là cờ commit của nghiệp vụ. Event enable hoặc bootstrap/invitation active trước commit chỉ là intent; receipt mất thì không hoạt động sau replay. Nếu fsync thành công nhưng commit rollback, việc khóa/cách ly vẫn được áp: lợi ích an toàn có thể gây từ chối dịch vụ ngoài ý định DB live. Không dùng một event “committed” do request gửi hậu commit để tự tái tạo quyền.

## Recovery, nâng cấp và khả năng phục vụ

1. Dừng `api/web/worker/beat` và các auth CLI writer; verify artifact/current journal và quyền project cô lập bằng công cụ hiện có. Journal ngoài artifact, bản nguồn không bị sửa. Chọn image từ commit coding đã được duyệt, không image cũ không hiểu kind mới.
2. Restore dump → **hold → migrate** như hiện có. `hold` vẫn chỉ seq/hash và registration cũ, không đọc receipt/cột mới để chạy được trên snapshot trước migration. Khi journal có kind mới, binary cũ parser từ chối/gate đóng; không downgrade để mở traffic.
3. Recover kiểm/fsync chain và replay **toàn seq thiếu**, kể cả common/review/session/reset/invitation; replay file ahead **trước** backfill để không tạo gap. Auth replay hai pha giữ nguyên trạng thái/receipt snapshot tới khi fold xong latest head rồi mới đối chiếu và hạn chế; record absent chỉ mirror/projection. Chạy lại vẫn kiểm hậu điều kiện cả record đã mirror; không chỉ bỏ qua “present”.
4. Backfill dữ liệu chưa có bằng chứng theo quy tắc dưới; đóng legacy reset token không fence và session không generation qua batch closure có journal. Cleanup common/object như trước. Không sửa quyền/role/paper khi xử lý auth.
5. Kiểm hậu điều kiện: từng User present hoặc usable + latest receipt khớp, hoặc quarantine đã bị mọi gate từ chối; status hoặc proven đúng latest, hoặc held bị từ chối. Latest disable phải effective disabled. NULL legacy phải đóng; receipt không được replay tạo. Projection phủ toàn head, không orphan binding sai UUID, không unconfirmed/gap, các hậu điều kiện cũ vẫn đạt.
6. Chỉ sau đó `recovery_end` + mirror + gate ready; start writer đúng image, smoke qua proxy web. Account quarantine/held **không buộc toàn hệ thống tiếp tục hold** nếu đã chứng minh chặn được; `/readyz` có thể ready dù mọi Admin cần recovery. UI công khai không lộ UUID/status qua login; operator report chỉ IDs/codes/counts.

Đóng toàn gate khi missing/corrupt/diverged/behind/torn/unconfirmed journal, hold/migration/projection/backfill chưa hoàn tất, ID/receipt mâu thuẫn cấu trúc, không thể kiểm chứng các predicate hoặc guard chưa rollout đủ. Credential rollback có thể nhận biết và đã quarantine, hay active intent thiếu receipt đã held, là kết quả recovery hợp lệ riêng account, không corruption toàn hệ thống. Operator CLI chỉ chạy khi chain/global hậu điều kiện đã sạch (có thể còn hold chủ động); không vượt lỗi journal để cứu Admin. Khi chạy trong hold chỉ được thêm quyết định recovery hạn chế/credential hợp lệ, recover kiểm lại trước mở traffic.

### Baseline và backfill

Baseline bảo thủ đã chốt cho C1: **không** gán generation usable cho verifier legacy, kể cả DB hiện có được gọi là live; migration đặt quarantine, giữ business status nhưng safety held. Credential mới phải được đặt bằng quy trình kiểm chứng. Legacy disabled còn trong DB **chỉ khi chưa có bất kỳ status event cho UUID đó trong chain** có thể journal `legacy_disabled` vì đó là hạn chế hiện kiểm được; không import snapshot disabled để ghi đè enable mới hơn đã có journal. Legacy active không chứng minh enable lịch sử, vẫn held đến quyết định `verified_initial_status` riêng bằng thẩm quyền được duyệt. Đây là chi phí một lần chuyển đổi A, không reset toàn bộ mọi restore như B; C1 thử trên môi trường cô lập, chưa nâng stack dev hiện có.

Backfill không tạo event password_reset/enable quá khứ từ timestamp/closure/audit; không import verifier hoặc rehash như credential mới. User snapshot cũ không có generation sau migrate vẫn quarantine/held. Nếu journal đã có baseline/status/credential cho UUID, replay chúng trước và không tạo `verified_initial_status` mới từ snapshot cũ. Một account legacy chưa bao giờ xuất hiện trong journal cũng không tự được coi live; cần kiểm chứng mới. `register-existing`/`--confirm-db-is-current` chỉ hỗ trợ registration/repair live journal hiện có, **không** tạo baseline credential/status cho phép.

Session legacy không generation và reset token legacy không fence được kết thúc bằng helper batch hiện có, với mã **đề xuất mới** `auth_safety_backfill` tương ứng revoked; parser/DB allowlist, enum session và reason reset phải thêm cùng PR C1, không gắn giả mã logout/password_reset cho lịch sử không biết. Account quarantine/held không cần tạo closure từng session để gate có hiệu lực; session generation cũ vẫn chặn. Khi User có token/session legacy nhưng User đã vắng, giữ quy tắc absent và FK/snapshot thực tế, không dựng User để ghi backfill. Giữ toàn batch false khi append/fsync lỗi như trước.

Trên DB đang phục vụ `0032`, kế hoạch cutover cần inventory User UUID/Admin và người có quyền phục hồi **trước** migrate; có maintenance window tới khi kiểm xong tất cả gate, nhưng không đòi mọi User reset xong mới mở hệ thống. Operator đặt credential Admin rồi quyết định initial status riêng nếu đúng quyền/bằng chứng; không tự enable disabled. Snapshot trước đăng ký journal/`0032` dùng chain độc lập cùng installation theo recovery hiện có; không tạo genesis mới để biến snapshot thành cài mới. Không tìm được journal độc lập đáng tin thì giữ hold, xử lý sự cố ngoài thiết kế, không hứa bảo đảm rollback.

Restore/recover lặp: cùng chain/latest op và receipt cho cùng kết quả, không thêm event credential/status khi chỉ replay. Backfill closure chỉ thêm khi thiếu, seq luôn head+1; absence không dựng User. Account recovery mới g3 ghi vào bản journal đang dùng cho môi trường phục vụ; nếu promote môi trường restore thành live cần chuyển quyền writer/journal độc lập theo runbook riêng, không cho source/target append đồng thời (PG advisory chỉ khóa trong một DB). Thử drill luôn viết bản sao, không tác động journal nguồn.

## Khôi phục User và Admin

### Thẩm quyền và chứng cứ đề xuất

MVP chưa có email adapter production. `delivery=disabled` không phát link; `dev_outbox` ở bộ nhớ và chỉ Admin hiện hành đọc được. Không coi việc User cung cấp email, biết mật khẩu cũ, giữ cookie/reset token snapshot hay có quyền đọc outbox là chứng cứ sở hữu account bị rollback.

Đề xuất chủ dự án chỉ định **operator recovery** (người thật được ủy quyền quản trị host/DB/journal) và quy trình kiểm chứng ngoài app trước incident: inventory liên kết installation/journal ID + User UUID với danh tính người nhận; bằng chứng nhân sự/chủ sở hữu được người có thẩm quyền xác minh trực tiếp qua kênh riêng. Hồ sơ recovery có UUID operator, UUID vụ việc/approval, User UUID, mã phương thức kiểm chứng; chi tiết danh tính/email/tài liệu giữ trong hồ sơ hạn chế ngoài journal/log/audit. Host access là điều kiện thực thi, **không tự đủ** để nhận diện người nhận hoặc cho enable. Cần chủ dự án chốt người có thẩm quyền, lưu hồ sơ và điều kiện approve; không giả định inventory độc lập đã có.

Không đối chiếu danh tính chỉ email. CLI chọn `--user-id <UUID>` + expected installation/journal ID; read-only inspect từ DB hiện tại cho operator đối chiếu với inventory/vụ việc, xác nhận UUID chính xác. User cùng email nhưng UUID mới không nhận credential/status của UUID cũ. User vắng → refuse, không create/FK/role/bootstrap. Nếu không kiểm chứng được UUID hoặc không có operator được ủy quyền, giữ account cách ly; hệ thống có thể phục vụ các account đủ bằng chứng.

### User có đường delivery kiểm chứng

Nếu có adapter/kênh đã được chủ dự án chốt và kiểm chứng đến đúng người nhận, User active/proven/quarantine có thể request reset mới; token bind User UUID + latest credential op/fence + purpose quarantine, hash chỉ DB, TTL dùng config hiện có. Confirm không cần session quarantined, vẫn double-submit + **trusted Origin bắt buộc** đề xuất cho confirm/request/login/accept (cần quyết định compatibility client); hash ngoài khóa, kiểm lại token/fence/TTL/account/gate bằng clock mới. Thay đổi latest event trong lúc token còn TTL → token không dùng được. Consume/revoke + generation/receipt + verifier + session closure + audit cùng batch/transaction, không tự enable/login.

Với dev outbox, Admin đang usable có thể đọc và chuyển link mới qua kênh riêng **sau** xác minh User; thao tác xem outbox không tự xác minh User. Không có Admin usable hoặc adapter disabled → dùng operator CLI; public reset 202 không hứa mail. Disabled/held không reset web; operator credential reset không thay status. Tất cả TTL, giới hạn nhập/attempt, lock timeout và chính sách audit/approval nằm trong config có test; không tạo lifetime hardcode trong route.

### CLI đề xuất cho User và Admin duy nhất

Tên **chưa triển khai/chưa kiểm chứng**: `python -m app.cli.auth_recovery inspect --user-id …`, `… set-credential --user-id … --case-id … --operator-id … --expected-journal-id …`. Không thêm vào lệnh đã kiểm chứng/AGENTS. Không cho `--password`, verifier, email để chọn target, `--enable`, `--role`, `--force` hoặc `--confirm-db-is-current` ở lệnh credential.

1. Operator xác minh ủy quyền và danh tính ngoài app, inventory đúng User UUID/installation, status và latest head qua inspect; trường hợp Admin duy nhất không cần session/outbox. Tất cả Admin quarantined vẫn dùng đường này cho từng UUID đã có, không tạo account thay thế.
2. Chạy trên host/project được duyệt với writer dừng nếu hold/recovery; thiết bị nhập do người nhận kiểm soát hoặc operator được ủy quyền tiếp nhận an toàn. Mật khẩu nhập hai lần qua TTY không echo/getpass; refuse nếu không TTY an toàn. Không password argv/env/file/log, không default/hardcode. DB-only verifier băm ngoài khóa; log lỗi chỉ ID/mã/type không exception có payload. Với credential quarantine, verify input mới trên verifier snapshot **ngoài khóa** để từ chối chọn lại mật khẩu đang bị cách ly; khóa và CAS/recheck verifier/generation sau đó, mismatch phải kiểm lại. Reset web quarantine cũng áp quy tắc này. Credential có thể trùng mật khẩu lịch sử đã mất khỏi DB nên không thể kiểm blacklist mọi mật khẩu cũ; yêu cầu người nhận chọn mới, không tuyên bố chứng minh khác toàn lịch sử.
3. Khóa User → reset tokens → sessions → flush receipt/audit → journal. Recheck UUID/role/status/head/case và thẩm quyền; không đổi role/business status/status_safety. Với Admin bootstrap, đối chiếu thêm `bootstrap_state.admin_user_id` với inventory độc lập; singleton snapshot chỉ là chứng cứ bổ sung, không tự ủy quyền recovery. Cấp generation/op mới đã kiểm unique toàn chain, verified_recovery event + verifier + credential usable/receipt + revoke tokens/sessions + audit (operator UUID/case UUID/method code) cùng transaction/batch. User disabled vẫn disabled, held vẫn held.
4. Thành công chỉ khi commit xác định, batch fsync và đọc lại predicate; trả UUID/op/generation/mã không secret. Không phát session/link. Outcome chưa rõ → exit lỗi/503 tương đương, inspect/recover để đối chiếu receipt theo op; không khẳng định mật khẩu đã đổi. Nếu rollback, event đã bền vẫn quarantine; lần đặt mới có generation mới và nhập lại, không lưu password để auto retry.
5. Admin active/proven sau credential recovery có thể login bằng mật khẩu vừa nhập rồi giúp User khác. Nếu mọi Admin disabled hoặc held, đặt credential không đủ: giữ khóa. Cần quyết định status riêng từ thẩm quyền chủ dự án đã chốt; không dùng bootstrap hay cờ xác nhận DB.

Đề xuất lệnh status riêng `… resolve-initial-status` chỉ cho legacy/held chưa có latest active receipt, với case/approval ngoài app cho **quyết định mới**, không gọi là replay enable. Nó không được vô hiệu latest disable; account đã disabled bởi quyết định có journal cần Admin usable thực hiện enable theo quyền hiện có. Nếu không còn Admin usable để enable, cần chủ dự án cho phép và xác định quy trình operator-enable riêng trước triển khai; **mặc định đóng**, không thêm đường đó trong task credential. Admin duy nhất bị disabled không được set-credential/initial-status tự cứu hoạt động. Admin duy nhất active snapshot nhưng held do enable thiếu receipt cũng phải có phê duyệt riêng; không suy quyền từ role admin trong snapshot.

## Ma trận kiểm thử

Đây là **tiêu chí chưa chạy** cho PR coding. Test PostgreSQL/MinIO/journal tạm, provider transport bị chặn; dữ liệu tổng hợp và artifact/log/journal ngoài repo. Dùng FakeClock/event/barrier và cả hai thứ tự race, không sleep để suy khóa. Kiểm API/web/DB/journal/audit, không chỉ status HTTP. Fault injection không chứng minh mất điện.

| ID | Setup | Hành động | Kết quả mong đợi |
| --- | --- | --- | --- |
| T01 | g1 usable, backup, reset g2, cookie/token trong TTL | Restore giữ journal, hold/migrate/recover; login mật khẩu g1 | 401 chung, không session/cookie mới; g1 quarantine, không g2 verifier từ journal; token/session đóng như #29–#31 |
| T02 | Snapshot có g2/receipt2 và User B không thay đổi | Restore/recover | g2 và B vẫn login được, session đúng generation/TTL không bị thu hồi hàng loạt; account g1 rollback riêng bị chặn |
| T03 | Active không session/token, backup, disable d1 | Restore/recover | Có status event dù batch closure rỗng; effective disabled, mật khẩu đúng vẫn không login |
| T04 | active → d1 disable → a2 enable → d3 disable | Restore tại trước d1, giữa d1/a2, sau a2, sau d3; dùng các head tương ứng | Với latest a2 chỉ snapshot receipt2 active; thiếu receipt held. Latest d3 luôn disabled. Không dùng timestamp quyết định thứ tự |
| T05 | Event credential/disable/enable bền | Rollback DB; riêng commit thực thành công nhưng lost reply | Request lỗi không cookie/delivery; credential quarantine/disable khóa/enable held khi không receipt; receipt thật có thể giữ trạng thái khi inspect nhưng request gốc không tự thành công |
| T06 | User tạo sau backup; User mới cùng email khác UUID | Restore snapshot không User gốc, replay nhiều lần | Absent projection/mirror, không User/verifier/role/session/token/FK mới; không chuyển event theo email |
| T07 | active/proven/quarantine, delivery kiểm chứng; token mới fence | Request/confirm, mật khẩu mới trùng snapshot, TTL biên sau hash/chờ khóa, hai confirm, head đổi giữa request/confirm | Trùng mật khẩu cách ly bị từ chối; một lần confirm hợp lệ thắng; expired/head mismatch từ chối; g mới usable đúng receipt; không đổi status. 202 chỉ tiếp nhận; lỗi/uncertain không delivery |
| T08 | disabled hoặc held; token snapshot còn TTL | Request/confirm và operator set-credential | Web không reset; CLI chỉ credential, giữ disabled/held/role; không session; latest disable không bị reset xóa |
| T09 | Tất cả Admin quarantine, outbox không truy cập; Admin duy nhất active/proven | Operator sai/thật, UUID sai/vắng, đúng case/installation; nhập password an toàn/trùng snapshot | Sai quyền/UUID/mật khẩu trùng bị từ chối; đúng UUID hiện có khôi phục credential không tạo Admin/role/bootstrap. Login mới thành công sau kiểm; không TTY/argv password bị từ chối không echo |
| T10 | Admin duy nhất disabled hoặc legacy held | set-credential, resolve-initial-status không approval/đối latest disable | Vẫn chặn; chỉ quyết định initial status mới có quyền mới cho legacy held; không tự enable latest disabled; không cờ bypass |
| T11 | usable g1 Argon2 cần rehash, session g1 | Login rehash, restore PHC cũ cùng g1/receipt | Giữ g1/password_changed_at, không credential event, session g1 vẫn hợp lệ; policy có thể rehash lại. Routine rehash không gắn generation mới |
| T12 | Anonymous + cookie quarantined; route auth/admin/write | Thiếu/sai CSRF, Origin trusted/khác/vắng, `/csrf`, final session check | CSRF/Origin theo hợp đồng chốt, không dùng cookie quarantine để được principal/CSRF bound/outbox; generic login 401 không lộ account |
| T13 | Login dừng sau verify ngoài khóa | Reset/disable/hold thắng trước/sau User lock; previous-owner A/B đổi chéo | Không session của generation cũ sống qua fence; login trước reset bị revoke, login sau reset stale verify bị từ chối; hold ngăn cookie cuối; không deadlock |
| T14 | Request có principal, original spool/RAG/search đang chậm | Quarantine/disable/hold trước final check/provider/response | Mọi dependency và final check chặn, không provider/byte mới sau phát hiện; bytes đã qua kiểm cuối có giới hạn như hiện có |
| T15 | Batch credential/status + invitation/token/session | Append none/torn/prefix/full-fsync-error × DB commit/rollback/uncertain | Mirror toàn batch false khi fsync lỗi; gate đóng đúng bảng lỗi; prefix chỉ hạn chế được phần hoàn tất, enable không tự cấp quyền; không seq gap |
| T16 | File ahead, mirror có/không, auth projection lệch | Replay fault từng bước rồi recover hai lần | Gate đóng khi lỗi; fold cuối kiểm cả mirror đã present, không receipt tạo từ replay, idempotent sau sửa |
| T17 | SQL bằng role runtime và routine writer test | Raw verifier/status/generation/head/receipt UPDATE/INSERT; NULL/missing/extra/sai User/op/generation/reason/time; GUC rehash; downgrade | Quyền/trigger/parser độc lập từ chối trước append; NULL không qua UNKNOWN; FK ghép/cross-user/generation reuse chặn; routine rehash trust boundary có test CAS riêng |
| T18 | DB restore gen UUID cũ, CSPRNG test ép collision với event rollback/absent | Cấp generation/op và retry | Không reuse cả User/domain; đọc journal current quyết định, timestamp/DB counter không mở quyền; seq liên tục |
| T19 | DB `0032` có active/disabled và chỉ một Admin; snapshot cũ `0031`/trước đăng ký | hold trước migrate → upgrade → replay/backfill; init/register-existing/cờ confirm | Không migration I/O journal; legacy quarantine/held, disabled import hạn chế đúng; không baseline usable từ snapshot/cờ; ORM schema khớp, downgrade fence |
| T20 | Project cô lập, pg_dump/restore + MinIO thật, proxy Next.js, password/cookie/token lưu trong TTL | Restore/recover lần một, credential recovery g3, restore lần hai + recover hai lần | Cùng predicate, g1 lại quarantine khi journal g3; B chưa rollback dùng được; absent không dựng; route web/API đều đạt; source journal không đổi, 0 provider call |
| T21 | Existing common/review/session/reset/invitation decisions + auth mới | Replay/backfill/drill chung, batch prefix | Tất cả fence/cleanup/hậu điều kiện cũ vẫn giữ; không quyền review/original/search/RAG sống lại; invitation không dựng account |
| T22 | Password/verifier/token/email/link marker tổng hợp, CLI/API mọi cửa sổ lỗi | Quét journal/log/audit, parser field thừa, repr/argparse/exception | Không secret/payload delivery; chỉ UUID/codes/time và metadata cũ allowlist; không hash/fingerprint verifier. DB verifier/token hash vẫn ở bảng auth đúng quyền |
| T23 | DB/journal cùng rollback hoặc mất; fsync fault trên môi trường test | Recover bằng dữ liệu còn lại | Không báo đã chứng minh bảo vệ quyết định mất; giữ hold khi không có chain đáng tin. Ghi giới hạn cùng rollback không phát hiện và chưa chứng minh mất điện |

## Chia PR coding

**Chưa mở tính năng khi chỉ một phần gate chạy.** Đã chốt triển khai xuyên PR trên môi trường cô lập: PR nền cài safety version yêu cầu nhưng chưa ready → recovery gate đóng, bootstrap/accept/reset/status writer chưa chuyển hợp đồng cũng bị chặn. C1–C3 không nâng stack dev/môi trường đang phục vụ; mọi môi trường đã nâng giữ pending tới C4, nâng môi trường đang phục vụ cần task cutover/runbook/maintenance riêng. Không có flag tắt để quay về auth `0032` trên DB/journal đã bật schema mới. Intermediate PR kiểm trên DB/project cô lập, không quảng bá chống rollback đã hoàn chỉnh.

| PR/task nhỏ | Phụ thuộc và quyết định phải chốt trước | Đầu ra kiểm chứng / đóng đường chưa hoàn chỉnh |
| --- | --- | --- |
| C1 — schema/journal/projection, receipt và backfill hạn chế | Quyết định nền A/body/generation/receipt, runtime/migrator/rehash, baseline và C1–C3 cô lập đã chốt theo ủy quyền. Không cần chọn email provider | Migration mới sau `0032`, ORM match; parser/mirror/body guard; fold seq; hold tương thích cũ, legacy đóng, session/token legacy closure. Test T15–T19/T21/T22. Safety version chưa ready, toàn gate và auth writer đóng, **không** release usable/active từ replay |
| C2 — tổng predicate login/session/final checks và toàn writer auth, chia C2a–C2d bên dưới | C1; session generation, rehash routine, Origin và disable/enable conditional receipt theo hợp đồng đã chốt | Bootstrap/accept/reset/rehash/disable/enable/commit_auth chuyển đồng bộ; actor/target locks, fence token/session và response/provider final check. T01–T06/T11–T14/T17/T21. Gate vẫn chưa ready và operator restore report chưa cho start traffic; route khôi phục còn đóng |
| C2a — login/session và predicate nền | PR #33 (`0034`); [Origin đã chốt](decisions.md#chia-c2-và-chốt-origin-theo-ủy-quyền-2026-10-03) | [Prompt C2a](tasks/m6c2b2c2a-login-session-predicates.md): login/rehash/session writer bằng runtime, commit nền, predicate ở dependency và caller dùng helper hiện có; test trực tiếp/race/integrity thật. C2 tổng chưa hoàn tất, toàn safety vẫn pending |
| C2b — reset và disable/enable | C2a; predicate/routine/commit/clock đã review | Normal reset fence, credential/status event + receipt, closure tokens/sessions, actor/target đúng lock order; không reset tự enable, không bypass held/disabled |
| C2c — bootstrap/invitation | C2a/C2b; User creation và auth batch nền đã review | Bootstrap một lần và accept User mới cùng credential/status receipt/event/closure/audit, không phát link/cookie khi uncertain; không dựng account từ replay |
| C2c-1 — runtime bootstrap và initial receipt | PR #35 đã merge `1d3e558`, `0036`; [prompt](tasks/m6c2b2c2c1-bootstrap-initial-receipts.md) | Routine creation/bootstrap hạn chế, singleton, cặp receipt, history/absent/uncertain và CLI; chưa chuyển invitation writers, vẫn pending |
| C2c-2 — invitation lifecycle | PR #36 đã merge `d1b53e6`, head `0038`; [prompt](tasks/m6c2b2c2c2-invitation-lifecycle.md) | Đã merge PR #37 `ca4a623`, `0039`: create/reinvite/list/inspect/accept/revoke và delivery compensation, actor session/Admin/CSRF cuối, một batch accepted + initial receipt; không role Admin qua invite |
| C2d — final checks và integration đủ writer/caller | C2a/C2b/C2c đã merge; [C2d-1](c2d1-final-caller-reads.md) đã merge PR #38 và caller inventory | [C2d-2a](c2d2a-resource-json-reads.md): đúng 13 GET library/collections/discovery đã triển khai trên nhánh review, chưa merge. Review/publication GET C2d-2b và writer/file/chunk/search/RAG/provider còn riêng; giữ lock order và T14. Không để C4 mới bổ sung bảo vệ cốt lõi |
| C3 — recovery credential User/Admin và status initial có approval | C2; chủ dự án chỉ định operator/bằng chứng/UUID inventory/case; chốt quyền initial status riêng, không operator-enable latest disabled mặc định | CLI inspect/set-credential/resolve-initial-status đề xuất, safe TTY, audit và uncertain outcome; reset quarantine chỉ nếu delivery kiểm chứng đã cấu hình. T07–T10/T12/T15/T22. Không bypass gate hỏng, không đọc outbox qua quarantined Admin, không release safety ready trước drill |
| C4 — cutover/drill và enable safety ready | C1–C3; phê duyệt runbook/image/storage journal độc lập và điều kiện hậu kiểm, không gán mất điện là đã đạt | `0032` + snapshot cũ → hold/migrate/replay/backfill/operator → checks → ready; restore hai lần qua proxy và mọi cửa sổ lỗi T01–T23. Safety ready chỉ do hậu điều kiện tất cả gate/writer hoàn chỉnh; cập nhật AGENTS lệnh đã thực sự kiểm. Full suite phù hợp và drill thật trên project cô lập, không provider/dev data |

C4 không triển khai account status lần đầu: status event/predicate/writer phải hoàn chỉnh ở C1/C2; C4 chỉ chứng minh và mở cutover. Có thể chia test/drill riêng theo phạm vi, nhưng không để disable chỉ đóng session rồi gọi credential gate hoàn chỉnh.

### Hợp đồng kiểm thử khi C1–C3 còn pending

HTTP trả `503 service_recovering` chỉ chứng minh gate pending đóng, **không** chứng minh journal, ACL hay writer đúng. Báo cáo mỗi PR phải tách kiểm trực tiếp ở tầng được triển khai, kiểm HTTP pending và luồng HTTP hoàn chỉnh chưa chạy; không gọi cả suite hồi quy đã đạt khi các ca được phép chưa được thực thi. Giữ nguyên inventory/expectation nghiệp vụ cũ; nếu ca HTTP chưa chạy được trước C4 thì ghi rõ phần hoãn và test tầng dưới thay thế, không đổi hàng loạt expectation thành 503 hoặc tính ca hoãn/skip là pass.

| Giai đoạn | Cách thực thi test | Chứng cứ phải có |
| --- | --- | --- |
| C1 | Gọi trực tiếp migration/schema, parser, routine/fence SQL dưới đúng role, journal batch, replay/fold/backfill và hậu điều kiện trên DB/journal tạm. Dùng fixture hợp lệ của domain cũ để chạy recovery/common/review/session/reset/invitation ở tầng trực tiếp, không đi qua HTTP pending | Mỗi fence có cặp được phép/bị chặn: batch/mirror hợp lệ commit và replay đúng; malformed/NULL/sai UUID/không journal bị từ chối trước append. Common còn quyền/private owner còn quyền được phép tại predicate/routine tương ứng, revoked/khác owner bị chặn. HTTP **không mock** xác nhận auth/library/file/search/RAG/Admin/collection trả 503 khi pending, `/healthz` sống và `/readyz` không ready; đây là kiểm gate riêng |
| C2 | Gọi writer/commit và predicate thật trên fixture đã có generation/receipt/head/session/token hợp lệ, rồi tạo từng điều kiện sai; test race bằng barrier ở hash/lock/commit. Nếu entrypoint writer vẫn kiểm pending cutover, fixture **chỉ trong test** monkeypatch kết quả kiểm sẵn sàng cutover tại boundary hàm đó, không thay writer, `commit_auth`, DB guard, gate integrity hoặc predicate auth | Có cả reset/disable/enable/login/rehash/accept hợp lệ thực sự commit, receipt/journal/audit đúng, session generation dùng được; và ca stale/quarantine/disabled/TTL/ID sai bị từ chối không phát cookie/link. Kiểm recovery chain/head/fsync/hold và final check vẫn thật; test pending không patch vẫn chặn. Kết quả tầng writer/predicate không được ghi là HTTP production ready |
| C3 | CLI thực chạy trên project/DB/journal tạm trong hold hợp lệ theo hợp đồng operator; HTTP recovery adapter/handler và predicate có fixture đúng UUID/case/authority/delivery. Khi cần, dùng cùng patch test-only của **pending cutover**, không patch xác minh operator/approval, trusted Origin/CSRF, TTL/fence hoặc gate integrity | Operator/User hợp lệ đặt credential mới có receipt, login/session predicate mới được phép; sai quyền/UUID, absent, disabled/held, token dùng lại/expired và uncertain commit bị chặn như T07–T10/T12/T15. CLI không cần mở HTTP; outbox của Admin quarantine vẫn bị từ chối. Giữ kiểm HTTP pending thật tách riêng |
| C4 | Bỏ mọi patch pending trong suite luồng hoàn chỉnh, chạy readiness/recovery thật và HTTP qua API lẫn proxy web sau hold/migrate/replay/backfill/hậu kiểm; restore/recover hai lần | Account không rollback login/file/chunk/search/collection được phép, RAG với provider giả hợp lệ; account cách ly/disabled/thu hồi/khác quyền bị chặn với mã nghiệp vụ đúng sau gate mở. Trước recover gate thật 503, sau recover không còn 503 toàn cục che lỗi. Chạy lại inventory HTTP cũ và T01–T23; không đổi expectation để làm suite xanh |

Patch test-only nằm trong fixture/test module, scope từng test và tự restore; không thêm env/config flag, header, route, CLI switch hoặc nhánh `environment=test` trong code production để bỏ gate. Không patch chung `recovery.evaluate` thành open: nếu pending và integrity đang gộp một hàm, test cần tách kiểm sẵn sàng cutover khỏi kiểm chain/hold/integrity bằng hàm thường **luôn chạy trong production**, rồi chỉ thay kết quả cutover ở test. Chứng minh lỗi journal/hold vẫn chặn writer ngay trong fixture có patch. SQL fence dùng role runtime thật; không tắt trigger hay sửa receipt/head bằng quyền owner để ép ca được phép. Fixture tạo dữ liệu theo hợp đồng hợp lệ, không gắn `journal_confirmed` giả hoặc registration mới để hợp thức hóa snapshot. CLI C3 trong hold vẫn kiểm mọi integrity/hậu điều kiện khác, không dùng patch để vượt journal hỏng.

**C1 đã merge, C2a được giao coding:** chủ dự án đã ủy quyền orchestrator chọn phương án hợp lý, thuận tiện; [quyết định nền](decisions.md#quyết-định-nền-c1-theo-ủy-quyền-2026-10-03) đã chốt A, cách ly **toàn bộ account legacy** một lần, C1–C3 cô lập/pending và runtime/migrator/trusted rehash. [Origin compatibility/cách chia C2](decisions.md#chia-c2-và-chốt-origin-theo-ủy-quyền-2026-10-03) đã chốt; coding agent bắt đầu [C2a](tasks/m6c2b2c2a-login-session-predicates.md), không xin lại nền này. Operator/delivery/status recovery trước C3 và cutover/storage deployment vẫn theo milestone riêng.

### Prompt coding C1

Thực hiện [prompt đầy đủ C1](tasks/m6c2b2c1-auth-safety-foundation.md), sử dụng quyết định nền đã chốt theo ủy quyền. Fetch/xác minh main có PR #32; giữ các cập nhật tài liệu bàn giao, tạo nhánh mới từ head thực tế và đưa chúng vào PR C1. Không tạo thêm PR thiết kế chỉ để xin lại nền đã chốt.

**Hoàn thành C1 khi:** ORM/migrated schema match, parser và SQL độc lập bắt NULL/sai UUID/op/gen/reason/secret, prefix/fsync/rollback/replay/absent idempotent và seq liên tục, upgrade `0032` + hold trên snapshot cũ đạt, downgrade bị chặn, baseline cũ không usable. Kiểm schema/journal/replay/fence và hồi quy common/review/session/reset/invitation **trực tiếp** phải có cả ca được phép và bị chặn; HTTP không patch xác nhận gate pending đóng cho mọi route được bảo vệ. Chạy nhóm T15–T19/T21/T22 và test recovery/schema/auth **ở tầng đã triển khai** theo hợp đồng pending ở trên; báo inventory pass/fail thật, ca HTTP hoãn tới C4 và giới hạn, không tính mọi request 503 là hồi quy đã đạt. Không khai triển C2–C4 hoặc mở ready trong C1.”

## Quyết định cần chủ dự án chốt và giới hạn

| Quyết định mở | Đề xuất hiện tại / PR phụ thuộc |
| --- | --- |
| A, baseline và phạm vi thử | Đã chốt theo ủy quyền: legacy một lần quarantine/held; C1–C3 cô lập/pending, không nâng dev. Cutover môi trường đang phục vụ cần runbook riêng |
| Guard và quyền DB | Đã chốt theo ủy quyền: runtime/migrator tách biệt, routine CAS rehash có trusted identity boundary; C1 xây nền, C2 chuyển writer |
| Operator và chứng cứ | Người được ủy quyền, inventory UUID ngoài backup, approval/case, thiết bị/kênh nhập và lưu hồ sơ; chưa tồn tại; C3 |
| Quyền status/Admin | Initial status là quyết định mới được duyệt; latest disabled không operator-enable mặc định. Nếu mọi Admin disabled, cần quy trình/ủy quyền enable riêng hoặc chấp nhận giữ khóa; C3 |
| Delivery/Origin | Không mặc định email; adapter/kênh kiểm chứng hoặc operator; Origin bắt buộc cho auth write đề xuất với client compatibility; C2/C3 |
| Cutover/storage | Maintenance xuyên PR, binary/image đồng bộ, chỉ một journal writer DB, nơi lưu journal độc lập/retention/độ bền; C4, production storage vẫn task riêng |

Journal phải độc lập và hiện hành, giữ lịch sử đủ lâu hơn mọi snapshot có thể restore; hash chain/UUID không giúp nếu journal và DB cùng mất hoặc cùng rollback. File/volume riêng trên cùng máy chưa là độc lập về miền sự cố; PG advisory không khóa hai DB restore cùng append một file. Không có verifier/token trong journal nên không thể tái tạo credential mới; không chứng minh lịch sử đã mất hoặc sự khác nhau của mật khẩu mới với mọi mật khẩu cũ. Quarantine/held bảo vệ được rollback nhận biết, không bảo vệ toàn bộ role/membership/private deletion hay host/DBA bị chiếm quyền.

Fsync trả thành công là hợp đồng phần mềm, không chứng minh flush thật qua Docker Desktop/Windows/controller/hardware khi mất điện. Cần chọn storage và kiểm độ bền riêng; fault injection/drill một máy không là chứng cứ mất điện, SLO/RPO/RTO production. PR này chỉ kiểm tài liệu; ma trận và CLI/schema mới chưa được chạy/triển khai.
