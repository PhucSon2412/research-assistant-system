# M7a — kiểm chứng OpenRouter live và đối soát chi phí

Nhánh `feat/m7a-openrouter-live-verification`, base `origin/main = 5362ace` (merge PR #63).
Source migration giữ **0052**, không migration/grant. [Task](tasks/m7a-openrouter-live-verification.md) ·
[inventory](m7a-test-inventory.json). Chưa merge. Dữ liệu chỉ tổng hợp tự viết
([corpus](../evaluation/m7a-synthetic/README.md)); không dev/Dify/arXiv, không paper thật.

## Sửa review PR #64 — chờ review lại

[Review tại `5dc8947`](reviews/pr64-m7a-openrouter-live-verification.md) tìm ba P2 ở tooling experiment; đã
sửa trên cùng PR, chỉ trong `scripts/m7a_*.py` và test. Không đổi production/migration/grant; key chỉ được nạp vào bộ nhớ một lần cho lượt quét vắng secret cuối (lệch với prompt sửa),
không gửi inference mới.

1. **Permit/serialization runtime:** `Gate` giữ ledger lock từ admission qua action, đóng permit và settlement,
   nên hai action không chạy chồng; crash để lại lock (fail closed). Mỗi slot có một token permit
   `open/<batch>.<k>.<family>` tạo sau khi slot đã ghi bền. Observer chỉ gửi khi giành được token bằng
   `rename` nguyên tử sang `claimed/`; host đóng bằng `rename` sang `revoked/`, mỗi token chỉ một bên thắng
   (không flock xuyên Windows/Linux). Token host thu hồi được mới là `not_sent` (có `proof`); token đã bị lấy
   mà chưa có receipt trong thời gian ân hạn là `uncertain`, giữ đủ request/trần. Receipt gắn token nên
   response muộn không gán sang action khác; response sau ân hạn vẫn giữ trần. Family route giới hạn
   permit (embedding không dùng được token generation). Slot mở hoặc token cũ từ lượt chết → từ chối.
2. **Cost receipt:** `valid_cost` dùng chung cho probe, runtime và mọi lần đọc ledger: chỉ int/float hữu hạn,
   không âm, không bool mới giải phóng trần; âm/NaN/Infinity/bool/chuỗi/thiếu giữ trần, không clip về 0.
3. **Deadline polling:** kiểm thời gian còn lại trước từng GET và sleep, timeout từng GET ≤ phần còn lại,
   bỏ `/key` khi hết hạn (`key_status=skipped_deadline`), báo `deadline_reached` và pending.

Trước sửa (source review): test repo mới **17 fail / 7 pass** (control all-found fail chỉ vì thiếu trường
`deadline_reached`); harness Linux `--network none` tái hiện slot 21 được admit, 2 delegate, action chồng.
Sau sửa: seal **67/67 pass** (4 file offline), 484 source hash trước/sau trùng; harness Linux cùng kịch bản
từ chối slot 21 trước delegate, đúng 1 delegate, action thứ hai bị chặn. Ba lượt tranh chấp host Windows thu
hồi/container Linux lấy token trên bind mount: không token nào ở cả hai phía, không claim trùng; chưa quan sát
va chạm cùng token cùng lúc, nên tính nguyên tử ở đúng thời điểm đó dựa vào ngữ nghĩa `rename` của bind mount.
Lượt đầu không tranh chấp giữ riêng, không tính là chứng cứ.

Đọc lại ledger lịch sử theo quy tắc mới: slot `not_sent` cũ (seq 19) không có proof nên tính đủ trần →
**20 request, $0,11675 cam kết**; không còn lượt live trong trần gốc. Dữ kiện đã audit (19 call gửi,
$0,01941006 billed) không đổi. Settlement cache-write vẫn chưa xác nhận live; M7b/E giữ nguyên giới hạn.
Evidence: `C:/Users/ADMIN/AppData/Local/Temp/ras-pr64-fix-20261019`.

## Trần và kết quả chi tiêu

Trần do orchestrator chọn: **$0,50 và 20 request inference**, concurrency 1, ledger ngoài repo
ghi `sending` (fsync) trước mỗi call; call chưa có outcome tính đủ trần. Đã dùng **19/20 request**.

| Nhóm | Request | Provider billed (`usage.cost` = `/generation total_cost`) |
| --- | --- | --- |
| Probe adapter host: embed document/query, rerank, generation ×2 | 5 | $0,00504212 |
| Runtime image: 8 embed tài liệu + 1 query + 1 rerank + 3 RAG + 1 RAG Chrome | 14 (1 slot sau delete không gửi) | $0,01436794 |
| **Tổng** | **19** | **$0,01941006** |

Ledger cam kết bảo thủ $0,06675 (probe generation lỗi identity tính đủ trần $0,05). Read-only
`/generation` tìm đủ **19/19 ID** (cả `api_type` embeddings/rerank, ID `gen-emb-…`/`gen-rerank-…`),
`total_cost` khớp từng `usage.cost`; tổng khớp `GET /key usage_daily` khi đã cập nhật. Một lần đọc
`/key` giữa chừng trễ đúng một generation — không dùng số dư/key để quy chiếu từng call.

## Ma trận route

| Route | Đã chứng minh (live, tổng hợp) | Chưa chứng minh | Điều kiện enable |
| --- | --- | --- | --- |
| Embedding document `openai/text-embedding-3-small` | Catalog có, $0,02/M; Azure (pin, ZDR list); 1536 chiều hữu hạn; `input_type=search_document`; usage/cost; runtime index tới `indexed` | Server thi hành `zdr`/`data_collection` khi không có endpoint hợp lệ (chưa thử ca âm live) | Chủ dự án duyệt policy; giữ trần $0,10/M (thực tế 5× thấp hơn) |
| Embedding query | Như trên với `search_query`; hybrid search runtime | Như trên | Như trên |
| Rerank `qwen/qwen3-reranker-8b` | Endpoint Fireworks (ZDR list); thứ tự đầy đủ; usage `total_tokens`; billed **$0,20/M** = trần config; settlement app khớp đúng charge | Catalog ghi giá `0` (không chứng minh giá); status endpoint `-2`, uptime 91% lúc kiểm; không có biên an toàn giá | Theo dõi giá; cân nhắc biên trên $0,20/M trước pilot |
| Generation `openai/gpt-6-sol` | Catalog có; Azure (ZDR list, `require_parameters`); JSON hợp lệ, `finish_reason=stop`; endpoint báo snapshot `openai/gpt-6-sol-20260922`; RAG single/compare/insufficient và Chrome qua proxy | **Bản sửa cache-write chưa chạy lại live** (image runtime build trước sửa) | Một lượt live mới có ngân sách riêng xác nhận settlement ≥ charge; human review M7b |

Không route nào được bật mặc định: `pricing_verified=false` giữ nguyên trong source/config.
**Chưa đủ điều kiện gửi paper thật**.

## Lỗi tích hợp thật đã sửa

1. **Identity generation:** endpoint được chọn báo model có ngày `openai/gpt-6-sol-20260922`;
   adapter cũ đòi đúng `openai/gpt-6-sol` nên từ chối call đã billed ($0,00266). Thêm mapping
   hữu hạn `VERIFIED_GENERATION_ENDPOINT_MODELS` (khớp tên endpoint trong catalog); snapshot khác,
   `-pro`, provider ngoài pin vẫn bị từ chối (test âm).
2. **Settlement generation thấp hơn charge:** hai prompt >1.024 token bị tính giá **cache write
   $2,5/M** (chênh đúng $0,5/M × prompt token); `max_input_usd_per_million_tokens=2.0` và
   `max_price.prompt` không bao phần này. App ghi $0,012714 cho 4 call runtime, billed $0,0141385.
   Thêm `generation.max_input_cache_write_usd_per_million_tokens=2.5` (giá niêm yết của endpoint
   đã kiểm); reservation và settlement tính input theo giá cao hơn. Trần request vẫn ≤ $0,05.
   Không đổi nhãn `usage_price_estimate` thành actual; test hồi quy dùng sáu charge live.

## Routing và xử lý dữ liệu (policy MVP theo ủy quyền)

Mỗi route gửi `provider={only:[slug đã kiểm], allow_fallbacks:false, data_collection:"deny",
zdr:true, max_price:<trần config/M>}` (generation thêm `require_parameters:true`); config
`routing.only` có validation, không cho mở fallback/ZDR/data collection. Adapter từ chối provider
ngoài tập pin. API reference hiện hành cho thấy cả chat/embeddings/rerank nhận `ProviderPreferences`;
response embeddings/rerank có `provider` dù schema embeddings không ghi. `GET /endpoints/zdr`
liệt kê đúng: embedding Azure, rerank Fireworks, generation Azure (`azure`, `azure/us`, `azure/eu`;
endpoint OpenAI không ZDR). `max_price` loại Azure us/eu và OpenAI fast vì giá vượt trần.

Chưa chứng minh: điều khoản ZDR/hợp đồng của từng provider, privacy setting cấp account (không
có API, không thay đổi), và hành vi server khi không endpoint nào thỏa (không chi thêm cho ca âm).
Metadata generation báo region `SIN`. Đây là lựa chọn của orchestrator, không phải chủ dự án đã
xác nhận provider/điều khoản.

## Runtime image

Stack C4 cô lập `rasc4bc9cc106` (image API `sha256:c17c0ce8…`, web `sha256:5a4cd110…`), flow C4
default 7/7 trước khi bật live. Pricing chỉ bật trong context tổng hợp; key chỉ qua env của tiến
trình compose (không file nào chứa key). Network app vẫn internal; CONNECT proxy cho phép đúng
`openrouter.ai:443` (15 allow, 1 deny `example.com`). Observer test-only mount ngoài image giữ
receipt có giới hạn và chặn cứng ngoài allowance do ledger cấp cho từng hành động.

Kết quả: A Markdown + A PDF + B cùng bytes đều `indexed` độc lập; hybrid search có query embedding
và rerank thật; RAG single/compare `answered`, insufficient → `insufficient_evidence`; citation mở
đúng version/file/locator; B/Admin 404; sau owner delete, chunk/RAG cũ 404 và không gửi provider;
Chrome qua Next proxy trả lời có trích dẫn, 0 request ngoài origin. Image runtime build **trước**
bản sửa cache-write: 205/208 file app/migration/config khớp source cuối, 3 file khác đúng phần sửa.
Không thử common live (cap); accounting/rights common giữ ở test mô phỏng.

## Runbook pilot (giữ default off)

1. Không đổi `pricing_verified` cho tới khi chủ dự án chấp nhận policy/provider và có lượt live
   xác nhận settlement generation sau sửa.
2. Bật từng route bằng `RAS_EMBEDDING__PRICING_VERIFIED`, `RAS_RERANK__…`, `RAS_GENERATION__…`;
   giữ `routing` mặc định, không thêm slug chưa kiểm.
3. Dòng key trong `.env` hiện có dạng `OPENROUTER_API_KEY =…` (khoảng trắng trước `=`); Compose có
   thể không nhận tên biến này. Chủ dự án tự kiểm/sửa `.env`; task không sửa.
4. Lặp lại: `python scripts/m7a_live.py --state <dir ngoài repo> catalog|probe <route>|reconcile|ledger`
   và `python scripts/m7a_runtime_drill.py --context <C4> --state <dir> configure|up|egress-check|run|chrome`.
   Đối soát lại: `python scripts/m7a_live.py --state <dir> reconcile --deadline 300` (read-only).

Còn lại: M7b (người chấm grounded/citation/abstention, paper thật có quyền), gói E (journal độc
lập, HTTPS/delivery/rate-limit/CI/backup/alert, cutover nhóm thử).

## Kiểm thử offline và hồi quy

- `api/tests/test_m7a_routing_and_ledger.py` (26 ca, không key/mạng): validation routing, pin provider
  embedding/rerank/generation, mapping snapshot hữu hạn và test âm, ledger trần USD/request, crash-resume
  (`sending` không outcome tính đủ trần), khóa một tiến trình, redaction key, observer delegate,
  429/5xx/identity sai, polling có deadline, settlement ≥ sáu charge live.
- Contract cũ cập nhật có lý do: payload thêm `provider`; fake generation dùng provider `Azure` (được pin);
  `scripts/c4_fake_provider.py` đòi policy no-fallback/ZDR.
- Selection theo rủi ro (`scripts/m6d_inventory.py --files …`, 5 shard tmpfs, image `rasm7a-api:local`
  từ source cuối): **652/652 pass**, 0 fail/skip, 20 file, collection 5.324, **4.672 not-run**, source
  trước/sau khớp. Gồm search/RAG/common/index/accounting runtime, C4 contract, evaluation, process smoke
  và hai browser C2d-6/7. Web không đổi nên không chạy web test. Full >5.000 không chạy lại; hai failure
  lịch sử M6d giữ nguyên chưa rõ nguyên nhân.
- Lỗi phát triển giữ riêng trong inventory (NaN ceiling, build PyPI timeout, PDF preflight, probe identity).

Evidence ngoài repo: `C:/Users/ADMIN/AppData/Local/Temp/ras-m7a-20261018` (ledger, receipt, reconcile,
check, screenshot; không key). Đã dọn container/volume/secret của task theo ownership; giữ ba image task.
