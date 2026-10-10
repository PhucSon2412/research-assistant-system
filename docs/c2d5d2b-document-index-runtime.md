# C2d-5d2b — document embedding/index qua runtime

**Bàn giao sau merge PR #52 `ba20fda`:** [task C2d-5e1 cleanup common revoked runtime](tasks/m6c2b2c2d5e1-revoked-common-cleanup-runtime.md). Normal cleanup tách DB/S3 và thêm fence shared key; operator recovery giữ đường riêng. Kết quả dưới đây giữ source trước merge, không rerun tại merge; safety pending tới C4.

**Review độc lập PR #52 tại `97bc6e7` (2026-10-10):** không phát hiện điểm chặn merge mới trong scope document index runtime. Reviewer **254/254 API**, gồm shared index/search và worker/beat Linux thực; 302 source hashes khớp. Artifact agent 703 API/8 countercheck kiểm riêng. PR đã merge `ba20fda`, chưa rerun tại merge; source `0043`, safety pending tới C4. [Report, cleanup và giới hạn](reviews/pr52-c2d5d2b-document-index-runtime.md).

[Task](tasks/m6c2b2c2d5d2b-document-index-runtime.md) · [Inventory](c2d5d2b-test-inventory.json) · [Extraction](c2d5d2a-worker-runtime-extraction.md)

Base đã fetch: `origin/main = 9fb8890568af85b7beea4503f93f12e2896e6c89`, merge PR #51. Nhánh `feat/m6c2b2c2d5d2b-document-index-runtime` tạo từ base đó. Commit `d80961c` giữ riêng toàn bộ tài liệu orchestrator/review có sẵn. Source migration `0042 → 0043`; không suy revision DB dev. **Safety pending tới C4**, không mở pricing/cutover hay provider thật.

## Call, engine và khóa

| Caller | Boundary hiện hành | Phạm vi chứng nhận |
| --- | --- | --- |
| Beat `ras.ingest.discover` | `worker.due_job_ids`: extraction riêng, reaper tài chính riêng, `document_index.due` riêng | UUID, gate mới, release trước broker; Redis outage không đổi durable job |
| `ras.ingest.process` / `process_job` | preflight ngắn → configured → reaper → index claim → `document_index.process_index` | Private/common document index; extraction vẫn dừng ở awaiting_index |
| Direct `indexer.process_index` | wrapper duy nhất tới document runtime | Không còn legacy `_gate/_reserve/_provider_failure` hoặc self-commit xuyên I/O |
| Content | `document_index.{claim,proof,step,pre_send,store_result,fail}` | Owner User nếu private → item UPDATE → job UPDATE → rights/publication/version/file/consent/sections/pages/chunks → capacity → generation/entries → budget/call cần thiết → flush + constraints → journal exclusive trực tiếp → exact predicates/state/clock → commit |
| Financial maintenance | `document_accounting.{renew,reap,settle}` | Runtime/gate entry → capacity → call UUID → original-day budgets → flush/constraints → journal exclusive → exact tuple/status/clock → commit; không khóa item/User/job, không ghi content |
| Shared query admission | `provider_capacity.reap_expired` compatibility bridge | Legacy query expiry giữ API; nonce-bound document expiry chuyển sang reaper runtime sau khi nhả capacity transaction cũ. Query/rerank/RAG provider boundary vẫn task riêng |
| Discovery/citation/cleanup/recovery/operator | Giữ hợp đồng trước | Không coi đã chuyển business boundary; cleanup dùng làm đối thủ/system fixture có nhãn |

Worker/beat dùng engine runtime từ `RAS_DB__*`; guard kiểm actual login/membership/ownership/protected auth writes/DDL/TEMP và các grant index/capacity. Không provision hoặc mở connection recovery/owner trong runtime. Mọi content checkpoint có Session ngắn, rollback và close đều được thử; release lỗi invalidate và dừng. `Outcome` độc lập teardown; commit exception luôn uncertain, không SELECT suy commit, failure checkpoint, retry, giải tiền hay xóa original trong attempt đó.

Chi tiết dưới capacity: `step`/publish khóa generation/entries rồi budget/call khi cần admission; `pre_send` chỉ khóa call; `store_result` khóa call rồi generation. Capacity serialize các checkpoint này với financial maintenance/query admission. Cleanup cùng item bị item UPDATE chặn; finance không lấy item/generation, nên không tạo vòng call→item với cleanup. Claim/proof/failure chỉ lấy các resource cần cho mutation tương ứng, không mở capacity hoặc provider từ helper resource.

