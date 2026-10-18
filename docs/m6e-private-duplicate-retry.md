# M6e — upload trùng trong kho riêng và retry an toàn

Nhánh `feat/m6e-private-duplicate-retry`, base `origin/main = d9bec94c63fd8f27a8897f4f83ce8f6a063d26dc`
(merge PR #62; head review `15f1dfa` là ancestor, tree khớp). Source migration **0052**, **không
thêm migration/grant/schema**. [Task](tasks/m6e-private-duplicate-and-retry.md) ·
[inventory](m6e-test-inventory.json). Chưa merge.

## Hợp đồng MVP (orchestrator chọn theo ủy quyền)

Không phải từng lựa chọn kỹ thuật đã được chủ dự án xác nhận trực tiếp.

- **Cùng file** = SHA-256 chính xác của file đã được worker đọc lại/kiểm, chỉ trong cùng owner,
  private, origin `upload`. Metadata/title/DOI/arXiv ID không thay checksum. Owner khác và bản
  chung không được đọc để quyết định hay hiển thị.
- **Retry** (cùng intent, cùng job, broker redelivery, lease takeover) không tạo thêm
  item/version/file/job/derived/generation/reservation/call ngoài hợp đồng một lần sẵn có.
- **Intent/consent mới** là một upload riêng: giữ item + original mới, job dừng `duplicate` ở
  bước validate, trước extraction/index/provider; không hợp nhất, không đổi owner/quyền/
  collection, không chép chunk/vector/citation, không tạo reservation/cost.
- **Điều hướng**: item detail của owner trả `existing_copy` cho job `duplicate`, tính lại ở
  mỗi lần đọc. Chỉ để điều hướng; route đích kiểm ACL/deletion bình thường. Không còn đích hợp
  lệ thì trả `null`, không trả ID/title/URL cũ. Không có endpoint retry/recovery/canonical mới.

## Bảng eligibility (`app/ingest/duplicates.py`)

Một predicate dùng chung cho worker validate và DTO. Điều kiện chung: cùng checksum, khác file
đang xét, `SourceFile.job_id` = job, file/version/job/item cùng binding, job `upload`/private,
`scope_owner_user_id` = item owner = owner đang xét, `cancel_requested=false`,
`rights_epoch_at_start` = epoch item, item private `deleted_at IS NULL`, **không** có
`private_item_deletions` hoặc `deletion_tombstones` cho item (kể cả chưa purge).

| Trạng thái job đích | Đích hợp lệ khi |
| --- | --- |
| stored, extracting, chunking, awaiting_index, indexing | chưa finished; item `metadata_only`, chưa có active generation |
| retryable_failed | resume `stored/extracting/chunking/indexing`, chưa finished, item `metadata_only` |
| ready | item `indexed`, `active_version` = version file; active generation `state=active` đúng job/file/version/item/epoch |
| received, validating, retryable_failed→validating | không (chưa qua validate) |
| duplicate, cancelled, terminal_failed, fulltext_rejected, needs_review | không |

Nhiều ứng viên: chọn job sớm nhất `(created_at, id)`; không gọi là canonical. Duplicate không
bao giờ là đích nên không có chuỗi A→B→A. Khi mọi bản cùng bytes đã xóa (kể cả purge), upload
mới với intent/key mới xử lý độc lập. DTO có thể trỏ tới một bản sống khác bản worker đã thấy
lúc validate (ví dụ bản đầu bị xóa, còn bản cũ hơn) — đây là điều hướng hiện hành, không phải
liên kết bền.

## Lock map và race

Không thêm khóa mới trong worker. Validate giữ thứ tự cũ: owner User `NO KEY UPDATE` → item →
job → version/file SHARE → advisory `owner:sha256` → đọc đích **không khóa** → journal. Không
lấy khóa đích sau advisory. Private delete khóa owner User trước review → item → job, nên delete
cùng owner và checkpoint validate tuần tự hóa qua khóa User:

| Thứ tự | Kết quả |
| --- | --- |
| Delete commit trước SELECT đích (kể cả giữa storage read và checkpoint) | Đích bị loại; job mới xử lý độc lập |
| Validate giữ checkpoint, delete tới | Delete chờ khóa User; validate commit `duplicate`; delete commit sau; DTO trả `null` |
| Hai validate cùng owner/hash | Tuần tự (User + advisory); bản commit trước là `stored`, bản sau `duplicate`; không 40P01 |
| Lease takeover / redelivery | Lease cũ `lease_lost` không ghi; job đã terminal → `not_claimed` |

Đọc item (`resource_json`): `item_ids` gồm cả `existing_copy.item_id`, nên boundary khóa
SHARE item + đích theo UUID với `visible_items_clause` rồi đọc lại. Delete commit trước khóa →
lần đọc mới bỏ link (retry trong boundary); đọc đang giữ SHARE → delete chờ ngay ở hàng item đích
(trước bước journal; test kiểm khóa đang chờ không phải advisory), response có link hợp lệ tại
thời điểm đó, request sau 404. Khóa journal shared của boundary vốn cũng tuần tự hóa với writer;
lượt countercheck đầu cho thấy test cũ không phân biệt hai khóa này nên đã siết test. Cleanup tombstone → review → item không chạm advisory/User
của worker; bản sửa P2 M6d giữ nguyên.

## API/UI

`GET /library/items/{id}` → mỗi job có `existing_copy: {item_id, title, content_status} | null`
(chỉ khác null với job `duplicate` + cảnh báo `duplicate_in_private_library` trên item private
của chính caller). Admin/owner khác vẫn 404 như trước; không thêm nhánh Admin.

Web (`web/lib/duplicates.ts`, trang item): thông báo tiếng Việt “trùng chính xác (cùng SHA-256)”,
nói rõ file gốc mới được giữ nhưng không trích xuất/lập chỉ mục/gửi provider, không liên kết hay
dùng lại nội dung; link “Mở bản hiện có …” chỉ dựng từ UUID API trả. Không còn đích: nói bản trước
không còn hợp lệ, không link. Item duplicate vẫn nhãn “Chỉ metadata”. Upload form giữ consent
không chọn sẵn, nút khóa khi đang gửi, unknown outcome không tự gửi lại.

## Phần F07 đã khép và phần later

Khép theo hợp đồng exact-byte MVP: nhận diện cả bản ready/indexed, cảnh báo có điều hướng theo
ACL hiện hành, retry/redelivery/lease không tạo tác dụng phụ lặp. **Later, chưa làm:** canonical
graph/version, nhận diện paper khác bytes, reuse dẫn xuất giữa item, xử lý lại một duplicate khi
đích sau đó lỗi/bị xóa (owner xóa duplicate rồi upload lại bằng intent mới). Bản lỗi
(`fulltext_rejected/needs_review/terminal_failed`) không là đích nên upload lại cùng bytes chạy
pipeline lại (không gọi provider nếu không tới index).

## Kiểm chứng cô lập

Conda `ras`, PG/MinIO/Redis/journal/project/port riêng, outbound HTTP chặn; không đọc `.env`, không
thao tác dev `ras`/Dify, không provider/arXiv/pricing thật. Hash từng artifact trong
[inventory](m6e-test-inventory.json); evidence ngoài repo `C:/Users/ADMIN/AppData/Local/Temp/ras-m6e-20261018`.

- **Trước sửa:** test file cuối chạy trên worktree `d9bec94` (chỉ thêm module predicate để
  import): **31 fail / 8 pass**; ca ready/indexed Markdown và PDF trả `awaiting_index` thay vì
  `duplicate`, DTO thiếu `existing_copy`. Lượt đầu với bản nháp test (31 fail / 6 pass) giữ riêng.
- **Phát triển:** lượt sau sửa đầu 29 pass / 10 fail, cả 10 do kỳ vọng test sai title (title thật
  lấy từ tên file); sửa kỳ vọng, chạy lại 10 ca → 10 pass. Không đổi code sản phẩm vì lỗi này.
- **Selection theo rủi ro** (`scripts/m6d_inventory.py --files …`, 5 shard tmpfs, image API
  thật cho process smoke): **1.462/1.462 pass**, 0 fail/error/skip, 22 file, collection 5.298,
  **3.836 not-run**; source trước/sau khớp. Gồm file M6e (39), ingest/PDF/markdown/upload/intent/PUT,
  worker/common extraction, document/common index (completed/uncertain call), private index,
  library ACL/resource reads/final clock, M6d delete/races/**9 ca P2 lock order**, review approval,
  C4 contract và hai process smoke. Không chạy lại full >5.000 ID (không đổi schema/grant/gate).
- **Countercheck:** lượt 1 bắt 3/5; `duplicate-as-target` là mutant tương đương (job duplicate luôn
  finished) và test khóa đọc chưa phân biệt khóa hàng với khóa journal shared. Đổi mutant, siết test
  (delete phải chờ khóa không-advisory); lượt 2 **5/5 bắt được**, bytes khôi phục và kiểm hash.
- **Web:** 75/75 test (3 mới), typecheck/build exit 0.
- **Image + Chrome qua Next proxy:** API/worker/beat `rasc40e859645-api:local`
  (`sha256:5add3da9…`, 208 file app/migration/config khớp host), web `rasc40e859645-web:local`;
  provider giả test-only có kiểm soát. HTTP default: lượt 1 fail vì bỏ bước `fake-off` của harness,
  lượt 2 fail deadline 60 s ngay sau recreate (job tới `awaiting_index` ngay sau đó), lượt 3 **4/4**
  (zero provider call). Smoke M6e **19/19**: A indexed; B cùng bytes độc lập; consent không chọn sẵn,
  double click chỉ 1 intent/1 PUT; duplicate có cảnh báo và mở đúng bản A; PUT mất response được
  báo chưa xác định, không gửi lại, đúng một item landed (cũng duplicate); duplicate 0 provider call;
  sau xóa A không còn link/ID cũ, link đã lưu 404; B vẫn đọc được; 0 request ngoài origin; readiness mở.
- **Cleanup/restore:** không binding/schema/journal mới; dựa vào đường M6d đã có và hồi quy M6d trong
  selection. Không chạy lại full backup/restore drill.

Hai failure lịch sử M6d (discovery barrier/orphan clock) nằm ngoài selection, không tái hiện và chưa
rõ nguyên nhân. Dọn container/volume/credential theo ownership; giữ hai image task để review.
