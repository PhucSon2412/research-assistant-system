# M11 — thư viện cá nhân trên máy và kho paper mở rộng

**Đang triển khai (2026-10-23, Asia/Saigon).** Base đã fetch/xác minh
`1f7bfac970fa0015a6d3211d3514a4136df4d4d6` (PR #71 MERGED), nhánh
`ops/m11-demo-knowledge-base`. Chỉ dẫn mới của chủ dự án thay 150/$10 bằng
**khoảng 300 paper bổ sung/$30**, dừng gọi mới ở **$24**. Không merge.

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

- CC BY 4.0 / CC BY-SA 4.0 / CC0 1.0; arXivRaw OAI phải trùng trang abs đúng version,
  và version đó phải latest trong OAI. Không nhận NC/ND/license mặc định arXiv.
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

**Manifest đã khóa 300 paper đúng phạm vi**: 282 CC BY 4.0, 14 CC BY-SA 4.0, 4 CC0 1.0;
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

Trạng thái nạp cuối, trang/chunk/object và câu hỏi: cập nhật sau khi toàn bộ job kết thúc.

### Sửa bộ lọc chủ đề trong lúc nạp

Bộ lọc ban đầu tìm chuỗi `rag`, khớp cả “average”/“paragraph”, và có thể gán nhầm nhóm đánh giá
cho paper ngoài retrieval. Đã sửa thành word boundary, bổ sung regression âm/dương, rà lại OAI
đã cache và tiếp tục tải bù. **48 PDF đã tải bị loại khỏi target**; PDF ngoài repo được giữ nguyên.
[Danh sách metadata/SHA-256 của các ngoại lệ](../evaluation/m11-papers/topic-exclusions.json).

**5 item đã upload được giữ lại**, không tính vào mục tiêu 300: `2502.00857v2` (HintEval),
`2505.01811v4` (BadPatches), `2506.08311v3` (Automated Program Repair Agents),
`2509.04183v3` (MAGneT), `2602.19736v3` (InfScene-SR). Chỉ chuyển membership sang collection
thứ tám **“Cần xem lại — ngoài phạm vi M11”**, không xóa item/file/job/index, không trả phí về 0.
Luồng chuyển cũng có checkpoint chặn tự retry khi outcome chưa rõ. Bảy collection chủ đề
vẫn là target chính; tổng thư viện cuối có thể lớn hơn 312 vì giữ các ngoại lệ này.

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

Offline: **63/63 pass** (`test_m11_tooling.py` 22 + `test_m7c_tooling.py` 41), kiểm local state,
CA/leaf, env/provider/image preservation, chỉ edge, mutation không retry, parser drift và ledger giữ trần.
Checkpoint ghi temp → flush/fsync → atomic replace; ledger có admission nhưng thiếu binding checkpoint
thì chặn upload lại. Không dùng việc checkpoint thiếu để suy ra request chưa gửi.
Lượt sandbox đầu 3 pass/9 setup error do quyền tmp; sau mở quyền chạy được. Assertion mới
ban đầu dùng sai tên env provider (53 pass/1 fail), đã sửa theo `OPENROUTER_API_KEY`.
Không cộng các lượt thành nhiều test độc lập.

Backup sau nạp, 10–15 câu hỏi và CI: **chưa xong**, sẽ cập nhật. Không chạy lại đánh giá M7c
hay ghi human verdict. Git ngoài sandbox xác nhận bundle human-review không bị xóa/thay đổi;
trạng thái D ở lượt sandbox đầu do permission denied.

## Chạy tiếp/tải lại

```powershell
$py = "$env:USERPROFILE\.conda\envs\ras\python.exe"
& $py scripts/m11_papers.py discover --target 300
& $py scripts/m11_papers.py fetch
& $py scripts/m11_papers.py curate  # rà topic từ OAI cache, không xóa PDF/item
& $py scripts/m11_library.py run --watch --target 300
& $py scripts/m11_library.py inspect
& $py scripts/m11_demo.py  # chỉ sau khi đủ 300 manifest và toàn bộ job đã kết thúc
& $py scripts/m9_lan_demo.py verify
```

Không chạy hai `run` cùng lúc. `uploads.json` lưu receipt/job IDs và trạng thái; record
`sending` chưa có ID khiến script dừng, phải đối chiếu thư viện trước, không tự resend PUT.
PDF đã có không tải lại; fetch kiểm lại license và checksum. Version/license thay đổi thì
từ chối thay vì âm thầm đổi bytes trong manifest.
