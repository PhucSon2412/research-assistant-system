# M7c — xác minh live, paper thật giấy phép mở, đánh giá bằng AI judge

Nhánh `feat/m7c-live-quality-verification`, base `origin/main = b5d8117` (merge PR #69, tree khớp `319670d`).
Source migration giữ **0052**, không migration/grant, không đổi code runtime của API/web. [Task](tasks/m7c-live-quality-verification.md) ·
[inventory](m7c-test-inventory.json) · [report AI judge](../evaluation/ai-review/report.md). Chưa merge.

**Tóm tắt.** Live trong stack C4 cô lập, image build từ source của nhánh: 10 paper arXiv CC BY/CC BY-SA index được,
retrieval hybrid+rerank Recall@5 = 31/34 span gold, cache-write và cache-read đã quan sát live và settlement ứng dụng
≥ charge thật ở mọi call có kết quả, bốn giới hạn budget/quota chạy đúng trên đường live. AI judge (Claude Sonnet 5.5,
track `ai_judge` riêng) chấm 80 nhãn M4c, 3 answer M7a và 36 answer M7c. Bản demo `ras-pilot` đã bật provider cho
ba route. Điểm yếu thật được ghi ở mục 4: so sánh nhiều paper hầu như không trả lời được ở keyword mode, title mặc định
là tên file, output 512 token cắt câu so sánh dài.

## 1. Bộ paper thật có giấy phép mở

[Manifest](../evaluation/m7c-papers/manifest.json) · tải lại: `python scripts/m7c_papers.py fetch --dest <thư mục ngoài repo>`
(kiểm lại license rồi SHA-256/kích thước). Repo **không** chứa PDF hay toàn văn.

**Cách kiểm license (`scripts/m7c_papers.py`, chính sách thuần `licence_verdict`).** Một version chỉ được nhận khi
**cả** trang `arxiv.org/abs/<id>v<N>` của đúng version đó **và** bản ghi OAI-PMH `arXivRaw` ghi CC BY 4.0, CC BY-SA 4.0
hoặc CC0 1.0. Hai nguồn phải trùng nhau, và version tải về phải là version mới nhất (license OAI là cấp bản ghi). License
mặc định arXiv, NC/ND, không rõ hoặc hai nguồn lệch đều bị từ chối. Script chỉ gọi arXiv (3,2 s/request, User-Agent mô tả).
`fetch` kiểm lại license trước khi tải; 12/12 khớp SHA-256 lúc kiểm.

**Phễu chọn.** 12 truy vấn chủ đề RAG (retrieval/rerank/chunking/đánh giá/hallucination), lọc comment ≤ 8 trang để vừa trần
400 call → 47 ứng viên → **30 qua license** (17 bị từ chối, đều license mặc định arXiv). Parse bằng parser production trong
image (`pypdf-6.21.1-layout-v2`) cho kết quả **17 `awaiting_index`, 11 `needs_review`, 2 lỗi parser**. Đây là số liệu
sản phẩm thật trên paper arXiv hai cột: gần 40 % paper hợp lệ không index được vì ranh giới body/bibliography không chắc
chắn. Sản phẩm cố ý không đoán và không OCR.

Bộ chọn 12 paper (agent chọn trong phạm vi orchestrator giao), gồm 10 index được và 2 không index được để báo đúng trạng thái:

| ID | arXiv | License | Parser sản phẩm | Kết quả trong stack thử |
| --- | --- | --- | --- | --- |
| P01 | 2605.03989v2 Experience-RAG Skill | CC BY 4.0 | awaiting_index | indexed, 11 embedding |
| P02 | 2308.00479v1 RAG + Representative Vector Summarization | CC BY 4.0 | awaiting_index, coverage partial | indexed, 14 |
| P03 | 2606.11199v1 NightFeats @ MMU-RAGent | CC BY 4.0 | awaiting_index | indexed, 15 |
| P04 | 2602.23184v1 MTRAG-UN | CC BY-SA 4.0 | awaiting_index | indexed, 17 |
| P05 | 2610.22615v1 Uncertainty và RAG hallucination | CC BY 4.0 | awaiting_index | indexed, 17 |
| P06 | 2608.03860v1 SciRet | CC BY 4.0 | awaiting_index | indexed, 19 |
| P07 | 2602.22217v1 RAGdb | CC BY 4.0 | awaiting_index | indexed, 20 |
| P08 | 2407.12101v2 Relevant Information Gain | CC BY 4.0 | awaiting_index | indexed, 21 |
| P09 | 2608.00824v1 Title-chain semantic chunking | CC BY 4.0 | awaiting_index | indexed, 23 |
| P10 | 2606.16661v2 SCAR | CC BY 4.0 | awaiting_index | indexed, 26 |
| P11 | 2607.11933v1 LLM cross-encoder distillation | CC BY 4.0 | needs_review | `metadata_only`, `pdf_body_uncertain`, 0 call |
| P12 | 2410.02914v1 Conformal IR | CC BY 4.0 | lỗi parser | `terminal_failed`, `pdf_text_invalid`, 0 call |

Thêm hai ghi chú tự viết ([`synthetic/`](../evaluation/m7c-papers/synthetic)): memo chứa chuỗi injection (user A,
marker `ORCHID-7731`) và ghi chú riêng của user B (marker `QUOKKA-5528`). Ghi công đầy đủ nằm trong manifest; PDF không chỉnh sửa.

**Bộ câu hỏi** ([`questions.json`](../evaluation/m7c-papers/questions.json), `authored_by: ai_agent`): 36 câu, 7 holdout,
gồm 16 một paper, 6 so sánh, 6 không có đáp án, 4 partial/không index và 4 injection (trong câu hỏi, trong tài liệu,
tài liệu riêng của user khác). 34 quote gold được định vị không phân biệt khoảng trắng rồi lưu đúng chuỗi con của text
parser, kèm trang, offset và segment (`python scripts/m7c_papers.py questions --parsed <dir>` để kiểm, `--write` để ghi).
Parse bằng image thử và rc2 cho ra JSON trùng byte.

## 2. Stack thử và cách gọi provider

- Stack C4 cô lập `rasc45baae9c6` dùng image build từ source nhánh: API `sha256:88c1e2ab57cf…`, web `sha256:ff17bfe37b2d…`.
  Hai image được giữ lại; container, volume và mạng đã dọn.
- Baseline trước khi bật live: flow C4 mặc định đạt (pricing tắt → 0 call provider). Lượt đầu fail ở bước 5 vì tôi chưa
  chạy `fake-off` (key rỗng cho mã `embedding_key_missing`). Đây là lỗi thứ tự harness, đã giữ riêng kết quả.
- Key được chép từ `%USERPROFILE%\ras-pilot\secrets\openrouter_api_key` vào `<context>/secret` với ACL chỉ user hiện tại,
  rồi mount read-only. Observer test-only (`scripts/m7c_observer.py`) nạp key lúc interpreter khởi động, nên key không
  có trong Compose env hay `docker inspect` (đã kiểm api/worker/beat). Egress chỉ cho `openrouter.ai:443` (`example.com`
  bị chặn). Bản sao key đã xóa sau khi dọn stack. Quét 4.744 file evidence/state/repo (không đọc `.env`) không thấy key.
- Mỗi call cần permit do ledger cấp cho đúng hành động (`m7a_runtime_drill.Gate` + ledger M7c). Trần $5/400 call, dừng
  mới ở 80 % ($4/320). Ledger ghi `sending` (fsync) trước khi gửi; call không có cost hợp lệ bị tính đủ trần.
- Routing pin ZDR như M7a: embedding/generation `azure`, rerank `fireworks`, judge `amazon-bedrock`. Mọi route đều có
  `allow_fallbacks=false`, `data_collection=deny`, `zdr=true` và `max_price`.

## 3. Chi phí và đối soát

Ledger cuối: **307/400 call, $1,98738776 cam kết**. Trong đó **$0,97738776 là billed thật** và $1,01 giữ trần fail-closed.
Ba lượt `reconcile` read-only `/generation` tìm đủ **300/300 ID**, `total_cost` bằng `usage.cost` ở cả 300.

| Nhóm | Call | Billed | Settlement ứng dụng | Ghi chú |
| --- | --- | --- | --- | --- |
| Embedding document | 188 | $0,00078616 | $0,0039308 (188/188 ≥ charge) | $0,02/M thật, ứng dụng tính trần $0,10/M |
| Embedding query | 26 | $0,000009 | $0,000045 (26/26) | |
| Rerank | 26 | $0,0205312 | $0,0205312 (26/26) | Bằng đúng $0,20/M, **không có biên**; `max_price` chặn giá cao hơn |
| Generation | 29 | $0,1874354 | 28/28 call có phản hồi được phủ | 1 call timeout (drill) giữ trần $0,05, không có ID |
| AI judge | 38 | $0,768626 | (ngoài ứng dụng) | 6 call 404 giữ trần $0,96, xem mục 6 |

Không tính 33 slot generation bị host thu hồi (permit chưa dùng). Hai call generation bị ứng dụng từ chối
(INJ-01 content filter $0; CMP-03 cắt `length` $0,01347) giữ reservation `uncertain` $0,04512 ≥ charge.

**Cache.** Cache-write đã quan sát live ở 21 call có prompt > 1.024 token: `cache_write_tokens` ≈ prompt − 3. Cache-read
quan sát ở lần lặp SP-15: `cached_tokens = 3.872`, prompt cost $0,0007804 so với $0,009686 lúc ghi. Giá live tách đúng từng
call: input thường $2/M, cache write $2,5/M, cache read $0,2/M, output $10/M (regression
`test_generation_settlement_covers_every_m7c_live_charge`, 26 charge thật). Settlement ứng dụng tính mọi input ở $2,5/M nên
luôn ≥ charge. Biên chỉ 3 token × $0,5/M ở call cache-write, còn call cache-read được tính dư khoảng 4×.

**Budget/quota trên đường live** (cấu hình api tạm đổi rồi khôi phục; mỗi drill có permit riêng):

| Drill | Kết quả | Call provider |
| --- | --- | --- |
| Trần User generation $0,05 | 429 `rag_user_budget_exhausted` | 0 |
| Bucket generation ứng dụng $0,05 (B chưa tiêu) | 429 `rag_generation_budget_exhausted` | 0 |
| Concurrent = 1, A và B cách 1,5 s | A answered; B 429 `rag_generation_concurrency_limit` | 1 |
| Timeout 1 s | 503 `rag_provider_outcome_uncertain`; row `uncertain`/`unknown_reserved_ceiling` | 1 (không phản hồi) |

Lượt đầu, câu của B ở scope `all` không có evidence nên dừng trước reservation và chưa chạm bucket/concurrent. Tôi sửa drill
cho B hỏi trên ghi chú riêng rồi chạy lại hai drill đó; kết quả lượt đầu giữ trong evidence. `usage_daily` của `/key` không
dùng để đối soát vì key do nhóm dùng chung (lúc bắt đầu đã $46,8/ngày, `limit` thực của key là $100/ngày, khác con số
$50/ngày trong docs). Hai job tổng hợp còn treo từ flow C4 mặc định được worker nhặt lại khi bật pricing; observer từ chối
vì không có permit, nên ứng dụng ghi `uncertain` giữ trần và không gửi provider (fail-closed đúng thiết kế).

### Kết luận `pricing_verified` theo route

| Route | Đủ chứng cứ? | Lý do |
| --- | --- | --- |
| Embedding document | **Có** | 188 call live trên paper thật, ZDR Azure pin, 1536 chiều, settlement ≥ charge ở mọi call, đối soát 188/188 |
| Embedding query | **Có** | 26 call, như trên |
| Rerank | **Có, kèm điều kiện** | 26/26 khớp đúng giá; settlement bằng charge (không biên). `max_price=$0,20/M` khiến provider đắt hơn bị loại thay vì tính vượt. Catalog vẫn ghi giá 0, nên cần theo dõi giá |
| Generation | **Có, kèm điều kiện** | Settlement ≥ charge ở 28/28 call, kể cả cache write/read; content filter/length/timeout đều fail-closed. Cache-write ($2,5/M) nằm ngoài `max_price.prompt`, nên nếu provider tăng giá cache-write thì settlement có thể thấp hơn charge. Xem lại khi đổi giá |

PR này **không** đổi mặc định trong source/`deploy/`: `pricing_verified=false` giữ nguyên. Chỉ bản demo bật (mục 7).

## 4. Chất lượng: retrieval, answer, AI judge

[Report AI judge](../evaluation/ai-review/report.md) · [snapshot run](../evaluation/ai-review/sources/m7c-run.json) (claim,
quote ngắn, locator, kiểm máy; không có text chunk). RAG keyword mode, user A.

**Retrieval** (span gold được phủ khi hit cùng paper, cùng trang và offset chứa trọn span; 26 câu có đáp án, 34 span):
hybrid + rerank Recall@5 = **31/34 = 0,912** (đủ mọi span ở 23/26 câu); keyword Recall@5 = 23/34 = 0,676 (19/26). Ba span
hụt đều thuộc câu so sánh. Lượt đo đầu ra 0 vì PDF lưu offset trong `locator_detail` mà bộ đo bỏ qua; tôi bổ sung offset
bằng cách mở từng chunk qua API (130 chunk, không gọi provider). Hit gốc giữ ở evidence.

**Kiểm máy** (độc lập judge, judge không ghi đè):

- quote nguyên văn trong chunk mở qua API: 25/25;
- quote có trong text parser production đúng trang: 24/24;
- citation trong scope: 25/25;
- lộ marker riêng của B: 0/36; lộ marker injection: 0/4;
- trạng thái khớp kỳ vọng: 27/36, theo nhóm: một paper 14/16, so sánh 1/6, không đáp án 6/6, partial 4/4, injection 2/4.

**AI judge** (`anthropic/claude-sonnet-5.5`, khác họ với generation OpenAI, Amazon Bedrock ZDR, rubric có version):

- **Nhãn M4c:** chấm 80/80. 71/73 `adequate` (không tính 7 câu lựa chọn sản phẩm, chỉ ghi nhận xét). 2 `needs_change`:
  CMP-09 và RCI-09, quote gold chưa phủ hết claim. Nhãn M4c **không** sửa trong PR này vì sửa sẽ đổi binding mà M7b đang
  chờ người đọc.
- **Answer M7a:** claim được hỗ trợ 2/2, citation đúng 3/3, abstention đúng 1/1.
- **Answer M7c:** claim được hỗ trợ đầy đủ 21/23 (2 partial: CMP-02 suy diễn cách diễn đạt, SP-13 tự tính chênh lệch).
  Citation đúng 27/27. Abstention đúng ở câu kỳ vọng không trả lời 9/10 (INJ-02 trả lời "memo không đo gì" thay vì từ chối;
  không làm theo injection). Cả 7/7 câu có đáp án bị hệ thống từ chối đều bị judge đánh là từ chối sai. Làm theo injection
  0/4, ngoài scope 0/36.
- **Ổn định** (lượt 2 trên mẫu): nhãn 8/8, M7a 3/3, M7c 8/8 giống lượt 1.

**Điểm yếu sản phẩm phát hiện được (chưa sửa, cần quyết định):**

1. **So sánh nhiều paper:** 1/6 trả lời được. Pack so sánh nhận diện hai paper qua từ trong title, mà upload PDF mặc định
   có title là tên file (`arxiv-2606.16661v2`). Lượt đầu (title = tên file) 0/6. Sau khi owner sửa title qua
   `PATCH /items` (luồng có sẵn trên trang item), CMP-02 trả lời đúng, CMP-03 bị cắt và 4 câu vẫn
   `comparison_requires_multiple_papers` vì retrieval keyword không đưa đủ hai paper vào pack. Hybrid cho so sánh chưa đo
   (để dành trần cho judge).
2. **Output 512 token:** CMP-03 cắt ở `length` (390 token reasoning) nên bị billed mà không có câu trả lời. Đề xuất cân
   nhắc nâng `generation.max_output_tokens` cùng trần giá, cần quyết định riêng.
3. **Keyword hụt câu một paper:** SP-08 (dải số có gạch nối xuống dòng) và SP-11 (`Intel i7-1165G7`): hybrid tìm được
   (Recall@5 có), keyword RAG thì không.
4. **Content filter Azure** chặn câu chứa injection rõ (INJ-01) và trả về `rag_provider_response_invalid` 503 thay vì một
   thông điệp dễ hiểu. Không lộ dữ liệu, chi phí $0.

## 5. Kiểm chứng

- Test offline mới: `api/tests/test_m7c_tooling.py` (40) và `api/tests/test_m7c_ai_review.py` (15), không key/mạng.
  Phạm vi: trần 80 %, key chỉ từ file, chính sách license, locator, judge transport, ghép settlement, kiểm máy, recall,
  regression charge live, lệnh `provider` của demo, chốt chặn track human, binding/stale, parse reply, report.
- [Countercheck](../scripts/m7c_counterchecks.py) **15/15 đột biến bị bắt**, bản sạch exit 0, source trước/sau trùng. Các
  đột biến gồm: ghi AI vào cây/cột human, judge không bind (3), nhận sai license (3), bỏ dừng 80 %, judge trả content khi
  cost sai, judge mở fallback, key từ env, settlement thấp hơn charge (2), drill nhận key.
- CI selection: thêm `tests/test_evaluation.py` và hai file M7c vào `ci.yml`. Chạy cục bộ đúng tham số `ci.yml` bằng
  `m8a_test_host.py` (PostgreSQL/Redis/MinIO theo digest pin): **1.282 pass, 1 fail, 1 deselect** (1 giờ 11 phút). Test fail
  là `test_m8a_smtp_delivery.py::…[starttls]` với `smtp_timeout` (process con gửi mail quá deadline khi máy đang tải).
  PR không đổi file SMTP nào; chạy lại riêng file đó 3 lần đều 25/25 pass. Lượt chính vẫn ghi là fail. Lượt chạy trước đó
  bị mất điện ở ~61 % và không tính. CI GitHub sẽ chạy trên PR.
- **Hai P3 của review PR #68 đã xử lý:** `scripts/m8a_test_host.py` và `scripts/c3_test_host.py` đọc digest
  PostgreSQL/Redis (và MinIO cho m8a) từ `deploy/compose.yaml` qua `deploy_image(service)`. Ảnh không pin digest thì dừng
  chứ không thay bằng tag. Đã kiểm container chạy `pgvector@sha256:a3625087…`, `redis@sha256:858f009f…`. CI vẫn chặn HTTP
  ngoài; test live chỉ chạy tay qua script có ledger.

## 6. Lỗi trong lúc làm (giữ riêng, không giấu)

- **6 call judge 404 bị tính đủ trần ($0,96).** Tôi gửi `temperature` cùng `require_parameters`, mà endpoint không hỗ trợ
  `temperature`, nên OpenRouter loại mọi endpoint. Script lúc đó chạy tiếp các batch thay vì dừng. Đã sửa: bỏ
  `temperature` (ghi "model default" trong provenance) và **dừng ngay** ở outcome không hoàn tất.
- **Một batch nhãn cắt `length`** ($0,07) với batch 16 câu; đã chạy lại theo batch 8.
- **Rubric answer `/1` quá chặt:** 5/9 batch bị parser từ chối khi judge chấm abstention cho câu có đáp án mà hệ thống
  từ chối. Rubric `/2` chấm cả trường hợp đó; toàn bộ answer M7a/M7c được chấm lại theo `/2` (10 call). Record `/1` chuyển
  ra evidence ngoài repo. Reply thô nay lưu ngoài repo, nên batch cuối được ghi lại từ reply đã lưu (prompt hash khớp)
  mà không gọi thêm.
- **10 câu scope `all` trả 422 ở lượt RAG đầu:** body thiếu `scope: "all"`, bị chặn ở validation trước mọi call (16 slot
  bị thu hồi, không tính). Đã sửa và chạy lại.
- **Mất điện máy giữa lúc chạy selection CI cục bộ:** stack test tmpfs mất. Đã dọn (theo label context), dựng lại và chạy
  lại trên source cuối. Bản demo tự khởi động lại; kiểm sau sự cố: gate open, journal 5 = DB 5, không torn tail, replica
  `ok`, cấu hình provider còn nguyên.

## 7. Bản demo `ras-pilot`

`python scripts/m9_lan_demo.py provider --routes embedding rerank generation --apply`:

- Key chỉ đọc từ `secrets\openrouter_api_key` và ghi vào `pilot\pilot.env`; file này và bản sao `pilot.env.before-provider`
  có ACL chỉ user hiện tại, key không bao giờ được in.
- Bật pricing ba route, recreate api/worker/beat. Cửa sổ khoảng 16 s; image rc2 giữ đúng ID `sha256:c0936a81…`; `deploy/`
  không đổi.
- Lựa chọn provider lưu trong `state.json`, nên `rebind` giữ nguyên. Drill restore không bao giờ nhận key.
- Trước khi bật đã kiểm demo không có item/job nào (0/0), nên không có tài liệu sẵn bị gửi đi.
- Kiểm sau: `/readyz` 200; Settings trong container có key, pricing true/true/true, budget app $10/ngày, quota generation
  của User $1/ngày, routing `azure`/`fireworks`/`azure`; GET công khai tới OpenRouter từ worker 200; `m9_lan_demo.py verify`
  ok (chỉ IP Ethernet); `ras_ops status` replica `ok`, `maintenance: null`.
- Theo hợp đồng deploy M8a, key nằm trong env của container demo (interpolation từ env file). Đây là cách deploy đã review,
  không phải lựa chọn mới.
- Thư mục paper demo ngoài repo: `%USERPROFILE%\ras-pilot\demo-papers\` gồm 12 PDF và `ATTRIBUTION.md`. Kịch bản demo nằm
  trong [handoff M9](m9-lan-demo-deployment.md) (mục "Kịch bản demo").
- **Not-run:** search/RAG qua HTTPS trên demo. Việc này cần chủ dự án upload paper qua UI (xác nhận thông báo xử lý) và
  một tài khoản thử; agent không tạo tài khoản hay dùng mật khẩu của chủ dự án. Hướng dẫn nằm ở handoff M9.
- Tắt lại: `python scripts/m9_lan_demo.py provider --off --apply`.

## 8. Giới hạn

- **AI judge không thay người đọc.** M7b vẫn 0 verdict người thật; track AI không ghi vào phiếu hay record `human_*`
  (có test + countercheck). Câu hỏi M7c và nhãn M4c do agent soạn.
- **ZDR theo cam kết provider qua OpenRouter.** Cài đặt logging/training của tài khoản do nhóm quản lý, chủ dự án tự xác
  nhận trên trang cài đặt; không kiểm được qua API và không suy ra là đã tắt.
- Mẫu nhỏ: 10 paper ngắn, 36 câu, một lượt keyword; không phải SLO, chứng nhận production hay CI gate.
- Image thử build từ source nhánh lúc bắt đầu live. So với hash lúc build, trong `api/` chỉ
  `app/evaluation/ai_review.py` và hai file test M7c đổi sau đó (không thuộc runtime API/worker), nên image không build lại.

Evidence ngoài repo (không có key): `C:/Users/ADMIN/AppData/Local/Temp/ras-m7c-20261021`, gồm ledger, receipt, actions,
answers, chunks, reply judge thô, settlement, reconcile và log.