Source thực tế khác một câu trong prompt: tại merge `_locked` **vẫn là alias** của `checkpoints.leased`; không phải import bị thiếu. Import được tái hiện bằng test entry; predicate extraction của alias vẫn từ chối index. Indexer nay bỏ hoàn toàn phụ thuộc alias; không thêm helper owner hay fake module. `checkpoints.context/leased/_final` giữ predicate extraction. Index có context/final riêng, gồm hậu mutation `ready/indexed/active` của chính publish transaction; không kiểm publish bằng predicate metadata_only cũ.

## Binding và admission

Private cần exact owner/scope/item/version/file, consumed intent/acknowledgement/session ID/notice/size/checksum/kind và latest status head/receipt còn active/proven. Consent tuple được snapshot vào proof; logout/reset credential không hủy quyền background của owner active. Common cần listed, publication + rights đúng file/version/checksum/epoch/size/audience, fulltext + external, không conditions/tombstone/cancel. Admin tạo job chỉ là attribution, session hết không hủy common job hợp lệ.

Claim chỉ nhận awaiting_index/indexing hoặc retryable_failed resume_state=indexing, đến hạn, unfinished, không active index, lease cũ đã hết. Hai claimant chỉ một token tăng attempt; item SKIP trước job. Thiếu key/price trả `embedding_key_missing`/`embedding_pricing_unverified`: không claim/đốt attempt/reservation/provider, giữ nguyên cả index đang dở. Reaper tài chính vẫn chạy từ dispatcher ngay cả khi pricing thiếu. Disable/held/head không chứng minh được thì pause, không terminal hoặc enable. Đúng biên lease là hết; final clock lấy sau SQL cuối và journal wait, không hợp thức hóa token cũ bằng deadline mới.

`0043` thêm ba cột nullable vào ledger có sẵn: `reservation_token`, `reservation_job_id`, `reservation_job_token`. CHECK buộc tất cả NULL (lịch sử) hoặc đầy đủ (reservation mới), FK giữ job tồn tại. Retry sau rejected mint nonce mới; heartbeat/outcome bind nonce + job token + call/file/owner/model/provider/pipeline/day/ceiling. Call ID riêng không đủ. Historic reserved/uncertain không được tự adopt/gửi lại. Không bảng dẫn xuất mới, không grants mới, fence/cleanup 0024 tiếp tục scrub hash/vector và giữ ledger cùng binding tài chính. Downgrade từ chối mất binding đã sử dụng.

Admission dưới shared capacity lock tính document + query concurrency, app committed/reserved/uncertain, embedding bucket và private User quota. Giữ config $10/app/ngày, $3/embedding/ngày, $1/User/ngày và warning fractions; ceiling đầy đủ theo max_request_bytes × configured token price, không dựa usage dự đoán để tăng allowance. Common owner NULL, không quota uploader/Admin: phương án orchestrator đã chọn, không ghi là xác nhận mới trực tiếp của chủ dự án. Quote/usage chỉ là ước tính chi phí; key cap $50 không là credit.

Slot chỉ tính reservation `reserved` có lease còn sống. Query hết lease hoặc thiếu deadline không chiếm slot document dù query reaper chưa chạy; ceiling của nó vẫn nằm trong app/bucket/User spend. Document admission không đổi status/cost của query và không chuyển query provider boundary trong PR này. Lease/capacity lock hiện hành ngăn heartbeat cũ hồi sinh deadline đã hết.

## I/O, reuse và publish

Proof/source/payload detached và frozen tới nested JSON/vector. Session nội dung đã đóng, không giữ connection/transaction/row/journal/capacity lock qua storage/hash/provider/broker. Heartbeat có các transaction tài chính ngắn độc lập trong lúc provider chạy. Trước mỗi call đọc toàn original, actual size + SHA-256 ngoài DB rồi reservation/pre-send mới, known commit và release sạch. Hash mở đầu có thể phục vụ call đầu; sau call mới có hash mới trước call kế. HEAD chỉ từ chối sớm. Payload phải khớp stored chunk/section/text/hash/locator/pipeline của đúng file; references không vào payload/entry. PDF giữ parser layout hiện hành, page roles/geometry/offsets, body threshold/coverage; không OCR hoặc thay threshold.

Completed calls chỉ reuse đúng file/model/pipeline/hash, còn vector hữu hạn/dimensions phù hợp, không content_removed. Không chia call giữa private/common hoặc owners khác dù bytes trùng. Sau entry cuối **luôn có full hash mới**, kể cả mọi call được reuse; hash ngay sau entry mới có thể phục vụ publish nếu không có entry nào được ghi sau đó. Publish cùng transaction kích hoạt generation/pointers, indexed, job ready/finished và kết thúc attempt/lease. Revoke thắng thì không active; publish thắng thì revoke tiếp theo đóng gates và pointer theo hợp đồng cũ. **Khe overwrite sau hash cuối tới provider/commit vẫn còn**; không hứa phát hiện mọi thay đổi trong khe đó.

