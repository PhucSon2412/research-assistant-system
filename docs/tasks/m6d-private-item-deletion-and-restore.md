# M6d — xóa paper riêng, cleanup và chống hồi sinh sau restore

## Giao việc và base

Đây là **task triển khai một luồng hoàn chỉnh trong một PR**, theo ủy quyền của chủ dự án về cách hoàn thành MVP. Không dừng ở khảo sát/kế hoạch và không chia mặc định theo từng endpoint hoặc bảng.

PR #61 đã merge tại `fb96354c4f8dc8fb8f4f8a54f1516d9d2e13625e`; head review `3f07901` là ancestor và tree khớp. Source hiện **0051**; chưa rerun tại merge, không suy revision hoặc tình trạng DB dev. Fetch và xác minh lại trước khi tạo `feat/m6d-private-item-delete-restore` từ `origin/main` mới nhất. Giữ mọi docs review/task modified/untracked hiện có, đưa vào PR này; không reset/discard hoặc bỏ sót report PR #61.

**Mục tiêu:** owner xóa một item private cùng mọi version/file của nó; từ commit xóa, request mới không đọc hoặc sử dụng nội dung đó; cleanup dọn dữ liệu trong thời hạn cấu hình; restore snapshot cũ với journal hiện hành không hồi sinh item/file/grant/index. Bản private khác và bản common đã phê duyệt độc lập vẫn nguyên vẹn, kể cả cùng checksum hoặc cùng physical key còn được dùng hợp lệ.

Đây là phần xóa/restore của **gói C** trong [kế hoạch MVP](../mvp-delivery-plan.md). C4 đã hoàn tất kiểm cô lập; **không đặt lại toàn hệ thống thành pending tới một C4 mới**, không mở gate bằng bypass. Không cutover dev hoặc bật provider thật trong task này.

## Đọc trước khi sửa

