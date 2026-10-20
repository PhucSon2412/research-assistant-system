# M8b — vận hành nhóm thử: journal độc lập, backup có lịch, alert và nghiệm thu restore

Bạn là coding agent tiếp theo. Triển khai **một PR hoàn chỉnh** khép phần vận hành còn lại của **gói E** trong [kế hoạch MVP](../mvp-delivery-plan.md): bản sao journal ngoài máy, backup có lịch/mã hóa/retention ngoài máy, alert tối thiểu có runbook và diễn tập restore trên topology `deploy/` của M8a. Mục tiêu là khi chủ dự án cấp host/storage/operator thật, cutover chỉ còn là điền cấu hình và chạy runbook đã diễn tập, không phải thiết kế thêm.

## 1. Base và phạm vi ủy quyền

- PR #66 đã merge: đã fetch/xác minh `origin/main = 0de10f4c7d119bd0190a673a52cc99b0e98ca99f` (parent `3b59a1d` + `5ca3e4c`), head review `5ca3e4c` là ancestor, tree khớp `119d604`. CI run `39379651177` trên head pass ba job. Source migration **0052**; không suy revision DB dev hoặc cộng pass mới tại merge.
- Fetch lại, xác minh base mới nhất, tạo `feat/m8b-pilot-operations` từ `origin/main`. Working tree đang có docs orchestrator/reviewer modified/untracked (AGENTS, CLAUDE, README, kế hoạch MVP, decisions, roadmap, handover, report/prompt PR #66, task này): **mang theo vào PR**, không reset/stash/discard.
- Đọc trước: [handover](../orchestrator-handover.md), [handoff M8a](../m8a-pilot-entry-hardening.md), [C4](../c4-integrated-runtime-cutover.md), [C3 operator](../c3-operator-auth-recovery.md), mục cleanup/restore/backup trong [dev-setup](../dev-setup.md), [nonfunctional](../nonfunctional.md), [access/data](../access-and-data.md#hợp-đồng-thu-hồi-và-xóa), `api/app/journal.py`, `api/app/backup.py`, `api/app/recovery.py`, `scripts/ras_backup.py`, `scripts/drill_backup_restore.py`, `deploy/`.
- Các lựa chọn dưới là **orchestrator chọn theo ủy quyền hoàn thành MVP**, không ghi như chủ dự án đã chọn nhà cung cấp/host/người trực. Triển khai ngay, không dừng ở proposal. Nếu source chứng minh một lựa chọn vi phạm bất biến, chọn phương án nhỏ nhất thay thế, ghi lý do trong decisions/handoff và báo rõ; không mở rộng scope.
- Chưa có host, storage ngoài máy, khóa mã hóa, người nhận alert, SMTP relay hay domain thật. Dùng tài nguyên tổng hợp cô lập; không tự mua dịch vụ, tạo tài khoản cloud, gửi mail thật hay cutover.

## 2. Journal: bản sao ngoài máy, không đổi đường ghi

Đường ghi hiện hành (append + `fsync` dưới journal advisory lock, mirror/confirm, gate) **giữ nguyên**. Không thêm network I/O vào đường ghi, không giữ DB/journal lock qua network, không để gate/readiness phụ thuộc remote. Orchestrator chọn **replica bất đồng bộ, chỉ thêm, có kiểm chuỗi**:

- Service/one-shot `journal-replicator` (cùng image API, user không root) mount journal **read-only**, không có credential DB/originals/backup. Chỉ đọc dòng hoàn chỉnh đã có newline, parse/kiểm hash chain bằng `app.journal`, không bao giờ gửi torn tail.
- Đích là **S3-compatible do người vận hành cấp**, bucket/credential riêng, khác originals và khác kho backup; yêu cầu versioning, dùng object lock nếu đích hỗ trợ. Ghi đối tượng bất biến theo `journal_id`/khoảng seq với ghi có điều kiện (không ghi đè), cộng một head record có seq/hash/thời điểm. Credential qua secret file, không argv/log.
- Replica phát hiện **fork** (prefix local khác prefix đã ship, journal_id/genesis khác, seq lùi) → dừng ship, ghi trạng thái lỗi, alert; không ghi đè, không tự sửa journal. Đích lỗi/chậm không ảnh hưởng request; chỉ tăng lag và alert.
- Công cụ lấy replica về thư mục bản sao journal (kiểm chain/journal_id/genesis) để dùng với `restore`. Khi có cả journal local và replica: chọn chuỗi dài hơn **chỉ khi** chuỗi kia là prefix của nó; phân kỳ → từ chối, operator quyết định. Không ghép/splice hai chuỗi.
- Đối chiếu ba bản: backup manifest (`journal_at_backup`), journal local, replica. Ca DB + journal local cùng bị quay về cũ trong khi replica có head mới hơn phải bị phát hiện và từ chối/giữ hold, không mở traffic (đây là ca `check_journal_for_backup` hiện ghi “không phát hiện được”).
- Ghi rõ giới hạn còn lại: mất host thì mất các record sau lần ship thành công cuối; khoảng này là cửa sổ rủi ro cho revoke/xóa/auth closure, được đo và alert, **không** phải zero-RPO. Replica không bao giờ bị prune; journal không bao giờ nằm trong artifact backup.

## 3. Backup cho topology deploy: lịch, mã hóa, ngoài máy, retention

Tận dụng `app.backup` (snapshot, manifest self-hash, SHA-256 từng object, verify) và hợp đồng writer dừng. Không đọc `.env` dưới bất kỳ fallback nào ở chế độ deploy: chỉ `--env-file`/secret file tường minh, `-p` và `-f deploy/compose.yaml` bắt buộc; thiếu → từ chối. Deploy không có cổng host cho DB/MinIO: chạy dump/copy object trong one-shot trên mạng `backend`. Credential đủ đọc toàn DB chỉ ở one-shot backup/restore, không ở api/worker/beat/web/replicator.

- **Cửa sổ bảo trì** (orchestrator chọn thay online backup): dừng api/web/worker/beat, edge trả trang/mã bảo trì rõ ràng (không 502 mơ hồ), backup + verify, rồi khởi động lại và kiểm readiness. Mọi đường lỗi sau khi đã dừng writer phải cố khởi động lại và alert; không để hệ thống tắt im lặng, không tự đặt/xóa hold ngoài hợp đồng recovery. Có khóa chống chạy chồng và timeout từng bước; đo thời gian cửa sổ.
- **Mã hóa trước khi rời host:** định dạng `age` với recipient public key; private key không nằm trên server (operator giữ). Dùng công cụ/thư viện được duy trì, pin version, không tự viết crypto. Artifact rõ chỉ tồn tại trong staging quyền hạn chế và bị xóa sau khi bản mã hóa đã upload và kiểm (hash ciphertext đọc lại từ đích). Restore giải mã trong context cô lập với khóa thử.
- **Đích backup ngoài máy:** S3-compatible riêng, khác bucket/credential journal replica, ghi bất biến/versioning nếu có. Lịch mặc định hằng ngày (giờ cấu hình được). Retention mặc định theo `retention.backup_retention` (30 ngày): chỉ prune artifact đã có bản mới hơn verify thành công, luôn giữ tối thiểu N bản tốt, không prune khi lượt gần nhất lỗi. Các giá trị là cấu hình vận hành khởi đầu, **không** phải RPO/RTO đã đạt.
- **Scheduler:** một entrypoint host (ví dụ `scripts/ras_ops.py backup|replicate|check|drill`) cộng unit `systemd` timer mẫu trong `deploy/` (host đích dự kiến Linux). Logic chạy được và kiểm được trực tiếp trên Windows/container; validate unit file nếu khả thi, không tuyên bố đã chạy systemd thật nếu chưa chạy.

## 4. Alert tối thiểu có runbook

Một lệnh kiểm (`check`) chạy định kỳ, mỗi điều kiện có mã ổn định, ngưỡng cấu hình và mục runbook xử lý:

- gate/readiness đóng (journal thiếu/torn/unconfirmed/mismatch, hold, recovery lỗi); replica lỗi/fork/lag vượt ngưỡng; backup gần nhất thành công + verify quá cũ hoặc lượt gần nhất lỗi; cửa sổ bảo trì không mở lại được; cleanup revoke/xóa private quá `purge_due_at`; diễn tập restore quá hạn (mặc định tháng); dung lượng journal/staging thấp.
- Kênh: **email qua adapter SMTP TLS hiện có** tới người nhận cấu hình; dedupe/thông báo hồi phục bằng state file; nội dung chỉ mã, số đếm, thời điểm, không email User/paper/token/path private/secret. Gửi alert lỗi → exit khác 0 và log mã. Ghi rõ cần thêm dead-man/probe ngoài host ở cutover; không tự đăng ký dịch vụ ngoài.
- Không xây dashboard/metrics stack. Quyền đọc của `check` tối thiểu; không dùng migrator trong service chạy lâu.

## 5. Kiểm chứng và tiêu chí hoàn thành

Mọi thứ trên project Docker cô lập (project/port/volume/journal/credential riêng, provider key trống, pricing false). Hai MinIO thử riêng mô phỏng đích journal và đích backup **ngoài máy**; ghi rõ cùng máy chỉ là mô phỏng, không chứng minh độc lập miền sự cố. SMTP capture như M8a.

1. **Cài mới và nâng cấp:** cài mới deploy có M8b; nâng cấp một deploy M8a có dữ liệu theo runbook (hold → migrate/provision nếu cần → recover). Readiness bằng hậu điều kiện thật.
2. **Replica:** ship liên tục khi có revoke/xóa/auth closure; torn tail không ship; đích dừng → serving vẫn chạy, lag tăng, alert; đích trở lại → bắt kịp; fork/tamper/journal_id khác → dừng + alert, không ghi đè.
3. **Backup theo lịch:** chạy entrypoint như scheduler gọi; cửa sổ dừng/mở writer, trang bảo trì, mã hóa, upload, đọc lại hash; lỗi giữa chừng vẫn mở lại writer + alert; retention bằng đồng hồ/artifact tổng hợp không xóa bản tốt cuối. Không có plaintext còn lại ngoài staging, không secret trong argv/log/artifact.
4. **Diễn tập thảm họa:** (a) DB về backup cũ, journal hiện hành → replay, revoke/xóa private/closure/session không hồi sinh; (b) **mất host**: xóa volume + journal local, restore từ backup mã hóa ngoài máy + replica vào project sạch → hold → recover → readiness; (c) replica cũ hơn backup → từ chối; (d) DB + journal local cùng quay về nhưng replica mới hơn → bị phát hiện, không mở traffic; (e) replica bị sửa/phân kỳ → từ chối. Đo thời gian restore và cửa sổ mất record, ghi là số đo, không phải SLO.
5. **Alert:** mỗi điều kiện ở mục 4 kích một mail capture, dedupe, hồi phục; mail không chứa dữ liệu nhạy cảm.
6. **Chrome qua HTTPS sau restore (b):** login, library, tải original của item còn quyền, item đã revoke/xóa là 404, phiên đã thu hồi không dùng lại được.

Countercheck có chủ đích (không chạy theo quota): ship torn tail, ghi đè replica, bỏ kiểm fork, chọn chuỗi ngắn hơn/splice, backup đọc `.env`, giữ plaintext sau upload, prune bản tốt cuối, quên mở lại writer, alert lộ dữ liệu. Mỗi đột biến phải làm fail test nhắm; giữ source hash trước/sau.

Chọn hồi quy theo rủi ro: file đụng `journal`/`backup`/`recovery`/deploy và restore drill liên quan. Nếu buộc phải sửa đường ghi journal/gate/schema, phải chạy năm restore drill C4 và hồi quy rộng tương ứng; migration mới chỉ khi thật cần, sau head thực tế. Cập nhật CI nếu có test mới chạy được không cần secret; không gọi selection là full suite.

Chụp inventory Docker trước/sau, chỉ dọn project thử sau kiểm label/path. **Không** `taskkill` theo tên process; chỉ dừng PID/project mình tạo. Không query/migrate/restart dev `ras`, không đụng Dify, không đọc key/`.env`, không gọi provider/arXiv, không gửi mail thật, không đổi `pricing_verified`, không điền phiếu human M7b.

## 6. Bàn giao một PR

Commit/push và mở **một PR tiếng Việt**, không merge, giữ author/committer, không AI co-author/trailer/promo. Handoff `docs/m8b-pilot-operations.md` + inventory `docs/m8b-test-inventory.json` gồm:

- Topology mới (service, mạng, mount, credential/secret của từng thành phần), runbook: cài mới, nâng cấp, lịch backup, kiểm replica, xử lý từng alert, restore khi DB rollback và khi mất host, diễn tập tháng.
- Lệnh đã thực chạy, kết quả pass/fail/error/skip/not-run, failure/rerun giữ riêng, số đo cửa sổ bảo trì/restore/lag.
- **Danh sách chủ dự án phải cấp trước cutover:** host, hai đích S3-compatible ở miền sự cố khác host và khác nhau, cặp khóa `age` và người giữ private key, người nhận alert/người chịu trách nhiệm recovery, SMTP relay, domain, giờ cửa sổ bảo trì, probe ngoài host.
- Cập nhật kế hoạch MVP (gói E, checklist “Vận hành nhóm thử” chỉ đánh dấu phần đã có chứng cứ cô lập), decisions, access/data/nonfunctional nếu giới hạn restore thay đổi, mục lục. Không nhân bản lịch sử vào mọi file.

**Sau M8b:** cutover thật là task riêng khi có hạ tầng/operator; D (phiếu human, cache-write live với ngân sách mới, policy/paper thật) đi riêng. M8b không cho phép bật pricing hay tuyên bố production đã chạy.

**Inventory khi giao:** chỉ đọc source/docs/Git, fetch xác minh merge/tree PR #66 và trạng thái CI; chưa chạy app/test/Docker/storage/SMTP. Task **mới giao/chưa triển khai/kiểm**.
