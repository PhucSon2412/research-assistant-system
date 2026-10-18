# M6d — xóa toàn item private và restore

## Sửa P2 của PR #62 — chờ review lại

Code `a5eb8bb6aeb81814707fbe2207e147c8a6120274`, source giữ **0052**. Cleanup finalize nay khóa **tombstone → mọi
review request theo UUID → item → derived/scrub → journal**. Acceptance/approval
giữ review → item; review-original, approval proof và review reads cũng khóa
review trước item. Common publication không lấy private review row lock.
Repeat chỉ đọc tombstone hiện có bằng MVCC, không lấy row lock
hoặc cập nhật tombstone. Các cleanup cùng tombstone vẫn dùng SKIP LOCKED. Guard
0052 chặn thêm review sau deletion nên tập request đã khóa không mọc thêm. Không
đổi acceptance, receipt proof, parser/SQL/grants/migration, timeout hoặc clock.
S3 vẫn ngoài mọi connection/DB/key/journal lock; recovery vẫn quiescent.

Regression trước sửa: **2 control pass / 2 failure**, cả HTTP và boundary có review
đều ghi `40P01 DeadlockDetected`. Sau sửa, **9/9 ca mới pass**: cleanup đi trước,
repeat đi trước, 0/1/3 review history, repeat sau purge và hai deletion đồng thời.
Observer chỉ đọc lấy PID từ Session lifecycle, kiểm `pg_blocking_pids`, ghi SQLSTATE;
barrier được thả trong finally. Lượt chuẩn bị observer thread-local sai được giữ
riêng (không là chứng cứ P2). Countercheck khôi phục code cũ bắt lại `40P01`; đã
khôi phục đúng bytes bản sửa trước lượt cuối.

Lượt hồi quy source sửa: **731/731 pass**, 0 fail/error/skip; **4.528 not-run**
trong collection 5.259. Gồm bốn file M6d, review/approval, cleanup runtime, C4
contract, ORM/migrations và schema. Source hash trước/sau khớp. Runtime/PG/journal
thật, operational C4 và `/readyz` thật; lock tests dùng storage fake đã khai báo.
Không chạy lại full 5.250, web/image/Chrome/backup-restore cho sửa Python lock hẹp;
chứng cứ trước P2 bên dưới giữ source cũ. Hai failure discovery barrier/orphan clock
lịch sử vẫn chưa xác lập nguyên nhân, không được coi đã giải thích bởi P2 này.
F07 và các giới hạn production/provider giữ nguyên.

[Inventory P2 theo ID, source/JUnit/artifact hash](m6d-p2-lock-order-inventory.json)
· [Review tại 7494f8c](reviews/pr62-m6d-private-item-delete-restore.md)
· [Task sửa](tasks/fix-pr62-private-cleanup-repeat-delete-lock-order.md).
Raw runner ghi commit `917a294`; chỉ thông điệp commit được sửa UTF-8,
tree/code/test bytes không đổi (mapping trong inventory). Evidence:
`C:/Users/ADMIN/AppData/Local/Temp/ras-m6d-p2-20261017`. Cleanup theo ownership, baseline và scan secret ghi trong inventory;
Dify/dev giữ nguyên. PR #62 vẫn OPEN, chưa merge.


Base đã fetch/xác minh: PR #61 merged `fb96354c4f8dc8fb8f4f8a54f1516d9d2e13625e`,
`3f07901` là ancestor, tree khớp. Source `0051 → 0052`, nhánh
`feat/m6d-private-item-delete-restore`; chưa merge. Chứng cứ nghiệm thu và hash ở
[inventory](m6d-test-inventory.json). Kết quả C4 trước merge giữ nguyên nguồn, không tính là một lượt kiểm mới.

## Chứng cứ kiểm cô lập trước sửa P2

