# M11 — thư viện cá nhân trên máy và kho paper mở rộng

**Đang triển khai (2026-10-24, Asia/Saigon).** Base đã fetch/xác minh
`1f7bfac970fa0015a6d3211d3514a4136df4d4d6` (PR #71 MERGED), nhánh
`ops/m11-demo-knowledge-base`. Chỉ dẫn mới của chủ dự án thay 150/$10 bằng
**khoảng 300 paper bổ sung/$30**, dừng gọi mới ở **$24**. Ngày 2026-10-24 chủ dự án
yêu cầu **nạp thêm khoảng 1.000 paper** và đã xác nhận thêm ngoài lô 300,
tổng khoảng **1.300 paper mới**. Giữ cùng ledger/trần/quota, không cộng
thêm ngân sách vì key còn tiền. Không merge.

## Chuyển sang local

Đã chạy `m9_lan_demo.py rebind --local --apply`: state lưu `access_mode=local`, chỉ sửa
`RAS_EDGE_BIND` trong env hiện có, cấp leaf DNS-only từ CA demo cũ, recreate **chỉ edge**
với `--no-deps --pull never`. API rc2, web ui2, dữ liệu, tài khoản và lịch giữ nguyên.
`provider` khi sinh lại env lấy bind từ state; `web-image` chỉ sửa dòng image như trước.

Kiểm thật: listener duy nhất `127.0.0.1:8443`; HTTPS `/login` với CA/SNI/Host
`ras-demo.lan:8443` trả **200**; Wi-Fi, Radmin và WSL đóng. IP trần không SNI vẫn bị từ chối.
CA hiện có không cho SAN IP loopback, nên leaf local chỉ dùng DNS SAN.
Không thay trust store, hosts hay firewall. Lệnh chủ dự án tự chạy và cách gỡ cấu hình Ubuntu:
[handoff M9](m9-lan-demo-deployment.md#dùng-trên-chính-máy-windows).

Inventory trước: **22 container / 194 volume / 12 network / 150 image**. Evidence:
`%USERPROFILE%\ras-pilot\evidence\m11-inventory-before.json`.

Inventory sau chuyển local vẫn **22/194/12/150**; chỉ `ras-pilot-edge-1` đổi ID,
không thêm/xóa container, không đổi volume hoặc image ID. Ethernet/Tailscale không có địa chỉ
IPv4 hiện hành trong lượt kiểm; listener ngoài loopback không tồn tại.

## Corpus và luồng nạp

- CC BY 4.0 / CC BY-SA 4.0 / CC0 1.0. Lô OAI yêu cầu arXivRaw trùng license abs đúng version,
  và version đó latest trong lịch sử OAI. Lô Atom yêu cầu entry ID có version, trang abs
  hiện hành và trang abs đúng version đều xác nhận cùng version mới nhất/cùng license mở.
  Không nhận NC/ND/license mặc định, lịch sử không rõ hoặc bằng chứng bất đồng.
- Bảy chủ đề: dense retrieval/embedding; reranking/search; RAG; đánh giá/benchmark;
  grounding/hallucination; long context/chunking; agent tìm kiếm/tool use.
- Không trùng arXiv ID/SHA-256 của corpus cũ. SHA-256 12 original được kiểm qua route
  tải file của chính user, không SQL/internal upload.
- PDF/attribution ngoài repo ở `%USERPROFILE%\ras-pilot\demo-papers\`; repo chỉ có
  [manifest](../evaluation/m11-papers/manifest.json), metadata và script tải lại.
- Request arXiv tuần tự cách ít nhất 3,2 giây, User-Agent mô tả, có checkpoint.
  OAI ListRecords giảm request lẻ; `from` là ngày cập nhật metadata, không phải ngày đăng bài.
  Hợp đồng nguồn: [arXiv OAI](https://info.arxiv.org/help/oa/index.html),
  [API user manual](https://info.arxiv.org/help/api/user-manual.html).
- Credential chỉ từ `library_user_email`/`library_user_password`; không in/log/commit
  mật khẩu, cookie hay token intent. Giữ một phiên cho mỗi lượt script.
- **Consent là đồng ý theo lô của chủ dự án/chủ tài khoản upload cho đúng corpus này.**
  Mỗi file vẫn notice → intent `acknowledged=true` bind SHA-256/size/notice version → PUT
  → poll trạng thái cuối → PATCH title thật → thêm collection qua API HTTPS.
- Tối đa hai job đang chạy; không đổi concurrency/budget/quota ứng dụng. Không retry mutation
  khi chưa xác định outcome; giữ item lỗi/cần review, không xóa để làm đẹp số.

**Lô đầu đã khóa 300 paper đúng phạm vi**: 282 CC BY 4.0, 14 CC BY-SA 4.0, 4 CC0 1.0;
**838.284.160 byte** PDF ngoài repo. Kiểm 300/300 SHA-256 và kích thước khớp, không trùng ID/hash
12 paper cũ; rà topic lần cuối không loại thêm. Attribution giữ 12 paper cũ + 300 target + 48 ngoại lệ tải về.

| Chủ đề target | Paper |
|---|---:|
| Dense retrieval & embeddings | 25 |
| Reranking & search | 34 |
| Retrieval-augmented generation | 14 |
| RAG evaluation & benchmarks | 62 |
| Grounding & hallucination | 59 |
| Long context & chunking | 43 |
| Search agents & tools | 63 |

Checkpoint trước khi Docker được mở lại ngày 2026-10-24: **252/300 target đã nhận receipt**, trong đó
121 `ready`, 40 `needs_review`, 89 `terminal_failed`, hai job còn ghi `indexing`.
Client phiên trước đã thoát; lúc đó Docker Desktop tắt nên chưa thể đối chiếu hai job hoặc
nạp 48 paper còn lại. Cam kết lúc đó **$2,7876122**, không có thao tác trả phí mới khi API tắt.
Chủ dự án đã mở Docker; cả hai job đối chiếu lại đều `ready`, client đã nạp tiếp từ receipt.
Kiểm local sau khởi động lại pass, còn khoảng 274 GiB trên ổ C. Snapshot manifest 300 đầu lưu
ngoài repo ở `evidence/m11/manifest-initial-300.json`; phần mở rộng chỉ bổ sung, không thay 300 đầu.
OAI tiếp tục phân trang theo target thay cho mốc 650 candidate ban đầu. Đây là checkpoint,
không phải kết quả cuối; trang/chunk/object và câu hỏi cập nhật sau khi job kết thúc.

Lượt mở rộng đầu timeout khi tiếp tục token của cache hôm trước; `Identify` mới vẫn 200.
[arXiv ghi token hết hạn hằng ngày](https://info.arxiv.org/help/oa/index.html#minor-differences),
nên cache phân trang mới theo ngày UTC, không nối token cũ. Cache XML/decision/PDF cũ giữ nguyên;
test kiểm không dùng lại stream cũ và vượt được ngưỡng 650 candidate.
Probe ListRecords mới (kể cả set CL hẹp hơn) và GetRecord cũng timeout, trong khi Identify 200;
không kết luận toàn bộ arXiv ngừng hoạt động. Tiếp tục các candidate chưa dùng trong snapshot
OAI 666 record đã có bằng `discover --cached-candidates --target 1300`: vẫn lấy license trang abs
đúng version trước PDF, so với license/version history của OAI snapshot, không bỏ kiểm license.
Nếu hết snapshot mà metadata mới chưa lấy được thì giữ checkpoint và chưa báo đủ target.
Một request ListRecords mới với read timeout 180 giây cũng đã timeout (180,28 s), giữ
`evidence/m11/fresh-oai-probe-180s.json`. Không hạ tiêu chí license để bù thiếu metadata.

Phần mở rộng còn phát hiện PDF tải về bị loại vì quá lớn/trùng hash chưa được ghi credit đầy đủ.
Tooling mới lưu `download-exclusions.json`, phân biệt hai lý do, giữ attribution cho cả file bị loại.
Với quyết định cũ đã mất verdict abs, script kiểm lại abs đúng version trước khi bổ sung credit;
không dựng lại bằng chứng giả từ trường OAI. File bị loại không đi upload/provider.
Offline phần mở rộng từng đạt **70 pass/1 deselected** (ca đủ manifest đợi khóa corpus).
Hai assertion mới từng sai do đòi raw evidence HTTP phải thành HTTPS; đã sửa để giữ raw URL
và so URL chuẩn hóa riêng. JUnit fail và lượt sửa lưu hai tên khác nhau ngoài repo.

API Atom chính thức trả 200 trong khi OAI vẫn timeout. Fallback
`discover --atom-candidates --atom-pages 4 --target 1300` giữ nguyên bộ lọc chủ đề,
license và hash. Với mỗi candidate, script lấy cả abs không version và abs có version,
kiểm citation ID/lịch sử/phiên bản đang chọn cùng trùng latest; license hai trang phải đồng ý.
Manifest ghi rõ `method=atom_and_live_abs_latest`, không giả trường OAI; HTML và XML gốc
ngoài repo có SHA-256. Tải lại cũng kiểm mới hai trang trước khi nhận bytes/hash đã khóa.
Regression mới kiểm newer version, NC/ND, thiếu lịch sử, sai ID, license bất đồng và file
hỏng được giữ nguyên. Lượt offline mới nhất **89 pass/1 deselected**, chưa phải corpus hoàn tất.
Nếu một trang Atom lỗi/timeout/XML không hợp lệ, script lưu metadata lỗi ngoài repo và tiếp tục
truy vấn chủ đề/thứ tự khác; không ghi trang lỗi thành cache hợp lệ hoặc suy ra license.
Snapshot Atom từ lần tìm đầu vẫn có 2.100 entry; bộ lọc hiện hành giữ 1.700 candidate.
`--atom-pages 1` tái dùng XML gốc với đường dẫn/hash thật, vẫn lấy mới hai trang abs trước PDF.
Một ID arXiv kiểu cũ (`cs/0111012v1`) nằm ngoài schema manifest nên được ghi lý do và bỏ qua,
không làm mất các candidate còn lại. Atom error entry cũng không được coi là paper.

Đề xuất khởi chạy controller nền để nối tải/nạp → smoke → backup/check đã bị bộ duyệt tự động
từ chối (phạm vi 1.300 và chuỗi chạy không giám sát chưa được xác nhận tường minh). Controller
**không được khởi chạy**. Các tiến trình M11 trực tiếp vẫn chạy theo checkpoint; smoke/backup
cuối phải được agent kiểm điều kiện và chạy sau khi hoàn tất, chưa được xếp chạy tự động.

### Sửa bộ lọc chủ đề trong lúc nạp

Bộ lọc ban đầu tìm chuỗi `rag`, khớp cả “average”/“paragraph”, và có thể gán nhầm nhóm đánh giá
cho paper ngoài retrieval. Đã sửa thành word boundary, bổ sung regression âm/dương, rà lại OAI
đã cache và tiếp tục tải bù. **48 PDF đã tải bị loại khỏi target**; PDF ngoài repo được giữ nguyên.
[Danh sách metadata/SHA-256 của các ngoại lệ](../evaluation/m11-papers/topic-exclusions.json).

**5 item đã upload được giữ lại**, không tính vào mục tiêu 1.300: `2502.00857v2` (HintEval),
`2505.01811v4` (BadPatches), `2506.08311v3` (Automated Program Repair Agents),
`2509.04183v3` (MAGneT), `2602.19736v3` (InfScene-SR). Chỉ chuyển membership sang collection
thứ tám **“Cần xem lại — ngoài phạm vi M11”**, không xóa item/file/job/index, không trả phí về 0.
Luồng chuyển cũng có checkpoint chặn tự retry khi outcome chưa rõ. Bảy collection chủ đề
vẫn là target chính; tổng thư viện cuối có thể lớn hơn 1.312 vì giữ các ngoại lệ này.

Lượt client đầu dừng tại 32 receipt đã xác định, không có upload `sending` bị bỏ dở.
Chỉ gỡ orphan lock sau khi kiểm đúng PID client đã thoát; không dừng process theo tên.

## Kế toán

Ledger append-only/fsync trước PUT, lock một process, ngoài repo ở
`evidence\m11\cost\ledger.jsonl`. Mỗi upload giữ ceiling cho **toàn bộ** input byte UTF-8
body/abstract đã parse bằng `pypdf-6.21.1-layout-v2` × 4 attempt × giá trần embedding hiện có.
Một byte UTF-8 chặn trên một token; reuse chunk chỉ giảm phí. Lỗi parser xác định vẫn đi
luồng sản phẩm; preflight không xác định được bound thì không upload.

**Thiếu receipt không trả reservation về 0.** Job xong vẫn giữ toàn bộ ceiling trong cam kết.
Dừng trước thao tác mới nếu tổng cam kết + ceiling mới vượt $24. Ledger theo operation
(mỗi upload bao trọn nhiều provider call), không tái dùng ngân sách/ledger 400 call M7c.

`/key` OpenRouter được đọc **bên trong API container**, key chỉ từ env demo hiện có; chỉ các
trường usage số được trả ra. Key nhóm dùng chung: delta có thể gồm người khác, **không phải
billed riêng M11**. Product API rc2 không lưu receipt/provider generation ID embedding;
không thay code/image hay gắn observer. Bàn giao tách cam kết fail-closed và delta provider
quan sát được; không giả chi phí estimate là invoice.

## Backup và kiểm

Trước nạp: backup `20261023T130947Z-e981fa49` **ok**, 12 object, dump **2.193.316 byte**,
ciphertext **10.058.385 byte**, cửa sổ bảo trì **19,42 s**, cả lượt **27,73 s**, reopen `ok`.
Ciphertext SHA-256 `1e7755e3c05d36dbb62b01a4e30742a259220b02daf8c9c0dfa57b43af7245d3`.
Lượt này dùng `--no-alert`. Baseline có backup preflight fail lúc 11:30 UTC, giữ lịch sử;
lượt vừa chạy thành công. Replica equal seq 13 trước nạp; maintenance null.

Offline: **65/65 pass** (`test_m11_tooling.py` 24 + `test_m7c_tooling.py` 41), kiểm local state,
CA/leaf, env/provider/image preservation, chỉ edge, mutation không retry, parser drift và ledger giữ trần.
Checkpoint ghi temp → flush/fsync → atomic replace; ledger có admission nhưng thiếu binding checkpoint
thì chặn upload lại. Không dùng việc checkpoint thiếu để suy ra request chưa gửi.
Lượt sandbox đầu 3 pass/9 setup error do quyền tmp; sau mở quyền chạy được. Assertion mới
ban đầu dùng sai tên env provider (53 pass/1 fail), đã sửa theo `OPENROUTER_API_KEY`.
Không cộng các lượt thành nhiều test độc lập. JUnit cuối được giữ theo hash ở
`evidence/m11/offline-tooling-65-86299274ff76.xml` (SHA-256 đầy đủ trong inventory).
Các JUnit phát triển trước đó đã bị ghi đè; giữ tóm tắt/exit trong nhật ký tool, không coi chúng
là raw artifact còn tồn tại hoặc dùng hash của lượt 64 test cho lượt cuối 65 test.

CI `39969594383` tại `487b098` đã kết thúc thành công: API **1.307/1.307 pass**,
Web test/typecheck/build và Pilot deploy config contract đều success. Đây là head trước sửa
inventory chunk (offline cuối 65 test).
CI checkpoint kế tiếp `40051049793` tại `25d50c6` cũng đã kết thúc: API **1.308/1.308 pass**,
Web test/typecheck/build và Pilot deploy config contract đều success. Chưa phải CI của head cuối;
vẫn phải chờ sau khi cập nhật toàn bộ kết quả.

Backup sau nạp và 10–15 câu hỏi: **chưa xong**, sẽ cập nhật. Không chạy lại đánh giá M7c
hay ghi human verdict. Git ngoài sandbox xác nhận bundle human-review không bị xóa/thay đổi;
trạng thái D ở lượt sandbox đầu do permission denied.

## Chạy tiếp/tải lại

```powershell
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py scripts/m11_papers.py discover --target 1300
& $py scripts/m11_papers.py fetch
& $py scripts/m11_papers.py curate  # rà topic từ OAI cache, không xóa PDF/item
& $py scripts/m11_library.py run --watch --target 1300
& $py scripts/m11_library.py inspect
& $py scripts/m11_demo.py  # chỉ sau khi đủ target manifest và toàn bộ job đã kết thúc
& $py scripts/m9_lan_demo.py verify
```

Không chạy hai `run` cùng lúc. `uploads.json` lưu receipt/job IDs và trạng thái; record
`sending` chưa có ID khiến script dừng, phải đối chiếu thư viện trước, không tự resend PUT.
Sau máy/phiên chạy dừng, mở Docker Desktop và kiểm PID trong `cost/ledger.lock`. Chỉ gỡ lock
nếu đúng PID đó đã thoát và không có client M11 khác; không tự gỡ lock đang hoạt động.
`run` poll lại job có receipt trước khi nhận paper mới. Không chạy lại toàn bộ câu hỏi trả phí
khi `smoke.json` đã có record; kiểm outcome chưa rõ trước.
PDF đã có không tải lại; fetch kiểm lại license và checksum. Version/license thay đổi thì
từ chối thay vì âm thầm đổi bytes trong manifest. Fetch thành công tạo lại `ATTRIBUTION.md`
kể cả ở thư mục mới; giữ ghi công cũ nếu thư mục đã có corpus M7c.