Adapter embedding/identity allowlist/model mặc định không đổi. Kiểm từng response vector hữu hạn, dimensions thống nhất, usage trong ceiling; document/query tiếp tục cùng active model. Fake transport đi qua adapter thật trong test mới và smoke Linux. Mismatch chiều giữa chunks vẫn tính chi phí response hợp lệ đã nhận, nhưng không lưu vector/entry của response đó.

Config hiện hành giữ model `openai/text-embedding-3-small`, provider route `openrouter`, request timeout 30 s, reservation lease 90 s, concurrency 2, batch một chunk, tối đa 16.384 bytes/call và price ceiling 0,10 USD/triệu token. Đây là giá cấu hình chưa đối soát charge, không giá đã xác minh. Không thay default pricing false. Publish kiểm lại cả keyword text/hash đúng chunk và provider binding của generation/call; keyword tsvector do DB tính từ text.

Job lease 120 s, max_attempts 4, retry base/max 5/300 s, dispatcher poll 5 s và batch 40. Reaper dùng cùng batch hữu hạn; heartbeat chạy mỗi `max(1, reservation_lease_seconds // 3)`. Đây là cấu hình giữ từ source, chưa là ngưỡng hiệu năng hoặc SLO đã đo.

## Accounting, thread và outcome

Job lease, reservation lease và thread/provider là ba lifetime khác nhau. Heartbeat chỉ duy trì capacity của đúng nonce còn live, không gia hạn job hoặc quyền nội dung; gate/DB lỗi thì dừng renew, giữ ceiling. Reaper batch hữu hạn theo dispatch_batch chuyển reservation quá hạn hoặc thiếu deadline sang uncertain, giải slot nhưng giữ USD gốc. Ledger lịch sử không nonce chỉ được expiry qua exact immutable tuple, không adopt/renew/settle/gửi lại. Các helper document không mở connection thứ hai khi caller đang giữ row/capacity locks. Maintenance không đòi live job/owner/content; trigger financial update không lấy item lock, tránh vòng khóa cleanup item→call.

Settlement tài chính trước content: call bền vững đúng nonce có thể ghi usage/model/provider/cost/status một lần ở reserved_day gốc kể cả revoke/cancel/disable/job lease mất/cleanup. Không ghi vector/hash/text/index hoặc mở item. Content transaction sau đó phải proof đầy đủ mới được lưu vector/entry. Khi mất response giữa known financial settlement và vector commit, call completed không vector được giữ, retry báo `embedding_content_unavailable`, không embed lại để đoán charge. Đây là giới hạn phục hồi an toàn của scope này; operator/đối soát provider vẫn riêng.

| Cửa sổ | Hành vi |
| --- | --- |
| Known admission commit + release sạch | Chỉ gửi sau exact pre-send proof mới |
| Admission/pre-send commit exception | Uncertain; không provider, inference, retry hoặc giải reserve |
| Known admission commit + release lỗi | Giữ reservation, dừng trước provider |
| Pre-send bị từ chối, chắc chắn chưa gửi | Dừng; giữ reserve bảo thủ để maintenance/đối soát, không coi job lỗi là chứng minh charge bằng 0 |
| 429/definite rejection | Settle rejected/no-charge đúng nonce; retry theo config, nonce mới, reuse completed |
| Đã gửi / timeout / malformed / identity hoặc usage sai | Giữ ceiling, status uncertain; không tự gửi lại |
| Response hợp lệ nhưng quyền/job lease mất | Settle tài chính; content proof từ chối, không vector/entry/publish |
| Settlement commit exception | Uncertain, không content/checkpoint tiếp; lượt độc lập sau mới dựa durable ledger |
| Late result sau expiry | Settle một lần trên ngày gốc; không debit reserved hoặc committed lần hai |
| Cleanup trước result | Call giữ nonce/chi phí, hash/vector đã scrub; chỉ settle tài chính, không resurrect |
| Entry/publish commit exception hoặc teardown lỗi | Giữ outcome, không failure retry/SELECT suy landed/xóa original |

Attempt sở hữu heartbeat thread tới khi thread settle; context stop Event rồi join, background lỗi không log traceback/DSN/content và không chạy tiếp sang nonce mới. Không content temp mới; original reader đóng stream trong finally như extraction. Lỗi transport/cancellation ngoài adapter giữ reservation chưa rõ, không suy chưa gửi. Fault injection không là bằng chứng SIGKILL/mất điện thực.

## Kiểm chứng và giới hạn

Exact source SHA, selection, JUnit unique/fail/error/skip/not-run, manifests, lỗi chuẩn bị, đối chứng và cleanup nằm trong inventory. Chỉ lượt sealed được tính tổng cuối; kết quả PR #51 và các lượt chuẩn bị không cộng. Python/pip dùng Conda ras; PG/MinIO/Redis/journal có project/cổng tường minh ngoài repo; outbound thật bị chặn. Host setup/migrate tách khỏi worker; worker/beat chỉ nhận runtime credentials. Business cutover chỉ scoped trong fixture; pending/hold/missing-journal tests giữ gate production thật.

