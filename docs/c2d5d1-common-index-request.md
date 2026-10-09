# C2d-5d1: yêu cầu lập chỉ mục bản chung qua runtime

**Mốc sau review:** PR #50 đã merge `03b7985`; kết quả giữ theo source trước merge, không rerun. [C2d-5d2a worker runtime/extraction](tasks/m6c2b2c2d5d2a-worker-runtime-extraction.md) là task tiếp đã giao, chưa triển khai/kiểm; provider/index C2d-5d2b và task background khác còn riêng. Safety pending tới C4.

## Review độc lập tại ff5fa82

**Không phát hiện điểm chặn merge mới trong scope một POST index request.** Reviewer thực chạy **200/200 API** (171 mới + 28 migration/ORM + Chrome), **70/70 web**, typecheck/build; 281 source hashes trước/sau khớp. Artifact 923 API/9 mutant và bốn lỗi baseline được đối chiếu riêng, không tính là reviewer chạy lại. [Report đầy đủ](reviews/pr50-c2d5d1-common-index-request.md). PR đã merge **`03b7985`**; chưa rerun tại merge, source `0042`, **safety pending tới C4**. Bốn ca baseline cần xử lý trước tổng hồi quy/C4; worker/provider vẫn riêng. Số agent bên dưới giữ theo source `e283bc6`, không thành pass mới tại head.

[Task](tasks/m6c2b2c2d5d1-common-index-request.md) · [Inventory](c2d5d1-test-inventory.json) · [Revoke](c2d5c2-common-revoke.md) · [Publish](c2d5c1-common-publish.md) · [Caller map](c2d1-caller-inventory.json)

Base đã fetch/xác minh: `origin/main` = merge PR #49
`859dfe9793dd9549e3ca9b8f0102361f414d0340`; nhánh
`feat/m6c2b2c2d5d1-common-index-request` tạo từ đúng merge này. Docs
orchestrator/reviewer chưa commit (review PR #49, cập nhật merge, caller map,
task này) được commit nguyên trạng ở `16eda35` trước mọi thay đổi khác.
Implementation và test ở `e283bc6400efdefd4641838ce4d94f4ccee3baaa`; các
commit sau chỉ docs. Source migration vẫn **`0042`**, **không migration, không
đổi grants**; không suy revision DB dev. **Safety pending tới C4; chỉ một POST
index được chuyển.** Worker/ingest/index/provider dispatch là C2d-5d2.

## Quan sát tại base và mâu thuẫn đã xử lý

- Route cũ dùng `get_db`/`get_now` đầu request, gọi `common_index.start` tự
  commit (cả nhánh reuse), nhánh `IntegrityError` rollback rồi SELECT để trả
  job, `_refuse` ghi audit denial bằng principal của dependency, response
  `_copy_out` trên cùng Session với principal/thời gian cũ. Không có actor/clock
  sau SQL cuối, không journal lock, không response proof mới.
- **Đã tái hiện bằng test** (lượt chuẩn bị `prep-repro-base-literal`, route cũ):
  `confirm_external_processing: Literal[True]` nhận `1` và `1.0` qua HTTP và
  tạo job thật (200). Đã đổi sang `StrictBool` + validator true như publish/revoke.
- Form index cũ không có busy/ref guard; Chrome mutant bỏ ref guard gửi **hai
  POST** cho hai submit cùng tick (mutant `web-ref-guard`).
- `common_index.start`/`_refuse`/`Started`/`_audit` không còn caller nào sau khi
  chuyển route; **đã gỡ** để không thành fallback tự commit. `block_reason`,
  `live_job`, `latest_job`, `status`, `REFUSAL_CODES` giữ nguyên và được worker,
  GET formatter và boundary mới dùng chung (chỉ đọc, không commit).

## Boundary và thứ tự khóa

`api/app/api/common_index_request.py` sở hữu Session/executor/outcome của
attempt (cùng mẫu `_thread`/`receive` của revoke). Route chỉ giữ Origin bắt
buộc, `require_admin`, body strict và chuyển `request/admin/item_id/clock/settings`.

Acceptance (một transaction, Session riêng):

1. `require_writer` (integrity gate trên connection riêng) → `SET LOCAL lock_timeout`.
2. Admin User `NO KEY UPDATE` qua `ras_auth_lock_users` → current Admin/session/
   head/receipt/generation/status/role/CSRF/clock (`common_publish._current`).
3. Item ID sai định dạng/không thấy: `require_final(writer=True)` + current actor
   rồi `404`. Common item `FOR UPDATE` (`publication._copy(lock=True)` qua
   `review_copy_admin_clause`).
