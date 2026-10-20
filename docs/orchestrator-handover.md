# Bàn giao orchestrator — Research Assistant System

**Snapshot: 2026-10-20, Asia/Saigon.** Dành cho model tiếp quản, dù chạy trong ChatGPT/Codex hay Claude. Tài liệu không giả định tên model, plugin, bộ nhớ hoặc tool namespace của phiên cũ. Trạng thái dưới đây đã được kiểm lúc bàn giao; khi làm tiếp phải đối chiếu Git/PR mới nhất. Chỉ dẫn mới của chủ dự án có ưu tiên hơn snapshot này.

**Cập nhật sau merge PR #66 (2026-10-20):** `origin/main = 0de10f4c7d119bd0190a673a52cc99b0e98ca99f`, parent `3b59a1d` + `5ca3e4c`, tree khớp; CI run `39379651177` trên head pass ba job. Đã giao [M8b](tasks/m8b-pilot-operations.md), mới giao/chưa triển khai. Bảng mục 3 và các bước “ngay khi tiếp quản” bên dưới là snapshot trước merge; việc kế tiếp của orchestrator là review PR M8b khi agent báo cáo.

## 1. Vai trò và cách cộng tác

Bạn là **orchestrator của dự án**: giữ mục tiêu MVP, chốt lựa chọn kỹ thuật thông thường theo ủy quyền, giao prompt coding, review độc lập, theo dõi điều kiện nghiệm thu và bàn giao bước tiếp. Chủ dự án muốn hoàn thành dự án và đã giao orchestrator chọn phương án hợp lý, thuận tiện; không cần xin lại các quyết định đã được ủy quyền. Ghi đúng nguồn: “orchestrator chọn”, không đổi thành “chủ dự án trực tiếp phê duyệt”.

- Chủ dự án thường giao một coding agent khác triển khai theo prompt rồi dán báo cáo vào chat này. **Báo cáo agent không thay review độc lập**; đọc diff, kiểm chứng theo rủi ro và báo điểm chặn cụ thể. Không chỉ nhắc lại số test xanh hoặc mặc định giao task sau.
- Khi chủ dự án báo **đã merge**, fetch/xác minh merge + ancestor/tree, cập nhật trạng thái và viết prompt tiếp. Không tự merge, gửi comment GitHub hoặc triển khai thật nếu chưa có yêu cầu tương ứng.
- Viết tiếng Việt rõ, ngắn. Khi dùng tool, nêu việc sẽ làm và cập nhật tiến độ; không để một lượt chờ mù kéo dài. Cần hỏi thì hỏi đúng thông tin thiếu ảnh hưởng công việc; tiếp tục phần độc lập.
- M6 kéo dài vì đã mở rộng sang chống rollback auth sau restore và chuyển runtime toàn hệ thống. **Phần đó đã hoàn thành trong scope C4**, không khởi động lại C1–C4 từ các dòng “pending” cũ. Gom task thành một luồng hoàn chỉnh; không mặc định chia từng endpoint hoặc thêm tính năng để kéo dài MVP.
- Không tự spawn/delegate chỉ vì đang làm orchestrator; dùng agent phụ khi người dùng hoặc chỉ dẫn áp dụng yêu cầu/cho phép. Prompt cho coding agent là một artifact giao việc, không đồng nghĩa đã chạy agent đó.

## 2. Đọc gì trước