Lượt sealed thứ hai phát hiện expiry lịch sử còn chiếm slot: 669 test ở các nhóm trước đạt, cleanup 4 đạt/1 lỗi, các nhóm sau chưa chạy; không cộng vào lượt cuối. Đã sửa reaper runtime để xử lý cả ledger lịch sử theo batch và giữ ceiling, thêm hai ca expired/absent deadline. Các countercheck chạy lại trên commit sửa trước seal cuối. Runner cuối dùng tối đa ba pytest process, DB/bucket/journal/tmp/settings riêng; runtime smoke và extraction smoke nằm ở hai wave tuần tự, không mutant hoặc sửa source song song suite.

Seal thứ ba đạt 699 unique tại `c7aeec5`, nhưng rà soát sau đó tìm thấy ca query expired chưa được kiểm. Bốn ca mới tái hiện 3 fail/1 pass rồi đạt 4/4 sau sửa slot predicate; thêm countercheck query lease. Seal thứ ba giữ riêng, không cộng vào tổng cuối hoặc gọi đã chứng nhận source sau sửa.

Smoke Linux dùng image dependency đã có, tag thử riêng, app source mount read-only; không build/restart image dev. Fake HTTP trả một 429 sau completed call, stop/restart worker+beat trong retry rồi kiểm nonce completed giữ nguyên và ba job Markdown/PDF/common tới ready/indexed. Redis stop/start thật; payload broker UUID-only; identity/privileges từ connection của chính process. Resource/consent source setup của smoke gắn session owner thật; HTTP upload/consent được kiểm riêng trên host, không giả vờ smoke là browser upload end-to-end.

Không đổi web/proxy nên không Chrome/npm. Không provider/arXiv/charge live, pricing change, benchmark/SLO, full suite/cutover/C3/C4, backup drill hay chuyển background writer khác. Bốn baseline debt PR #50 giữ riêng nếu không thuộc selection; không gọi đã sửa hoặc pass mới. Default Compose/API chưa cutover. **Safety pending tới C4**.

## Seal cuối và cleanup

Code **`43a656488d3a15a7d27d9c5bda5eb732f854fd3b`**: **703 API unique pass**, 0 fail/error/skip. Collect **4.477**, **3.774 not-run**; selection/JUnit khớp từng nhóm, **302 source hashes** trước/sau trùng nhau. Tám cơ chế countercheck trên đúng code SHA bắt business assertion, khôi phục bytes/hash trước seal. Tổng 23 lượt raw đối chứng gồm 8 final, 14 chuẩn bị và 1 yếu; không gọi là 23 cơ chế hoặc cộng pass lịch sử.

| Nhóm final | Pass |
| --- | ---: |
| Document runtime/common/Linux smoke/schema mới | 134 |
| Extraction/worker/PDF | 108 |
| Private/common index, search shared capacity, adapter | 92 |
| HTTP index request | 171 |
| HTTP revoke | 170 |
| Cleanup | 5 |
| ORM/migration | 3 |
| Recovery | 11 |
| Upload | 9 |

Các lệnh cuối đã chạy bằng Conda ras, từ gốc repo:

```powershell
& 'C:\Users\ADMIN\.conda\envs\ras\python.exe' scripts/c2d5d2b_counterchecks.py --context C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5d2b-20261010
& 'C:\Users\ADMIN\.conda\envs\ras\python.exe' scripts/c2d5d2b_seal.py --context C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5d2b-20261010 --name sealed-4
```

Artifact ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5d2b-20261010`. Inventory chứa node IDs, JUnit, source manifests, counterchecks, các lượt lỗi và SHA-256 của artifact manifest. Secret scan kiểm credential sinh riêng ở UTF-8/UTF-16 LE/BE trước khi xóa chúng; không in raw inspect Env. `py-spy` chỉ được thêm vào Conda ras để lấy một stack chuẩn bị, đã uninstall sau seal.

Cleanup xác minh exact ID/label và sole volume consumer: xóa 3 container thử, 6 volume (3 anonymous + 3 Compose chưa dùng), 1 network và tag image thử; không còn tài nguyên project hoặc volume phát sinh. Chín container có sẵn giữ nguyên ID/image/mount, không thiếu volume baseline. **Drift:** `StartedAt` của `ras-worker-1`/`ras-beat-1` thay đổi, phù hợp trạng thái restarting đã quan sát từ đầu; không gọi stop/start/restart/migrate/query dev. Reader cleanup đầu gặp BOM UTF-16 của snapshot PowerShell và dừng trước mọi removal, sửa reader rồi chạy thành công; lỗi được giữ trong inventory.