4. `FOR SHARE` theo đúng thứ tự revoke: rights UUID → publication → jobs UUID →
   version UUID → file UUID. **Không khóa tombstone** (cleanup lấy tombstone →
   item); tombstone là predicate trong `block_reason`.
5. `block_reason`: `listed`, không revoked/tombstone, đúng một rights sống ở
   epoch hiện tại, đúng file/version/checksum/size của publication sống,
   `instance_users`, `permits_fulltext_search` **và** `permits_external_processing`,
   không `scope_conditions`, MIME PDF/Markdown. Không suy quyền từ checksum
   trùng, private item hay `indexed` của item khác.
6. Live job của file: chỉ trả lại khi origin/scope/owner NULL/item/version/file/
   epoch khớp và chưa finished; lệch → `409 index_job_binding_changed`; đã yêu
   cầu hủy → `409 index_job_cancelling`. Không ghi đè actor/state/lease/cancel,
   không attempt mới, không broker/provider. Không có live job: giữ
   `already_indexed` và `extraction_rejected` (needs_review/fulltext_rejected),
   rồi INSERT job `promotion/common`, owner NULL, actor = current Admin,
   key `promotion:{file}:{n}`, label `admin_index`, thời gian lấy **sau** mọi lock
   wait; audit success chỉ `job_id`/`source_file_id`.
7. `flush` + `SET CONSTRAINTS ALL IMMEDIATE` (FK/unique/audit trước journal) →
   snapshot job → `require_final(writer=True)` (**exclusive trực tiếp**, không
   shared rồi nâng) → trong `no_autoflush`: `_binding` (item/tuple eligible lại,
   job row đúng snapshot, đúng một live job; job mới phải là `received` của
   actor hiện hành) → current actor/clock **sau SQL cuối** → kiểm `stopped` →
   commit. Không khóa User/resource mới sau journal; không DML trong formatter.

Không tạo bản ghi journal cho job (không phải sự kiện thu hồi); journal lock chỉ
là gate. Không áp ngoại lệ restriction-only của revoke: mọi lỗi trước commit
rollback. Denial nghiệp vụ (`REFUSAL_CODES`, `already_indexed`,
`extraction_rejected`) ghi audit `denied` **best-effort** trong transaction
writer riêng có current proof (`refusal_audit`), chỉ `reason_code`; lỗi audit
không đổi denial, không retry. ACL/input/auth denial không audit.

## Commit matrix và response

| Cửa sổ | Kết quả |
| --- | --- |
| Auth/gate/binding/DML/FK/unique trước commit | Rollback; denial hiện hành hoặc `503 index_start_unavailable`/`index_binding_changed`; không job/DTO |
| IntegrityError ngoài luồng dedup đã khóa | `503 index_start_unavailable`; không retry, không SELECT để trả job |
| Commit ném lỗi (landed hoặc rollback) | `503 index_start_outcome_uncertain`; outcome giữ `commit_started`; không IDs/SELECT inference/replay/retry/xóa job |
| Known commit, teardown/response proof lỗi | Giữ job/audit; denial hiện hành hoặc `503 index_start_response_unavailable` |
| Known commit + proof/release sạch | `200 {item, job:{id,state,cancel_requested}, created}` |

`Outcome` giữ `commit_started`/`committed` độc lập rollback/close/invalidate
(`_release` thử cả hai, invalidate khi lỗi). Response: Session/transaction mới,
common item SHARE → rights/version/file SHARE → **đúng job** SHARE → journal
shared → current Admin/clock → `_copy_out` + cleanup decoration → đọc lại job
(đúng item/version/file/origin/scope) → current caller/clock sau SQL cuối →
release sạch. Job có thể đã `validating`, cancel-requested, revoked hoặc
scrubbed: DTO phản ánh trạng thái hiện hành, `created` là fact của attempt.
Item/job không còn chứng minh được → `409 resource_scope_changed` sau current
proof; denial formatter muộn cũng kiểm actor mới. Response mở rộng tương thích:
thêm `job.cancel_requested`; `created=true` không có nghĩa đã index hay provider đã chạy.

Cancellation: trước commit, `stopped` → rollback, không job; sau khi đã vào
response, attempt giữ connection tới thread settle và không trả DTO khi đã ngắt
(kiểm `stopped`/disconnect cả trước lẫn sau response). Không tự POST lại.

Endpoint không I/O storage/parser/Redis/broker/provider và không hash object.
Thiếu key/`pricing_verified=false` vẫn tạo job (`provider_ready=false`). Job DB
bền vững là hàng đợi: beat `ras.ingest.discover` tìm lại qua `due_job_ids`.
Job là công việc ứng dụng theo rights/epoch của bản chung, không gắn tuổi
session Admin; actor ID chỉ là attribution.