Source chạy full inventory trước sửa P2: `3f0695b550e9a2e08617320596be0932171fe65b`;
migration `0052`. Image `rasc47507ea3a-api:local`,
`sha256:465f004562ce25848e3566b4454eaaf5a7e8a5e1ba92ec9502faa513f3015013`:
207 file app/migration/config khớp host; API/worker/beat/migrate/provision/recovery-init
cùng image, OS user `ras`, network ứng dụng internal. Inventory API mới tại source cuối
**5.250/5.250 ID pass**, 0 fail/error/skip/not-run; giữ đủ 5.163 ID C4 và 87 ID mới.
441 hash source trước/sau lượt cuối khớp; node/JUnit/artifact hash trong JSON inventory.
Lượt full bị ngắt sau 7 nhóm/1.852 ca có exit 0; chạy tiếp các file còn lại
tại cùng source, giữ log/JUnit của cả hai lượt. Số trên hợp nhất theo ID,
không gọi một invocation toàn xanh. Nhóm thiếu kết quả được chạy lại cả file.
Continuation ở source cuối có failure `test_revoke_can_commit_while_http_waits`: barrier chưa vào fetcher giả trong 10 giây. Chạy lại toàn file một lần cùng source với observer chỉ đọc đạt 6/6; outcome pages/records/requests đều 1, không có checkpoint error. Không đổi timeout/gate/clock/assertion; nguyên nhân chưa xác lập. Giữ failure/exit gốc và rerun riêng; tổng trên hợp nhất theo ID.
Lượt chính còn có một failure orphan process smoke: DELETE barrier không tới trong 60 giây, container dừng `orphan_clock_invalid` trước DELETE. Chẩn đoán toàn file một lần cùng source/guard/timeout đạt 1/1, SIGKILL exit137 rồi resume hoàn tất; observer clock chỉ đọc không ghi nhận lỗi. Nguyên nhân halt ở lượt gốc chưa xác lập; giữ riêng log container, clock, failure và rerun. Tổng hai failure của continuation không được đổi thành một invocation toàn xanh.

- Web: 72 test pass, typecheck và build exit 0; bytes web không đổi sau lượt này.
- Image default: 7 check pass, bootstrap/invitation thật, pricing chưa xác minh chặn
  paid call, zero provider calls, readiness giữ mở sau mutation hợp lệ.
- Image M6d: prepare 3, Chrome/delete 19, restore 70 check pass. Chrome thật
  `155.0.8059.79` qua Next proxy; lost committed response không resend POST và không
  có request ngoài origin. Backup dùng exported snapshot/pg_dump và checksum object.
- Hai vòng restore snapshot trước deletion, cộng snapshot sau purge có object từ
  artifact cũ được trả lại; mỗi vòng recover hai lần. Kiểm HEAD chứng minh mọi
  private key vắng trước start traffic/readiness 200; A/B/Admin bị từ chối, B cùng
  bytes và common vẫn đọc/fulltext được, B RAG trả câu trả lời có nguồn.
- Shared key trên MinIO thật: 9 check pass. Fixture tạo hai SourceFile của hai
  owner cùng key bằng migrator, mọi trigger bật; đây là setup kiểm, không là flow
  upload mới. Delete/cleanup dùng API/runtime CLI/image thật. Sau xóa A, B đọc đúng
  bytes; sau xóa tham chiếu B cuối, object vắng và writer mới bị fence. Common và
  B độc lập vẫn đọc được.
- Bảy countercheck bắt guard bị gỡ, khôi phục đúng bytes trước inventory: private
  SQL ACL, late derived, known-commit release, shared key, coverage sau purge,
  caller sau fsync lỗi và sweep object ở tombstone đã purged.

Evidence tại `C:/Users/ADMIN/AppData/Local/Temp/ras-m6d-20261016`.
Lượt `full-c129c5e` dừng ở 4.351 pass/1 fail (SQL offline); lượt `full-2ed16c4`
dừng ở các nhóm đầu để khóa coverage. Không tính các lượt này là inventory cuối.
Lượt `full-a7b57b0` tại application source trước đó bị gián đoạn, tiến trình không còn khi tiếp
tục phiên; giữ riêng 5 nhóm runner đã ghi exit 0/1.403 ID pass. File review writers
có JUnit đầy đủ nhưng chưa ghi nhận exit cũng được chạy lại cả file. Continuation
chạy mọi file còn thiếu; 441 hash trước lượt chính và tại lúc tiếp tục khớp nhau.
Không đổi một lượt bị ngắt thành exit 0 hoặc dùng các dòng dấu chấm làm kết quả.
Năm ca tái hiện caller trước sửa đều fail; kiểm sửa tập trung đã pass. Assertion
replay review được sửa theo semantics có sẵn `closed_by_recovery`; phép thử coverage
JSON ban đầu không chứng minh guard vì cột là enum text, ca hợp lệ `complete` đã
tái hiện fail trước sửa. Các lỗi fixture duplicate, wiring relay và URL readiness
của harness được giữ riêng; không ghi chúng thành lỗi sản phẩm hay pass cuối.

