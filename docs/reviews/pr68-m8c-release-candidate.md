# Review PR #68 — M8c bản ứng viên phát hành

**Kết luận tại `36444ae` (2026-10-21): không thấy điểm chặn merge mới trong scope M8c.**

- Hai lỗi thật đã sửa đúng và có regression:
  - image hạ tầng chưa pin đã trôi trên registry;
  - CLI evaluation hỏng từ M8a.
- Chứng cứ full suite 5.579 node khớp artifact khi reviewer đếm độc lập.
- Có bốn điểm P3 không chặn merge, liệt kê ở mục dưới.

PR OPEN, chưa merge; source **0052**; chưa cutover. CI run `39502499929` trên `36444ae` **success** cả ba job.

## Đã kiểm độc lập

Python Conda `ras`; context ngoài repo `C:\Users\ADMIN\AppData\Local\Temp\rv68`. Không đọc `.env`, không đụng dev `ras`/Dify, không provider/mail thật.

**1. Pin digest hạ tầng.**

- Chạy offline (`--network none --pull never`) ba image pin:
  - PostgreSQL **16.14** (`pgvector@sha256:a3625087…`);
  - Redis **7.4.11** (`redis@sha256:858f009f…`);
  - Caddy **v2.10.2** (`caddy@sha256:4c6e91c6…`).
- `docker buildx imagetools inspect` (chỉ đọc registry): cả bốn digest (gồm MinIO PR #66) resolve thành OCI index, có `linux/amd64`. Tag `pgvector/pgvector:pg16` hiện trỏ `7b822b0a…`, xác nhận việc trôi tag.
- Checker trên bản sao `git archive` ngoài repo:
  - checker cũ + compose cũ: **ok** (lỗ hổng);
  - checker mới + compose cũ: **FAIL 3** (db/edge/redis);
  - checker mới + compose mới: **ok**.
- Không chạy `--check-registry`/`--settings-image` của checker.

**2. Sửa CLI evaluation**, chạy trên test host cô lập `m8a_test_host` (project `rasm8atc341d4c6`, đã dọn):

- `test_evaluation.py` trên source head: **23/23 pass**.
- Đối chứng với bản sao `api` chỉ thay `runner.py` của `e0dcdb4`:
  - ca regression mới **fail** `RuntimeError: login failed for admin: 503`;
  - ca `renews` pass.
  - Hai lượt đối chứng đầu không tính, do thao tác của reviewer:
    - PowerShell làm hỏng file khi pipe, gây `SyntaxError`;
    - basetemp nằm trong thư mục cha của bản sao nên runner từ chối như "trong repo".
- Ca `test_runner_renews_expired_idle_session_through_real_login` kèm ca regression, chạy lặp **8 lượt: 8/8 exit 0** (16 test). Không tái hiện failure một lần của agent.

**3. Full inventory `full-r1`.**

- Reviewer tự đếm JUnit trong evidence: **122 file, 5.579 testcase duy nhất, 5.579 pass**, 0 fail/error/skip/trùng.
- `collection.json` 5.579 node khớp số testcase **từng file**; không file nào thiếu XML.
- `artifact_hashes` của inventory: **294/294** khớp file trong evidence.

**4. Bytes image rc2** (`rasm8c-api:rc2` = `c0936a81…`):

- 224 file `app/`+`migrations/`+`alembic.ini`+`pyproject.toml` khớp **working tree host** 224/224.
- So với blob Git `36444ae`: 0 file khác nội dung sau chuẩn hóa line ending. 69 file chỉ khác CRLF, do build trên Windows với `core.autocrlf=true`.

**5. Docker:** trước/sau 24 container (cùng ID), 188 volume, 9 network trùng tập. Chỉ dọn project thử do reviewer tạo.

**Không chạy lại:** full suite, năm drill C4, smoke M8a, diễn tập M8b, web, CLI evaluation `run`. Các số đó là kết quả agent; reviewer chỉ đối chiếu artifact/hash/đếm JUnit.

## P3 — không chặn merge, nên xử lý trước hoặc cùng cutover

1. **Harness test chưa pin image theo deploy.**
   - `scripts/m8a_test_host.py` (CI dùng) và `scripts/c3_test_host.py` vẫn dùng tag `pgvector/pgvector:pg16` và `redis:7-alpine`.
   - Máy này chạy 16.14 nhờ cache; runner CI kéo mới nên chạy **PostgreSQL 16.15**, khác bản deploy đã pin. CI và deploy vì vậy không còn kiểm cùng bytes.
   - Nên đọc digest từ `deploy/compose.yaml` như đã làm cho MinIO.
2. **CI không chạy `test_evaluation.py`.** Lỗi evaluation tồn tại từ M8a đến M8c mà CI không bắt được. Nên thêm file này vào selection CI (cần DB/MinIO, harness đã có).
3. **`auth_safety.cutover()` nuốt ngoại lệ** (`except Exception` → `auth_safety_postcondition_failed`), nên không chẩn đoán được ca 503 một lần của agent.
   - Gate fail-closed theo từng lần đánh giá, nên an toàn về dữ liệu.
   - Nếu lặp lại trên pilot thì không có nguyên nhân để xử lý. Nên log tên lớp ngoại lệ hoặc mã `RecoveryError` (chỉ log, không response).
   - Đổi code app thì phải build rc3 và chạy lại lớp bị ảnh hưởng; chỉ làm cùng thay đổi khác buộc rc3, hoặc nếu lỗi tái diễn.
4. **Provenance rc2.** Image chứa bytes CRLF của working tree Windows, không phải blob LF. Nội dung bằng nhau, nhưng build lại từ checkout Linux sạch sẽ ra digest khác. Handoff đã yêu cầu cutover dùng đúng digest; nên ghi rõ "nội dung = `36444ae` mod line ending" và build RC sau từ checkout LF.

## Trạng thái sau review

- Có thể merge M8c làm baseline chứng cứ cho cutover.
- P3-1/2/4 chỉ chạm harness/CI/docs, không đổi image rc2, có thể gom một PR nhỏ trong lúc chờ hạ tầng.
- Cutover thật vẫn chờ chủ dự án cấp thông tin theo [danh sách M8b](../m8b-pilot-operations.md#thông-tin-chủ-dự-án-phải-cấp-trước-cutover) cộng mirror image.
- D giữ nguyên: 0 verdict thật, `pricing_verified` false.