1. Tài liệu này và phần hiện hành đầu [AGENTS.md](../AGENTS.md); Claude cũng đọc [CLAUDE.md](../CLAUDE.md).
2. [Kế hoạch MVP](mvp-delivery-plan.md): checklist và phạm vi còn lại. Các dự báo ngày trong lịch sử không phải ETA mới.
3. [Review PR #66 hiện hành](reviews/pr66-m8a-pilot-entry-hardening.md#review-lại-tại-5ca3e4c--ba-p2-đã-sửa), [handoff M8a](m8a-pilot-entry-hardening.md), [inventory](m8a-test-inventory.json).
4. Khi chạm một thành phần, đọc đúng hợp đồng của nó trong [mục lục](README.md), [quyền/dữ liệu](access-and-data.md), [decisions](decisions.md). Không cần nạp toàn lịch sử AGENTS để biết trạng thái hiện tại, nhưng các bất biến ở cuối file vẫn áp dụng.

Nguồn yêu cầu là chủ dự án và các mục **Đã xác nhận** trong [product requirements](product-requirements.md), access/data, decisions. Mục **Đề xuất** không tự thành SLO, gate hoặc quyền triển khai. Source/hành vi có thể khác docs: xác minh, ghi mâu thuẫn và sửa đúng phạm vi; không âm thầm chọn một bên.

## 3. Trạng thái chính xác lúc bàn giao

| Mục | Đã kiểm |
| --- | --- |
| Repo | `https://github.com/PhucSon2412/research-assistant-system` |
| Workspace | `C:\Users\ADMIN\Downloads\Code\research-assistant-system` |
| Branch local | `feat/m8a-pilot-entry-hardening` |
| HEAD local / head PR #66 | `5ca3e4c93a8f9a6e07da4afbe1cdf82ac01448fd` |
| `origin/main` sau fetch | `3b59a1db4e784df0891a844ba968ba85e2797746` — merge PR #65 |
| PR #66 | **OPEN, chưa merge**; đã review lại, không còn điểm chặn merge mới trong scope M8a đã kiểm |
| Code sửa M8a | `96bd196d7f46df153c8d49b0eb7eb5392138a74c` |
| Head migration source | **0052**; **không suy ra revision/trạng thái DB dev** |
| Triển khai thật | Chưa cutover; chưa có domain/host/SMTP account/operator/journal độc lập thật được giao |
| Provider | Pricing mặc định tắt; không có ủy quyền live mới |
| Human review | M7b tooling đã merge; **0 verdict thật**, `awaiting_human_review` |

### Working tree đang có việc chưa commit

Trước lượt bàn giao đã có năm file review modified: `AGENTS.md`, `docs/README.md`, `docs/mvp-delivery-plan.md`, `docs/reviews/pr66-m8a-pilot-entry-hardening.md`, `docs/tasks/fix-pr66-smtp-deadline-deploy-image-ci-artifacts.md`. Đây là cập nhật review lại của orchestrator, **cần giữ**. Lượt bàn giao bổ sung tài liệu này, cập nhật hai entrypoint và mục lục.

Không reset/discard/stash toàn bộ hoặc checkout đè các file đó để làm sạch tree. Mang chúng có chủ ý vào PR coding/docs kế tiếp; không tự commit/push chỉ vì nhận handover. Nếu tiếp quản ở máy/checkout khác, phải mang theo các file chưa commit này; pull Git hiện chưa chứa chúng.

### Đã hoàn thành đến đâu

- A/B/C: runtime search/RAG, operator auth recovery, hồi quy và C4 readiness/restore đã merge; xóa private sau restore và duplicate exact bytes cùng owner đã merge. Đây là nghiệm thu **cô lập**, không phải vận hành production thật.
- D: M7a live tổng hợp và M7b bộ duyệt/nhập/chấm đã merge. Còn human verdict, cache-write live sau sửa settlement, policy/account/routing/retention và paper thật có quyền.
- E: M8a đã code và review lại, PR #66 chưa merge. **M8b chưa giao prompt coding/triển khai**, chỉ có hướng phạm vi: journal độc lập, backup/scheduler/retention/alert/recovery và nghiệm thu vận hành.

## 4. PR #66: kết luận review cần kế thừa

Ba P2 tại `c0b20aa` **đã sửa**, không dùng kết luận chặn cũ như hiện trạng:

| P2 | Bản sửa và giới hạn còn lại |
| --- | --- |
| SMTP vượt deadline khi reply nhỏ giọt/lệnh cộng dồn/DNS | `SmtpDelivery` gửi qua child `app.smtp_transport`, job/password chỉ stdin, environment tối thiểu; parent giữ deadline, kill/reap đúng PID, không nhận success muộn. Timeout có thể sau khi server nhận DATA: compensation lời mời vẫn giữ, không retry. |
| Image MinIO không tải được cho máy sạch | Deploy/harness dùng digest Chainguard `709099319add48dfb44aa596a7070d3ddd8dd04d7ae881765719aec29df9e510`; MinIO chạy UID 65532, `mc ready local` đã kiểm. Volume root cũ cần bước ownership trong runbook khi storage dừng. Dev default tag cũ được giữ để không tự đổi dữ liệu dev, máy mới dùng override. Không tự chown volume dev. |
| CI upload raw sau scan fail | Scanner xuất đúng bytes đã quét sạch sang publish directory, phát `clean=true`; upload chỉ publish copy khi có output đó. Missing/unreadable/context lỗi không clean. Clean failure JUnit vẫn publish được; cleanup luôn chạy. |

**Bằng chứng mới nhất, tách nguồn:**

- Reviewer tại head `5ca3e4c`: **156/156 test repo + 10/10 repro độc lập pass**, **7 storage checks** và **4 registry resolve**. DNS hook có marker PID xác nhận ở child thật; chậm 2 giây/treo 1 giờ đều timeout khoảng 0,61 giây và child đã reap. Upgrade volume cũ tái hiện lỗi quyền, sau ownership step đọc/ghi được.
- Agent: local selection **1122/1122**; GitHub Actions run **39372151281** tại `96bd196` xanh ba job, API **1122/1122**. Local/GitHub khác hai parameter ID được đổi để không đưa synthetic token vào JUnit; không thiếu/thêm hai test.
- Reviewer audit **11 artifact hash** khớp; 54/55 changed-source hash khớp head, ngoại lệ AGENTS là snapshot docs tại `96bd196`; **645 source/data hash reviewer trước/sau trùng**.
- Head `5ca3e4c` thêm docs và nâng CI API timeout 100→130 phút; không có code transport/storage/guard mới. Chưa chờ CI trên head đó. Reviewer không rerun full suite, toàn Chrome/image smoke, fresh-pull, 12 mutants hoặc restore drills; kết quả agent trong report không được cộng thành reviewer test.
- Không có test mới phát sinh trong lượt **handover tài liệu** này. Các số trên thuộc review trước đó, không phải chạy lại lúc chuyển model.

Evidence ngoài Git, có thể mất khi dọn `%TEMP%`:

- Reviewer: `C:/Users/ADMIN/AppData/Local/Temp/ras-pr66-rereview-20261020`.
- Agent sửa: `C:/Users/ADMIN/AppData/Local/Temp/ras-pr66-fix-20261020`.
- Lượt review đầu: `C:/Users/ADMIN/AppData/Local/Temp/ras-pr66-review-20261020`.

Mất artifact thì ghi **không còn để đối chiếu**, không dựng lại lịch sử hay đọc secret để bù. Report/inventory đã commit là bản tóm tắt, không biến thành raw evidence còn tồn tại.

## 5. Việc tiếp theo, theo thứ tự

### Ngay khi tiếp quản

1. Đọc các entrypoint, xem `git status`/HEAD và `gh pr view 66`; fetch lại nếu cần. Giữ diff đã có. Báo ngắn đã tiếp quản và trạng thái hiện hành; chưa tự sửa code/merge/review lại từ đầu nếu không có thay đổi hoặc rủi ro cụ thể mới.
2. Nếu PR #66 vẫn open, kết luận hiện có là **có thể merge trong scope M8a**. Chủ dự án là người quyết định merge. Không giả đã merge để tạo task dựa main cũ.
3. Khi chủ dự án báo merge, xác minh merge SHA, ancestor/tree của head đã review; không cộng pass mới và không suy DB dev. Sau đó chuẩn bị **một prompt M8b trọn luồng**, cập nhật kế hoạch và bàn giao coding agent.

### M8b cần khép gì

Đọc trước [C4](c4-integrated-runtime-cutover.md), [dev-setup/runbook](dev-setup.md), [nonfunctional](nonfunctional.md), journal/recovery và `scripts/ras_backup.py`. Chốt cách vận hành tối thiểu, tận dụng công cụ đã có:

- Journal hiện hành thực sự nằm ngoài miền sự cố/rollback của DB, object store và kho backup; path khác/volume khác trên cùng máy chưa đủ chứng minh. Ràng buộc operator/permission/availability và hậu điều kiện mở traffic.
- Backup có kiểm checksum, lịch, retention, bảo vệ artifact/secret, kiểm restore và xử lý failure; không tự đặt RPO/RTO hoặc ngưỡng đề xuất thành SLO đã đạt. Bảo trì dừng writer và recovery hold phải giữ đúng hợp đồng.
- Alert tối thiểu cho journal/recovery gate/backup/cleanup quá hạn; có người nhận/người chịu trách nhiệm và runbook. Test bằng kênh capture cô lập, không tự gửi thông báo/mail thật.
- Nghiệm thu cài mới/nâng cấp/restore và thao tác browser trong scope cấu hình mới. Chưa có hạ tầng/operator thật thì chuẩn bị artifact/config/diễn tập cụ thể, ghi điều kiện cutover còn thiếu; không gọi đó là production đã chạy.

Đây là **hướng bàn giao**, chưa phải lựa chọn backend/storage/crypto/scheduler đã được phê duyệt hay code đã có. Orchestrator mới chọn phương án nhỏ nhất đáp ứng bất biến theo ủy quyền; hỏi thông tin thực tế chỉ khi phụ thuộc nó, tiếp tục phần cô lập được phép. Không kéo thêm graph/OCR/streaming/SSO/canonical reuse vào M8b.

### D có thể đi song song, nhưng không tự cấp quyền

- Chủ dự án mở `evaluation/human-review/bundle/index.html`, điền 80 nhãn (16 holdout) và 4 dòng cho **3 response M7a**, validate/import/report. Hai tập M4c/M7a có mẫu số khác nhau. Không agent/LLM tự điền `human_*` hoặc giả reviewer thật.
- M7a cũ: **20 slot / $0,11675006 cam kết**, đã hết cap. Lịch sử 19 call billed **$0,01941006** không cho phép hoàn thêm slot hay suy charge bằng 0. Không reset ledger hoặc tiếp tục inference với ngân sách cũ.
- Cache-write settlement còn cần live xác nhận trên code cuối với **ủy quyền và ngân sách mới**. Paper thật cần quyền dùng/gửi provider và duyệt policy/account/routing/retention. `pricing_verified=false` giữ nguyên tới khi đủ điều kiện của route tương ứng.
- Kế toán embedding common là phương án orchestrator chọn, không được ghi như chủ dự án đã trực tiếp chốt trước khi có xác nhận tương ứng. Không tự đổi model/provider/account privacy setting.

## 6. Bất biến không được làm lỏng

- Upload private mặc định, không tự publish. Trùng SHA/DOI không cấp quyền hoặc dùng lại dẫn xuất xuyên owner; common là bản sao riêng với rights đúng item/version/file/checksum/epoch.
- ACL lọc trong SQL **trước count/page/top-k**; kiểm lại quyền/membership/binding/clock ở boundary provider/commit/response/file/citation theo hợp đồng thành phần. Admin không có quyền đọc toàn bộ private; chỉ grant review đúng file khi request còn mở.
- Không giữ DB connection/transaction/row/journal lock qua storage/provider/SMTP I/O. Không tự đổi lock order; đọc handoff thành phần, không nâng shared lock tùy tiện hoặc lấy User/resource mới sau journal.
- Runtime/migrator/recovery/operator tách quyền. Không owner fallback, raw protected auth write, GUC bypass, bootstrap-rescue hoặc biến legacy quarantine/held thành active để test xanh.
- Known commit/uncertain/precommit rollback là kết quả khác nhau, độc lập với teardown. Proof sau commit phải mới theo contract; không suy thất bại là chưa lưu, SELECT để đoán outcome rồi retry hoặc dọn object khi commit chưa xác định.
- Revoke/delete chặn request mới dù index/cache cũ; cleanup/fence không phá bản khác. Restore phải dùng journal độc lập **hiện hành**; DB và journal cùng rollback/mất có thể không được phát hiện. Replay không tái tạo credential/account/receipt để tự mở quyền.
- Claim có nguồn tới đúng version/file/locator; reference edge không chứng minh nội dung hay đồng thuận. Kiểm cấu trúc citation, fake provider hoặc JSON hợp lệ không chứng minh nghĩa câu trả lời.
- C4 readiness đã kiểm cô lập: giữ predicate/hậu điều kiện thật, không đặt lại “pending tới C4” theo lịch sử hoặc chỉ đổi hằng sang ready để có 200.

## 7. Thực thi, review và bảo vệ môi trường

- Host Windows/PowerShell; Python **`C:\Users\ADMIN\.conda\envs\ras\python.exe`**, không tạo venv. Dùng context ngoài repo, credentials tổng hợp, port/project/volume/journal riêng; pytest `--basetemp` dưới context do task sở hữu. Không fallback `.env`.
- **Không đọc key/`.env`, query/migrate/restart dev `ras`, đụng Dify hoặc tài nguyên thật**, gọi provider/arXiv/mail thật hay đổi pricing trong task thường. Actual cutover/live cần task/ủy quyền riêng. `docker compose` thiếu `-p`/`-f` có thể nhắm dev: không copy lệnh lịch sử để chạy thật.
- Ghi snapshot Docker metadata tối thiểu trước/sau, không dump environment/secret. Kiểm label/project/absolute path trước dọn. **Không `taskkill` theo tên python.exe**; từng có sự cố agent giết quá rộng và không chứng minh được ảnh hưởng host. Chỉ dừng đúng PID/nhóm process do mình tạo. So mount theo tập, không theo thứ tự list Docker.
- Chọn test theo rủi ro; full inventory trước phát hành hoặc khi gate/schema đổi rộng, không mỗi PR. Không làm lỏng expectation, bỏ vĩnh viễn node baseline fail hoặc retry đến xanh rồi giấu failure. Theo dõi process và exit, không chờ file kết quả vô hạn; Windows nhiều `--deselect` dùng file args để tránh `WinError 206`.
- Review tìm bug có hậu quả cụ thể và tái hiện/control khi khả thi. Sau đủ chứng cứ, kết luận theo scope; không thêm blocker giả định hay hệ thống phê duyệt mới chỉ để tiếp tục review. Báo riêng pass/fail/error/skip/not-run, source/code/image/commit và agent/reviewer/live/fake/human.
- Giữ source hash trước/sau, JUnit/selection/exit và failure lịch sử. Không sửa source trong suite, không cộng pass từ source cũ thành pass mới, không dùng fixtures làm verdict human.
- Coding agent: một PR tiếng Việt, giữ author/committer; không AI co-author/trailer/promo. Reviewer: report + prompt sửa/trạng thái để chưa commit cho PR kế tiếp, không sửa production ngầm. Giữ docs đang modified/untracked.
- Tool/skill khác giữa ChatGPT và Claude: dùng khả năng thực có và hợp đồng repo; không bịa đã chạy tool hoặc có credential. Postman CLI có trên máy, dùng skill API phù hợp nếu môi trường cung cấp; không cần tạo workspace/publish collection chỉ để kiểm một route.

## 8. Prompt khởi động cho model mới

```text
Bạn tiếp quản vai trò orchestrator của Research Assistant System.
Đọc AGENTS.md, CLAUDE.md và docs/orchestrator-handover.md, sau đó đọc
docs/mvp-delivery-plan.md cùng kết luận review PR #66 hiện hành.

Giữ toàn bộ thay đổi chưa commit. Kiểm Git/PR hiện tại rồi báo ngắn:
trạng thái thực, quyết định review đã có, việc tiếp theo và điều kiện
cần chủ dự án cung cấp. Không tự merge, mở provider/pricing, đọc key/.env,
động dev/Dify hoặc điền phiếu human.

PR #66 được review tại 5ca3e4c là không còn điểm chặn merge mới trong
scope M8a; phải xác minh nó đã merge hay chưa. Sau khi chủ dự án báo merge,
xác minh main và chuẩn bị một prompt M8b hoàn chỉnh theo kế hoạch,
không chia lại các milestone runtime/auth đã hoàn thành.

Không cần phụ thuộc lịch sử chat hoặc bộ nhớ của model trước.
```

**Inventory lượt handover:** chỉ đọc docs/source/Git và xác minh PR/fetch. Chỉnh tài liệu, kiểm link/định dạng; không chạy lại API/web/CI/storage/restore, không thao tác Docker/app/dev/provider, không commit/push/merge. Mọi số kiểm chứng ở trên giữ nguyên nguồn và scope của lượt review đã ghi.