Container/volume và credential thử đã dọn theo ownership sau inventory; image
không còn consumer của task đã gỡ. Không còn container M6d. Chi tiết cleanup và
baseline nằm trong inventory/evidence.
Giữ Dify theo lựa chọn của chủ dự án; không thao tác dev `ras` hoặc sửa restart
drift có từ trước. Không prune Docker toàn máy.

## API và quyết định

`POST /library/items/{item_id}/delete`, JSON duy nhất `{ "confirm_delete": true }`.
Chỉ boolean true thực; thiếu, false, số, chuỗi hoặc trường lạ bị 422. Session,
Origin/CSRF và caller hiện hành được kiểm theo dependency chuẩn. Chỉ owner của
private item; common, ID vắng và ID trái quyền cùng 404. Không nhận key/file từ HTTP.

`t_del` là commit DB chấp nhận quyết định. Cùng transaction: `deleted_at` cuối,
epoch tăng một lần, bỏ active version/generation, metadata-only, hủy mọi job còn dở
và lease, signal cancel cả job đã xong, rút các request review còn mở, tombstone,
mirror và audit ID/count. Request đã approved giữ nguyên semantics; bản common,
rights, publication và job common độc lập không bị thu hồi.

Mọi kết quả append, kể cả fsync lỗi, đều kiểm lại binding/caller/clock trước commit.
Session hết hạn sau append làm rollback DB; prefix journal chỉ thu hẹp quyền khi
recover. Nếu caller/clock của receipt thất bại sau known commit, báo 503 chưa xác
định với client, giữ nguyên quyết định đã commit.

Receipt dùng transaction mới: `item_id`, `deleted=true`, `purge_due_at`,
`cleanup_status=pending|completed`. Receipt không cấp nội dung và không khẳng định
S3 đã xóa khi còn pending. Gửi lại thao tác đã biết là idempotent, kể cả sau purge;
không thêm event/epoch/job hoặc reset progress. UI xác nhận không chọn sẵn, nêu
không undo và common độc lập; success xóa nội dung đang hiển thị. Transport/5xx
được mô tả là chưa xác định, khóa nút và chỉ hướng dẫn tải lại thư viện; không tự POST lại.

| Cửa sổ | Kết quả |
| --- | --- |
| Từ chối trước append | Rollback; không journal mutation |
| Append thiếu/torn/prefix/full nhưng fsync lỗi; DB commit được | Commit restriction + toàn batch mirror false; 503 `deletion_journal_unavailable`; gate đóng |
| Append đã bắt đầu, final binding/caller/clock bị từ chối | Rollback DB, 503 `deletion_pending_recovery`; journal chỉ có thể replay hạn chế |
| COMMIT ném lỗi, dù landed hay rollback | 503 `deletion_outcome_uncertain`; không SELECT đoán, replay hoặc retry trong attempt |
| Known commit, teardown/response/disconnect lỗi | Giữ commit; 503 `deletion_response_unavailable`; không hứa rollback |
| Full line nhưng mirror false | Recover phải fsync; bytes khớp chưa đủ xác nhận |
| DB có tail mà journal thiếu | Giữ hợp đồng cũ: operator chỉ sửa journal khi xác nhận DB hiện hành; không dùng tùy chọn này để adopt snapshot cũ |

## Schema, caller và khóa

`private_item_deletions` không FK vào target/User: quyết định còn khi snapshot chưa
có item. Binding owner/item/epoch/time và manifest mọi version/file/job/key/checksum,
không title, filename, URL, lời khai hay text. Giới hạn
`retention.private_delete_manifest_limit` mặc định/tối đa 128 cho mỗi tập
version/file/closure; từ chối toàn thao tác trước append, không truncate. Parser
kiểm shape/UUID/order/duplicate/bounds/key/checksum/time; SQL dựng manifest độc lập
từ hàng đang khóa và kiểm NULL-safe. SQL mirror/confirm bind transaction/xmin.
`private_object_fences` append-only theo key; runtime/recovery chỉ SELECT, definer
ghi qua routine hẹp; fixed search_path, không PUBLIC EXECUTE. Reprovision cấp đúng
routine/grants. Downgrade chỉ cho khi không có lịch sử deletion/fence; không xóa quyết định.
Upgrade online và SQL offline đều chỉnh định nghĩa function ở PostgreSQL, giữ các
check/property cũ; kiểm parity function sau áp SQL xuất từ `0051:0052`.