- `AGENTS.md`, [mục lục](../README.md), [yêu cầu F07/F12](../product-requirements.md), [hợp đồng thu hồi/xóa](../access-and-data.md#hợp-đồng-thu-hồi-và-xóa), [decisions](../decisions.md).
- [C4 handoff/runbook](../c4-integrated-runtime-cutover.md), [review PR #61](../reviews/pr61-c4-integrated-runtime-cutover.md), [inventory C4](../c4-test-inventory.json).
- [Cleanup common runtime](../c2d5e1-revoked-common-cleanup-runtime.md), [orphan reconcile](../c2d5e2-orphan-original-reconcile-runtime.md), [original downloads](../c2d4c-original-downloads.md), [review writers](../c2d5a-review-json-writers.md), [approval](../c2d5b-review-approval-original-copy.md).
- Worker/document index, citation/discovery hooks, search/query/rerank và RAG runtime handoff tương ứng. Đọc code/FK thực tế của item/version/file/job/intent/review/rights/ledger trước thiết kế purge.

Các điểm đã thấy trên main: private chưa có deletion state/gate; các private SQL clause chủ yếu kiểm owner; `DeletionTombstone` hiện chỉ dành cho common copy; review request và upload intent còn tham chiếu private item/file; SourceFile có thể chia sẻ storage key. Không giả định cleanup common dùng nguyên trạng cho private được.

## 1. Hợp đồng sản phẩm/API đã chọn

- **Owner-only, xóa toàn item:** chốt `POST /library/items/{item_id}/delete`, body `{ "confirm_delete": true }`. Đây là logical delete với purge bất đồng bộ. Chỉ nhận boolean `true` thực; `1`, `1.0`, chuỗi, thiếu hoặc false bị từ chối trước mutation. Dùng session/Origin/CSRF/runtime và final caller/clock hiện hành. B/Admin không được xóa private của A; common không đi qua endpoint này. ID trái quyền và vắng mặt không tiết lộ khác biệt.
- UI có nút xóa ở item riêng của owner, xác nhận tường minh không chọn sẵn, thông báo không có undo. Giải thích xóa khỏi thư viện có hiệu lực trước khi physical purge hoàn tất. Không tạo màn hình trash/restore hay xóa từng version trong MVP này.
- **`t_del` = commit DB chấp nhận deletion.** Cùng transaction: ghi trạng thái xóa cuối, tăng epoch một lần, gỡ active generation/indexed, đánh dấu hủy job, đóng mọi review request còn mở với journal đúng hợp đồng, tạo deletion/tombstone bền và audit chỉ ID/code/count. Không đọc/xóa S3 hoặc gọi provider trong transaction API này.
- Sau known commit, dùng transaction/proof mới để trả receipt tối thiểu (item ID, đã xóa, hạn purge hoặc trạng thái cleanup hiện tại), kiểm caller/clock và binding deletion. Thành công không đồng nghĩa object đã bị xóa. Commit exception là outcome uncertain: lỗi JSON có kiểm soát, không trả success/IDs mới, không SELECT để đoán, không tự retry. Release lỗi không được đổi outcome đã biết; giải phóng connection trên mọi đường, kể cả ngắt.
- Owner gửi lại cùng thao tác có receipt hợp lệ là idempotent: không thêm quyết định deletion, tăng epoch lần nữa, mở job hoặc reset cleanup đã xong. UI không tự resend POST khi timeout/503/504 hoặc mất response; cho người dùng tải lại thư viện để kiểm trạng thái.
- **Private và common là hai bản độc lập.** Xóa private không tự thu hồi bản common pending/listed đã được approve với object/rights riêng; không sửa rights/publication/common jobs hoặc quyền của bản đó. Request đang open bị kết thúc, nên không approve mới từ private đã xóa. Nếu approve thắng trước deletion, bản common mới giữ nguyên; nếu deletion thắng, approve không được công bố bản sao mới. Chính sách này được ghi rõ trên UI/docs.

Có thể điều chỉnh tên DTO/mã lỗi cho thống nhất code hiện hành, nhưng không làm yếu các semantics trên. Cập nhật contract FastAPI/OpenAPI và docs từ cùng nguồn; không tạo hệ thống API/mock/workspace mới chỉ để làm task này.

## 2. Trạng thái, SQL gate và writer fence

Thêm revision mới sau head thực tế; **không sửa các migration đã merge**. Tự chọn schema nhỏ nhất đáp ứng đầy đủ hợp đồng: trạng thái riêng/tombstone/manifest file có thể là bảng mới hoặc mở rộng có phân biệt target rõ. Không giả item private là common revoked, không nới `AccessRights` hoặc các fence `0044/0045` để né ràng buộc.

- Trạng thái xóa không đảo ngược. Tombstone sống độc lập với vòng đời target, có binding owner/item/epoch và đủ mọi version/file/key cần dọn; không chỉ active version/file. Không nhận tombstone naming một file của item khác. Không cho tái sử dụng ID đã xóa để vượt replay/fence.
- Lập caller map và áp predicate private còn sống **trong SQL trước count/page/top-k**: library/detail/files, metadata candidate/coverage/chunks/references, collection membership/list, discovery citing-items, keyword/vector/hybrid, viewer/citation và mọi scope RAG. Không chỉ lọc ở UI hoặc sau top-k.
- Đọc file private và review-original phải kiểm deletion ở lần proof cuối. Request mới sau `t_del`, gồm URL/range cũ, bị từ chối trước gửi nội dung. Review list/detail/download không giữ grant vì request còn nằm trong snapshot hay cache.
- Upload/metadata/membership/review/approve và mọi writer dẫn xuất không được nối thêm dữ liệu hoặc phục hồi active pointer cho item đã xóa. Worker claim/extraction/index, citation reconcile/discovery retarget, query/rerank/RAG kiểm lại deletion/epoch ở các boundary hiện có. Trigger/SQL guard phải bắt late derived write, không chỉ rely vào Python.
- Nội dung đã qua điểm kiểm cuối trước `t_del` có thể đang truyền; nội dung đã gửi client/provider không lấy lại được. Khi phát hiện deletion, ngừng phần nội dung tiếp theo theo hợp đồng hiện hành; kết quả generation không được trả hoặc index không được activate từ nguồn đã xóa. Không hứa triệt tiêu khe mạng giữa proof và send, không thêm streaming trong task này.
- Outcome provider về muộn vẫn được đối soát tài chính một lần, nhưng không ghi lại text/vector/index sau deletion/cleanup. Quota/reservation `uncertain` không bị hoàn tiền giả hoặc gửi lại để né phí.

Đối chiếu lock graph trước sửa. Approval khóa review request trước private item; worker khóa item trước job. Deletion phải tương thích cả hai: khóa các request theo thứ tự ổn định trước item nếu cần đóng chúng; không chờ request sau khi giữ item trong khi approval giữ request chờ item. User/caller lock trước journal; không lấy User/item/resource lock mới sau journal. Cleanup và trigger theo key cũng phải có thứ tự tương thích. Chỉ có proof đã release sạch mới được bắt đầu storage I/O.

Nếu thêm bảng/routine bảo vệ, cập nhật provisioning/runtime profile, fixed search_path, grants âm/dương và ORM/schema. Runtime không owner/DDL/TEMP/raw writes auth/journal/control plane; không fallback owner hoặc gọi cleanup operator từ runtime.

## 3. Journal và recovery: bảo vệ toàn bộ quyết định xóa

Mở rộng **journal hiện có**, không tạo một journal/flag bypass khác. Typed event deletion mang ID/epoch/code/time và manifest version/file/key/checksum cần thiết; không chứa title, filename, URL, text, câu hỏi, token hay credential. Manifest đầy đủ được xác định dưới khóa và parser/SQL kiểm độc lập, NULL-safe, đúng owner/item/file. Có giới hạn cấu hình phù hợp; từ chối toàn thao tác trước append nếu vượt giới hạn, không truncate manifest.

- Mirror của toàn batch được flush `journal_confirmed=false` trước append; chỉ confirm sau append/fsync thành công. Dùng thứ tự **review closure(s) trước → private deletion event cuối** để prefix sau rollback chỉ áp các hạn chế đã ghi đủ. Thêm reason/status cho review closure bằng revision nếu cần; không đổi semantics approved thành withdrawn hoặc dựng một phê duyệt.
- Append thiếu/dở/đủ nhưng fsync lỗi vẫn phải theo hướng fail closed: khi DB còn commit được, commit quyết định hạn chế với mirror false, báo lỗi và giữ gate đóng tới recover. Không đánh dấu confirmed chỉ vì bytes file và DB khớp. Nếu transaction rollback hoặc commit uncertain, journal chỉ được replay theo hướng hạn chế; không báo thành công hoặc retry mutation dựa vào replay.
- Recover fold/replay event hiện hành trước backfill, kiểm cả present-record hậu điều kiện. Snapshot còn item phải bị đóng; item/file/request absent chỉ nhận quyết định phù hợp, **không dựng lại target/User/receipt/credential**. Nếu ID có trong snapshot nhưng tuple trỏ owner/item/file khác, fail closed trước xóa nhầm.
- Recover hai lần an toàn. Snapshot trước khi tạo item, trước deletion, sau logical delete hoặc sau purge đều không hồi sinh file/index/grant. Object bị restore trả về phải được dọn lại trước mở readiness, kể cả tombstone trong DB đã có `purged_at`.
- Dùng contract C4 thật: hold trước restore/migrate; provision/recover hoàn tất mới ready. Không patch ready/integrity/receipt hoặc miễn replay vì checkpoint được restore cùng DB. New-install/bootstrap, năm domain journal cũ và C3 case vẫn giữ hành vi.

Lịch sử chỉ bảo vệ khi journal độc lập hiện hành chứa quyết định. DB và journal cùng rollback vẫn ngoài khả năng phát hiện. Không tuyên bố mất điện an toàn từ fault injection/SIGKILL.

## 4. Cleanup vật lý và dữ liệu nhạy cảm

Tích hợp normal cleanup runtime/CLI/Celery với checkpoint bền và lịch hiện hành, không thêm online orphan GC. Thời hạn purge từ config (tái dùng mặc định 24 giờ nếu đúng hợp đồng hiện tại), có trạng thái/số lần thử/mã lỗi và retry idempotent. Recovery có entry quản trị đã provision riêng, không negative-check runtime rồi cho qua.

- Đọc FK graph thật và liệt kê mọi dữ liệu thuộc item: versions/files, job, intent, candidate/page/section/chunk/reference/citation edge, index/embedding, membership và review text. Xóa/scrub đúng thứ tự, giữ chỉ những skeleton/ID/checksum/cost fields cần ràng buộc hoặc đối soát và ghi rõ lý do. **Không giữ title, URL, filename, lời khai/ghi chú riêng, text/hash chunk/vector chỉ vì trigger/NOT NULL/FK cũ đang ngăn scrub**; sửa hợp đồng cleanup có kiểm soát bằng migration. Không sao chép giá trị đã xóa vào audit/journal/error.
- Review/rights history có thể cần row binding để bản common độc lập còn dùng được; giữ binding tối thiểu, không giữ toàn bộ private text bằng lý do có FK. Không thêm UI exception retention hoặc workflow chính sách mới cho private trong task này.
- Xóa S3 ngoài mọi connection/DB/key/journal lock. Trước DELETE có fence bền theo key và kiểm mọi tham chiếu còn hợp lệ trong toàn DB; SourceFile writer dùng cùng serialization và từ chối key đã fence. Không chỉ đếm cùng owner/checksum hoặc suy object không shared từ storage prefix.
- Khi một bản B/common còn dùng physical key hợp lệ, giữ object đó nhưng vẫn xóa dữ liệu/đóng quyền của A. Khi không còn tham chiếu hợp lệ, dọn được object kể cả target file absent trong snapshot. Không nhận bucket/key tùy ý từ HTTP/CLI.
- Giữ retry sau crash ở giữa fence/DELETE/finalize, storage lỗi, commit uncertain/release lỗi. Không rollback object cùng DB, không hứa DELETE đúng một lần, không reset `purged_at` tùy tiện để né replay. Chỉ báo purge xong sau kiểm không còn dữ liệu dẫn xuất thuộc target và các bước object cần thiết đã hoàn tất.

## 5. Kiểm chứng bắt buộc theo rủi ro

Reuse runner/context C4 và tooling hiện có. Conda `ras`; DB/roles/MinIO/Redis/journal/project/port riêng; outbound bị chặn. **Không đọc/sửa `.env`, query/migrate/restart dev, gọi provider/arXiv thật hoặc bật pricing thật.** Có thể dùng fake provider test-only và paid settings synthetic như C4 để có item indexed/RAG thực; không bypass content authority/accounting.

1. A/B/Admin, private cùng checksum, common pending/listed riêng, multi-version/file, received/extracting/awaiting_index/indexed/needs_review: allowed deletion và mọi denial, confirmation nghiêm, CSRF/Origin/session expiry/current role, repeat/concurrent deletion, response proof và commit/release/interrupt outcome.
2. Trước cleanup, toàn bộ URL/source/ref/reverse count/list/collection/search/RAG đã chặn; top-k=1 không bị item đã xóa chiếm slot. Giữ control active để không pass vì mọi account/source đều bị chặn.
3. Race tất định delete với approve (hai thứ tự), download đang spool/stream, extraction/index trước/trong provider và trước activate, rerank/generation trước send/trước response, citation retarget và late financial settlement. Dùng barrier/lock-wait rõ, không sleep ngẫu nhiên làm phép thử chính; assert không lộ nội dung/không ghi mới, không chỉ khác mã lỗi.
4. Journal none/torn/prefix/full-fsync-error × commit/rollback/uncertain; binding missing/NULL/sai UUID/owner/file; raw SQL guard, runtime grants, checkpoint/hold và replay idempotence. Không tắt trigger trên đường được tuyên bố business hợp lệ; privileged corruption setup phải tách riêng.
5. Purge scrub đủ trường, không tái ghi marker/vector/chunk hash, không đổi rows của B/common. Có fixture shared key thật và fixture same checksum/key khác; xử lý orphan, storage lỗi, crash sau fence/DELETE và late writer. Không dùng yêu cầu marker biến mất toàn DB khi một bản hợp lệ khác còn cùng bytes.
6. **Một full drill mới qua image thật:** backup DB + object → xóa private (có grant mở và index) → cleanup → restore snapshot/object cũ giữ journal mới nhất → hold/migrate/provision/recover → kiểm ready và denial. Hai vòng restore/recover, cộng item được tạo rồi xóa sau backup; A/B/Admin qua proxy, common và B control vẫn dùng được. Có trường hợp restore object đã purge để chứng minh sweep chạy lại. Không chỉ `CREATE DATABASE TEMPLATE` hoặc gọi seed rồi ghi full drill pass.
7. **Chrome thật qua Next proxy:** owner xác nhận/xóa; library/collection không còn item; source/original URL đã lưu bị chặn; B/Admin không xóa được A; UI phân biệt logical delete/cleanup và unknown outcome, không auto POST lại. API/worker chạy cùng image đã niêm, không thay backend bằng host fake.
8. Upgrade có dữ liệu từ `0051`, ORM/schema và quyền runtime sau reprovision; hồi quy new-install/C4 readiness, common revoke/cleanup, review closure và auth journal/C3 liên quan. Giữ các expectation đã chứng minh; không phục hồi pending seam để test xanh.
9. Duplicate/retry: cùng upload intent không tạo thêm item/job/chunk; retry job không tạo dẫn xuất hoặc embedding charge lặp ngoài hợp đồng; B/common không bị liên kết qua checksum. **Item đã xóa không được làm đích reuse/dedup hoặc được retry để sống lại.** Upload mới cùng bytes sau deletion là item/key mới với consent/intent mới. Ghi chính xác hành vi duplicate cảnh báo hiện có; nếu F07 còn thiếu, nêu gap cụ thể trong handoff/kế hoạch, không gọi là đã hoàn tất hoặc xây canonical merge/version graph trong PR này.

Vì thay đổi shared private ACL, triggers và journal/recovery, chạy full API inventory **một lần ở source cuối**, đối chiếu JUnit theo ID và giữ 5.163 ID C4 cùng các ca mới; không full suite mỗi lượt sửa. Chạy web tests/typecheck/build và các kiểm trên. Không cần lặp cả năm full drill C4 nếu không có thay đổi/rủi ro tương ứng; hồi quy domain liên quan và drill private mới phải thực chạy. Countercheck một số guard quan trọng, khôi phục bytes trước suite cuối. Không sửa code/mutant khi suite chính đang chạy. Giữ fail/interruption/rerun riêng; theo dõi process/exit, dùng `@args` tránh WinError 206.

## Hoàn thành và bàn giao

PR hoàn tất khi có luồng owner delete → denial ngay → cleanup → restore denial, runtime thật và C4 readiness thật, đầy đủ API/UI/migration/test/runbook trong **một PR review được**. Không kết thúc ở soft-delete UI hoặc journal parser riêng.

- Cập nhật docs hợp đồng bị ảnh hưởng, caller/FK/lock map và [kế hoạch MVP](../mvp-delivery-plan.md), đánh dấu PR #61 merged/source 0051 theo chứng cứ. Lịch sử review giữ nguyên; không nhân bản log kiểm vào mọi file.
- Handoff/inventory ngắn ghi base/code/image/head/schema, file đổi, selected/pass/fail/error/skip/not-run, source/JUnit/artifact hashes, fault thật/giả, các lựa chọn schema và dữ liệu giữ lại. Không coi kết quả merge là một lượt test mới.
- Kiểm baseline/ownership trước cleanup; chỉ xóa project/volume/image/credential thử của task, không prune hoặc sửa drift dev. Quét secret, kiểm diff/links.
- Commit/push và mở **PR tiếng Việt, chưa merge**; không thêm AI vào tác giả/committer/trailer hoặc dòng quảng bá PR. Giữ docs orchestrator/reviewer hiện có trong commit/PR. Báo giới hạn production/provider, bytes đã gửi và journal cùng rollback đúng phạm vi.