## Web và proxy

`IndexForm`: checkbox rỗng ban đầu, ref đồng bộ + `busy` disable checkbox/nút,
hai submit cùng tick chỉ một POST. Thành công chỉ khi body đúng item/job/`created`
boolean; sau đó bỏ tick và refresh từ GET (lỗi refresh do danh sách tự báo, không
thành “POST thất bại”). Lỗi luôn bỏ tick. Transport/502/504/body thiếu/
`index_start_outcome_uncertain` → “Không xác định được kết quả… job có thể đã được
tạo… tải lại trạng thái; không tự gửi lại”. `index_start_response_unavailable`
nói đã lưu. Proxy giữ **normal 15 s** cho đúng route index (request chỉ DB);
không thêm pool/deadline/config.

## Caller map và legacy

- Đã chuyển: `POST /admin/common-copies/{item_id}/index` → `common_index_request.receive`.
- `common_index.start` đã gỡ; không caller legacy nào còn tự commit job index.
- Fixture tạo job trong test cũ: `test_common_index` tạo job qua HTTP runtime
  route mới; vài test dùng INSERT owner engine có nhãn (job SQL-bypass cho worker,
  job live có sẵn, job finished) — đó là setup/adversary, không phải writer HTTP.
- Worker/beat/cleanup/reconcile, search/rerank/RAG provider và C3/C4 **chưa chuyển**.

## Bàn giao C2d-5d2: checkpoint worker còn mở

Không dùng Admin session của POST đã xong làm quyền: worker phải kiểm quyền bản
chung tại thời điểm xử lý. Hiện trạng (chỉ kiểm tương thích trong PR này):

| Entry/checkpoint | Engine/role hiện tại | Khóa/gate hiện tại | Còn mở cho C2d-5d2 |
| --- | --- | --- | --- |
| Beat `ras.ingest.discover` → `due_job_ids` → `ingest_job.delay` | `get_sessionmaker()` theo `RAS_DB__*`; Compose worker/beat dùng `POSTGRES_USER` (owner) | `recovery.gate_open(..., task)`; SELECT không khóa | Role runtime/worker riêng; `auth_safety.cutover`/integrity gate như HTTP; broker chỉ mang ID |
| `ras.ingest.process` → `process_job` | như trên | `gate_open('worker')` một lần đầu; check key/pricing đọc job không khóa | Gate theo từng bước và trước mọi I/O; quyết định provider_ready/pricing không mở safety |
| `claim_job` | như trên | job `FOR UPDATE SKIP LOCKED`, **không khóa item**; `attempts_exhausted` set finished không item lock | Thứ tự item → job như HTTP/revoke, hoặc chứng minh an toàn; lease token/until theo clock DB/app |
| `_begin`/`_finish`/`_fail` qua `_locked` | như trên | item `FOR UPDATE` → job `FOR UPDATE` → lease → `_context`/`_guard`/`block_reason` | Journal shared/gate dưới khóa, fresh clock sau SQL cuối; không giữ khóa khi I/O storage |
| `_validate`/`_extract`/`_chunk` (đọc object) | như trên + `OriginalStorage` | đọc object ngoài khóa, ghi kết quả dưới item→job | SHA-256/size gắn với rights checksum; fence dẫn xuất `0024` |
| `indexer.process_index` (embedding provider) | như trên | `gate_open('indexer')` trước mỗi call; `_check_unchanged` + rehash trước call/công bố; `block_reason` dưới khóa | Provider dispatch boundary, budget/kế toán (owner NULL), `pricing_verified` vẫn tắt; không gọi OpenRouter trong test |
| `ras.revocation.cleanup` → `cleanup_revoked` | owner engine | tombstone → item, xóa dẫn xuất/object | Role/gate cleanup, không đổi hợp đồng tombstone |

Race đã quan sát được bằng test thật trong PR này: legacy worker item→job chờ
index (và ngược lại) không deadlock; claim SKIP LOCKED sau khi index nhả khóa
vẫn claim được job; revoke thắng sau acceptance đặt `cancel_requested` (test chỉ
khẳng định legacy `process_job` trả `cancelled`, `not_claimed` hoặc
`recovery_gate_closed`). Đây **không** phải chứng nhận worker
đã chuyển sang hợp đồng auth safety mới.

## Kiểm chứng

Xem số liệu, selection, JUnit/hash và not-run đầy đủ ở [inventory](c2d5d1-test-inventory.json).