| Caller | Gate/fence |
| --- | --- |
| Library/detail/count/page, files, metadata candidate/coverage/chunk/reference, collection và discovery citing-items | Predicate private `deleted_at IS NULL` trong SQL trước count/page |
| Keyword/vector/hybrid, viewer và RAG item/collection/all | Private SQL trước top-k, hydrate/scope/epoch/proof mới trước provider và response |
| Private original và review-original | Gate riêng kiểm lại sau spool/SHA-256 trước gửi; review còn cần exact open grant/epoch |
| Metadata/membership/review submit/approve/upload receipt | Gate private sống; SQL insert/transition fence không nối vào target đã xóa |
| Extraction/document index | Job cancel/lease/epoch/deleted gate ở claim/checkpoint/pre-send/store/activate |
| Citation backfill/discovery retarget | Khóa cả private/common item trước reference/citation; late item NOWAIT, SQL fence bắt write muộn |
| Late document/query/rerank/generation accounting | Nonce/day/cost settlement giữ một lần; không cấp quyền content, không ghi lại vector/hash/index |
| Normal CLI/Celery cleanup | Runtime thật, từng checkpoint có gate, fence commit + release trước S3 |
| Recover | Operator proof dương; replay restriction, sweep cả tombstone đã purged trước complete readiness |

Lock order acceptance: owner User → review request UUID → item UPDATE → job UUID →
version/file UUID → DML/FK/audit → journal exclusive. Nếu submit tạo thêm request
trong khi chờ item, từ chối attempt trước append; không khóa request mới sau item.
Approval request → item và worker User/item → job vẫn tương thích. Response item
SHARE → journal shared → scalar binding/caller/clock. Không lấy resource lock mới
sau journal.

Cleanup prepare chỉ scalar + key advisory `1918989163/hashtext(key)` → journal;
SourceFile INSERT dùng cùng key serialization và thấy cả common/orphan/private
fence, READ COMMITTED bắt buộc. Finalize tombstone → review request UUID → item → derived/metadata rows →
journal, không key. Pre-delete proof mới đã release sạch; HEAD/DELETE/HEAD ngoài
mọi connection của cleanup. Recovery cũng trả connection giữ orchestration mutex
trước private sweep; `recovery_begin` bền vẫn đóng readiness. Sau sweep, run phải
lấy lại mutex và cùng open recovery ID trước verify/complete; run khác hoàn tất
trước thì run này từ chối `recovery_scope_changed`, không ghi marker end thứ hai.

## FK, scrub và chia sẻ object

| Dữ liệu | Sau purge |
| --- | --- |
| library item | Skeleton ID/owner/visibility/deleted epoch/time; title và active pointers NULL |
| versions | ID/item/time phục vụ FK; label/source identifier/source URL/coverage NULL |
| files | ID/item/version/job/key/checksum/MIME/size/page-count/time giữ FK/đối soát; filename/file_source NULL |
| jobs/attempts | Skeleton UUID/idempotency/scope/state/counters/time/error code; đóng attempt/job dở, bỏ lease; source_label/warnings scrub |
| upload intent/acknowledgement | Used intent/consent binding, ID, token hash, checksum/size/kind/time giữ chống retry tạo item; filename NULL |
| review request | Target/requester/checksum/status/reason/time và binding common đã approve; mọi declaration URL/text/rejection note NULL |
| reference/citation/index/generation/chunk/section/page/candidate | Xóa theo FK: link → reference → entry → generation → chunk → section → page → candidate |
| embedding ledger | Giữ nonce/job/day/model/provider/token/cost/status; vector/chunk_hash NULL và content_removed_at cuối |
| membership | Xóa mọi membership của target |
| tombstone/journal/fence/audit | Giữ quyết định ID/code/count/checksum/technical key, không sao chép metadata đã scrub |

Private scrub không dùng retention exception của common. Migration chỉ cho phép
null các trường bị xóa của target đã đóng; không cho rehydrate. Review/intent sống
vẫn bắt buộc declaration/filename ở SQL INSERT. `purged_at` chỉ ghi sau hậu điều
kiện dẫn xuất/metadata/membership và mọi bước object cần thiết; không reset cột này
để né restore. Batch/deadline/poll dùng retention hiện hành (50/24h/900s), chưa là SLO đo được.

