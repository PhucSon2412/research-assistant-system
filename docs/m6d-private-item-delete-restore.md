# M6d — xóa toàn item private và restore

Base đã fetch/xác minh: PR #61 merged `fb96354c4f8dc8fb8f4f8a54f1516d9d2e13625e`,
`3f07901` là ancestor, tree khớp. Source `0051 → 0052`, nhánh
`feat/m6d-private-item-delete-restore`; chưa merge. Chứng cứ nghiệm thu và hash ở
[inventory](m6d-test-inventory.json). Kết quả C4 trước merge giữ nguyên nguồn, không tính là một lượt kiểm mới.

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
| Append đã bắt đầu, final proof/commit bị từ chối | Journal chỉ có thể replay hạn chế; không success |
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
fence, READ COMMITTED bắt buộc. Finalize tombstone → item → derived/metadata rows →
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
