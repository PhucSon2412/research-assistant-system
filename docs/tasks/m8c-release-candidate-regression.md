# M8c — bản ứng viên phát hành: hồi quy tổng và nghiệm thu cô lập trên một bộ image

Bạn là coding agent tiếp theo. Gói E đã đủ phần coding (M8a + M8b). Trước cutover thật, [kế hoạch MVP](../mvp-delivery-plan.md) yêu cầu **hồi quy tổng trước phát hành**. Task này đóng băng một bản ứng viên trên `main` và chạy lại toàn bộ lớp chứng cứ trên **cùng một bộ image/source**, gồm:

- full inventory API;
- năm restore drill C4;
- smoke deploy M8a qua HTTPS/Chrome;
- diễn tập vận hành M8b;
- web.

Mục tiêu là một gói chứng cứ duy nhất, nhất quán để cutover dựa vào. **Không thêm tính năng.** Chỉ sửa lỗi thật có tái hiện, kèm regression.

## 1. Base và phạm vi

- PR #67 đã merge: đã fetch/xác minh `origin/main = e0dcdb472506a49691edffdbee6e4071a3f17d5f` (parent `0de10f4` + head review `68351dc`), tree khớp `cd71eac`, CI run `39431941809` trên head pass ba job. Source migration **0052**; không suy DB dev.
- Fetch lại, xác minh base, tạo `chore/m8c-release-candidate` từ `origin/main`. Working tree có docs orchestrator/reviewer modified/untracked (report PR #67 review lại, task này, trạng thái AGENTS/CLAUDE/kế hoạch/README/decisions/roadmap/handover): **mang vào PR**, không reset/stash/discard.
- Đọc [handover](../orchestrator-handover.md), [C4](../c4-integrated-runtime-cutover.md), [M8a](../m8a-pilot-entry-hardening.md), [M8b](../m8b-pilot-operations.md), review [PR #66](../reviews/pr66-m8a-pilot-entry-hardening.md)/[PR #67](../reviews/pr67-m8b-pilot-operations.md), và các inventory C4/M6d/M6e/M8a/M8b.
- Không cutover, không host/S3/SMTP/domain thật, không đọc `.env`/key, không đụng dev `ras`/Dify, không gọi provider/arXiv, không gửi mail thật, không đổi `pricing_verified`, không điền phiếu human M7b, không tạo tag/release GitHub.

## 2. Đóng băng bản ứng viên

- Build một lần API/web/ops image từ source `e0dcdb4` (cộng chỉ các sửa lỗi của task này nếu có). Ghi digest, đối chiếu bytes file `app/`/`migrations/` trong image với host.
- Mọi lớp kiểm dưới đây dùng **đúng** các digest đó; đổi source thì build lại và chạy lại các lớp bị ảnh hưởng, ghi riêng từng lượt.
- Kiểm `deploy/compose.yaml` cài mới từ registry theo digest pin (MinIO Chainguard, Caddy, PostgreSQL, Redis). Ghi image nào chưa pin digest; chỉ pin khi đã kiểm manifest/health.

## 3. Các lớp chứng cứ phải chạy

1. **Full inventory API.**
   - Collect toàn bộ node trên source đóng băng, đối chiếu với inventory C4 (5.163) và các node thêm sau đó (M6d…M8b): không thiếu, không trùng, mọi node mới có nguồn gốc PR.
   - Chạy hết theo shard cô lập (DB/journal/MinIO/Redis riêng); dùng file args để tránh `WinError 206`.
   - Báo pass/fail/error/skip/not-run theo node. Không deselect vĩnh viễn, không retry đến xanh rồi giấu failure; rerun giữ riêng.
2. **Năm restore drill C4** (backup/restore, session, reset token, review closure, invitation) trên image đóng băng.
3. **Smoke deploy M8a** HTTP + Chrome qua HTTPS với CA thử, SMTP capture.
4. **Diễn tập M8b** các phase `install`, `replica`, `backup`, `disaster` (a–e), `alerts`, `drill` (kể cả ca env drill dùng chung thư mục), có Chrome sau mất host.
   - Phase `upgrade` từ M8a chỉ chạy nếu image/compose đổi so với lượt chốt PR #67.
5. **Web:** `npm ci|test|run typecheck|run build --prefix web`.
6. **CLI evaluation** như C4 (`validate` + `run` trên context riêng). Nhãn giữ `pending_human_review`; không chấm semantic.

## 4. Failure lịch sử chưa rõ nguyên nhân

Thử tái hiện có giới hạn thời gian (tải CPU, lặp, cô lập file); xác lập nguyên nhân nếu được:

- M6d: discovery barrier và orphan clock smoke (`orphan_clock_invalid`);
- M8b: `test_m8a_smtp_delivery.py::test_recipient_reject_is_a_failure` từng nhận `smtp_timeout`;
- mọi failure mới trong full inventory.

Lỗi thật thì sửa hẹp + regression fail-trước/pass-sau. Không tái hiện thì giữ đúng nhãn "chưa rõ nguyên nhân", không quy cho môi trường. Không nới timeout/assertion nghiệp vụ để xanh. Nếu buộc phải đổi timeout của test, ghi lý do đo được và giữ kỳ vọng hành vi.

## 5. Bàn giao một PR

Commit/push và mở **một PR tiếng Việt**, không merge, giữ author/committer, không AI co-author/trailer/promo. Handoff `docs/m8c-release-candidate.md` + inventory `docs/m8c-test-inventory.json` gồm:

- source/commit, digest từng image, lệnh đã thực chạy, kết quả theo lớp và theo node; failure/rerun giữ riêng; not-run chính xác.
- Bảng **điều kiện cutover**: những gì đã có chứng cứ cô lập, những gì cần hạ tầng/người thật. Danh sách thông tin chủ dự án phải cấp lấy từ handoff M8b, kèm runbook cutover theo thứ tự lệnh.
- Cập nhật kế hoạch MVP: checklist nào có thêm chứng cứ; không đánh dấu "Vận hành nhóm thử" là đạt trước cutover thật.

Cleanup như các task trước: inventory Docker trước/sau, chỉ dọn project thử sau kiểm label/path; không `taskkill` theo tên process.

**Inventory khi giao:** chỉ đọc docs/Git, fetch xác minh merge/tree PR #67 và CI; chưa chạy app/test/Docker. Task **mới giao/chưa triển khai/kiểm**.