Key còn bất kỳ tham chiếu hợp lệ toàn DB (B/private sống, common pending/listed,
file không xác định item) → shared_kept, giữ object. Cùng checksum/key khác không
liên quan. Không còn tham chiếu hợp lệ → fence vĩnh viễn rồi xóa; target file absent
vẫn dùng manifest journal. Storage lỗi giữ pending, attempts/error code và fence;
tick sau retry. Commit uncertain/release lỗi dừng trước I/O tiếp theo hoặc success.
S3 là at-least-once, không rollback object cùng DB, không hứa exactly-once DELETE.

## Restore và vận hành

Giữ C4 readiness thật. Dừng writers, hold bằng binary đang phục vụ trước migrate;
upgrade `0052`, reprovision; restore qua `ras_backup.py` với journal **hiện hành**
ngoài artifact; recover rồi mới start traffic. Không patch checkpoint/receipt/gate.
Replay closure trước deletion, kiểm present-record lẫn target tuple; không dựng
User/item/file/credential/rights/common đã vắng. Binding trỏ owner/item/file khác
đóng recovery trước xóa. Sweep chạy cả khi DB có `purged_at` để dọn object restore
trở lại. Backup không bắt original của private đã deleted còn tồn tại; kiểm này
dùng JSON row để vẫn đọc được schema trước 0052.

Harness: `scripts/m6d_image_drill.py --context <C4 context ngoài repo>
prepare|delete|restore`; phase default/controlled provider dùng `c4_stack.py` và
`c4_http_flow.py`. Restore dùng pg_dump + object artifact thật, hai vòng snapshot
trước delete và thêm snapshot sau purge với object cũ trả lại. API/worker/CLI cùng
image, Chrome thật qua Next proxy. Inventory runner `m6d_inventory.py` giữ full
collection và đối chiếu 5.163 ID C4; raw fail/rerun giữ riêng. Không đọc `.env`,
query/migrate/restart dev, arXiv/provider hoặc pricing thật.

## Duplicate/retry và giới hạn

*Sau M6e (nhánh review): gap cảnh báo ready/indexed bên dưới đã được xử lý theo [handoff M6e](m6e-private-duplicate-retry.md); đoạn này mô tả source M6d.*

F07 còn gap: ingest hiện chỉ cảnh báo duplicate trong cùng owner khi file trước ở
`stored/extracting/chunking/awaiting_index`; bản trước đã `ready/indexed` không nằm
trong query cảnh báo. Cảnh báo tạo job `duplicate`, chưa liên kết/reuse derivatives
hay canonical version graph. Không nhận diện paper tương đương khác bytes. M6d
lọc target đã deleted khỏi query này; retry job đã hủy không hồi sinh; upload mới
cùng bytes cần consent/intent và item/key mới. Không coi hồi quy an toàn retry là
đã hoàn thành toàn F07; không thêm canonical merge/trash/undo/streaming.

Bytes đã qua proof cuối/gửi client/provider không lấy lại được; không cam kết đóng
khe mạng proof → send. Provider fixture không xác minh charge/model routing/dimension
hay semantic production. DB và journal cùng rollback vẫn ngoài khả năng phát hiện.
Fault injection/SIGKILL không chứng minh chịu mất điện; deployment/storage/retention
backup production và provider thật tiếp tục thuộc gói D/E.


Continuation ở application source `a7b57b0` hoàn tất **3.837 pass/10 fail**,
giữ riêng khỏi inventory mới: bảy ca thuộc giả định upgrade trong bốn file test
(JSON toàn hàng có thêm `private_deletion_id`, hoặc thiếu reprovision routine mới).
Phép thử sửa vẫn so đủ mọi cột lịch sử, buộc binding mới NULL, reprovision và
kiểm C4 thật trước business write. Một smoke proxy khác fail trước request vì
cổng cố định 58589 nằm trong dải Windows loại trừ 58587–58686; harness chuyển
sang bộ chọn cổng C4, không sửa mạng hệ thống hoặc assertion. Tổng 29 ca của
năm file đạt trước full inventory mới. Application/migration/web bytes giữ
nguyên; chỉ năm file test đổi sau `a7b57b0`.
Hai ca còn lại thuộc citations/discovery: một outcome `discovery_lease_lost`,
một lượt không ghi record. Chẩn đoán hai ca cùng source/clock thật đạt nhưng
chưa xác lập nguyên nhân; không gọi là lỗi baseline hoặc quy cho Windows.
Lượt full mới có observer chỉ đọc clock/checkpoint/outcome ở hai file đó;
không nới lease/gate, đổi clock hoặc thêm retry/fallback. Log/JUnit lỗi, observer
và kết quả các lượt riêng đều giữ trong inventory.