Tất cả nhóm chạy tại `e283bc6` (source hash 299 file api/web trước/sau suite khớp, không sửa source khi chạy):

| Nhóm | Pass |
| --- | ---: |
| Core: 171 index-request mới + Chrome index + 28 migration/ORM | 200 |
| Revoke runtime + Chrome revoke (route module đổi) | 171 |
| Publish runtime + Chrome publish | 192 |
| GET review/publication (formatter `_copy_out` dùng chung) | 260 |
| Content cũ: common index/search/RAG, publication, cleanup, original, health | 89 |
| Recovery chọn theo journal/revocation | 11 |
| **Tổng API unique** | **923/923** |

0 fail/error/skip; selection khớp JUnit, không trùng node. Collect **4.268**, **3.341
not-run** (không tính pass). Bốn node cũ fail y hệt ở base `859dfe9` (chạy từ
`git archive` cùng môi trường) và ở `e283bc6`, nên được loại khỏi nhóm content và
chạy riêng: `test_common_search` ×2 (quyền `session_replication_role`),
`test_health::test_readyz_ok_after_migration` (đòi 200 khi safety pending),
`test_common_original::test_common_original_refused_unless_live_rights_open_that_exact_file`
(helper revoke trả 409). Không thuộc phạm vi, không sửa, nguyên nhân chưa điều tra
sâu hơn assertion.

Web **70/70** (66 cũ + 4 mới), typecheck và production build đạt tại `e283bc6`.
Chrome thật qua Next production proxy tới Uvicorn socket: unticked không POST; hai
submit cùng tick một POST khi acceptance bị giữ; create rồi refresh hiện job;
POST chủ động lần hai nhận cùng job (`created=false`); known commit + response giữ
quá 15 s → proxy 504 → UI “không xác định”, không re-POST; qua proxy: `1` → 422,
User 403, session hỏng 401 xóa cookie, Admin mất role 403.

**9/9 mutant đối chứng bắt assertion** (0 setup error), mỗi mutant XML/log riêng,
source khôi phục và kiểm hash: literal coercion, clock sau SQL cuối, outcome fact
bị teardown ghi đè, reuse không kiểm binding, bỏ khóa item (hai Admin), bỏ actor
check ở denial formatter, bỏ kiểm `stopped` trước commit, đảo thứ tự khóa, và web
bỏ ref guard (Chrome thấy hai POST). Mutant `item-lock` bị bắt ở assertion quan sát
lock wait của Admin thứ hai (observer hợp đồng khóa), không ở kết quả nghiệp vụ.
Mutant chạy trên working tree trước commit, chỉ khác `e283bc6` ở thứ tự import của
`page.tsx`; mọi file API trùng byte.

Lượt chuẩn bị/tái hiện và lỗi runner được giữ riêng, không cộng: tái hiện `1`/`1.0`
tạo job ở route cũ; sáu lỗi setup test lượt đầu (CSRF trước auth, CHECK thật,
observer chờ advisory, filename vốn null); regex Playwright có `/`; lượt mutant đầu
trên file lỡ bị ghi CRLF (đã chuẩn hóa LF, chạy lại toàn bộ); lượt content đầu của
suite cuối dùng `--deselect` sai tiền tố nên dừng ở node lỗi có sẵn (28 pass) — đã
sửa runner, thu thập lại và chạy nhóm content mới.

Cleanup: gỡ đúng **2 container/2 volume** label `ras.task=c2d5d1-20261009` sau kiểm
sole consumer/anonymous; xóa ba file credentials; **9 container dev giữ nguyên
ID/image/StartedAt** so với snapshot đầu (lấy trước mọi thao tác). Port PG/MinIO/
API/Next đã đóng. Quét fingerprint credentials sinh ra: 0 hit trong file đổi và
evidence. Evidence ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5d1-20261009`
(manifest `evidence-manifest.json`); không commit journal/paper/credentials/artifact.

## Giới hạn

- Không full business cutover/C3/C4, không mở safety ready; business cutover chỉ
  scoped trong fixture, bài HTTP pending riêng giữ gate production thật.
- Không Compose API image/deployment; Chrome qua Next production build + Uvicorn
  socket chạy working tree. Lost response do barrier trong tiến trình API giữ
  response quá 15 s (không phải lỗi mạng thật).
- Fault injection commit/teardown không phải bằng chứng crash/mất điện thật.
- Không gọi provider/OpenRouter, không đổi `pricing_verified`; HTTP ngoài bị chặn.
- Không benchmark/SLO; thời gian test chỉ mô tả fixture.
- Worker/cleanup/search/RAG chỉ kiểm tương thích hiện hành.
