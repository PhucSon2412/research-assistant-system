# Review PR #62 — M6d private deletion và restore

Ngày **2026-10-17**, PR **OPEN/chưa merge**, base `fb96354c4f8dc8fb8f4f8a54f1516d9d2e13625e`, head `7494f8c01e7af52e3a4f7db07384ee16d44ee999`, source **0052**. **Có một P2 cần sửa trước merge:** đảo thứ tự khóa giữa cleanup và xóa lặp gây PostgreSQL deadlock. [Prompt sửa cùng PR](../tasks/fix-pr62-private-cleanup-repeat-delete-lock-order.md).

[Task gốc](../tasks/m6d-private-item-deletion-and-restore.md) · [Handoff agent](../m6d-private-item-delete-restore.md) · [Inventory agent](../m6d-test-inventory.json)

## P2 — cleanup và xóa lặp khóa review/item theo thứ tự ngược

Vị trí: [private_delete.py](../../api/app/api/private_delete.py), `accept()` dòng 112–115; [private_cleanup.py](../../api/app/private_cleanup.py), `finalize()` dòng 178–189 và `scrub()` cập nhật `review_requests`.

- `accept()` khóa **mọi** review request của item rồi mới khóa item. Nó chỉ phát hiện `item.deleted_at` và đi vào nhánh idempotent **sau khi đã lấy hai nhóm khóa này**. Các request đã withdrawn/approved vẫn bị khóa ở lần POST lặp.
- Cleanup finalize khóa tombstone, rồi **item**, sau đó scrub bằng `UPDATE review_requests`. Lệnh UPDATE phải lấy khóa row review request.
- Khi item đã bị xóa và có review history, một repeat POST có thể giữ review row chờ item của cleanup, trong khi cleanup giữ item chờ review row của repeat POST. Đây là cycle thật; gate, User, epoch và receipt đều có thể hợp lệ.

**Tái hiện độc lập, giữ nguyên timeout/source/guard:**

1. Tạo item private cùng một open review request qua fixture có runtime và C4 readiness thật; owner xóa thành công, review chuyển withdrawn.
2. Chạy cleanup, dừng bằng event ngay trước `scrub()`; tại đó finalize đã giữ item UPDATE.
3. Chạy yêu cầu xóa lặp; observer chỉ đọc PostgreSQL xác nhận request đang chờ `transactionid` sau khi đã giữ review request.
4. Thả cleanup tiếp tục. PostgreSQL phát **SQLSTATE `40P01`, `DeadlockDetected`**. Trong cả hai lượt đã quan sát, cleanup hoàn tất `{'purged': 1}`, còn xóa lặp bị hủy.
5. Lượt boundary trực tiếp trả `ApiError(503, 'deletion_unavailable')`. Lượt xác nhận lại gọi **HTTP POST qua FastAPI TestClient**, trả **503 `{"error":"deletion_unavailable"}`** thay vì receipt idempotent 200. Ca control không có review request đạt.

Không thay auth/cutover/clock/DB lock timeout; event chỉ điều khiển thứ tự thread, `handle_error` chỉ ghi loại lỗi/SQLSTATE. Không phải lỗi setup, assertion về lịch thread tùy ý hoặc một timeout chờ barrier. PostgreSQL thực sự chọn một transaction làm deadlock victim. Tùy victim, cycle này còn có thể làm cleanup thất bại sau khi đã đi qua pha object I/O; bảo đảm cần sửa là thứ tự khóa, không phải đổi tên lỗi hoặc thử lại vô điều kiện.

**Cần sửa:** dùng thứ tự tương thích cho deletion/cleanup/review approval; hoặc tách nhánh receipt cho deletion đã commit để nó không giữ review lock khi chờ cleanup, với proof và TOCTOU được chứng minh. Giữ khóa review cần thiết cho lần xóa đầu và race approve/delete. Không đổi toàn bộ acceptance sang item → review vì approval hiện đi review → item. Thêm regression tất định với review history, control không review và hai thứ tự chạy; không coi retry sau deadlock là giải pháp cho cycle.

## Kiểm độc lập đã chạy

Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe`, project riêng `rasc3ed71a2ea`; PostgreSQL/role/journal thật. MinIO/Redis riêng được dựng; các fixture M6d dùng storage fake như khai báo trong test, nên không gọi kết quả này là MinIO/image/browser drill mới. Business M6d dùng `operational_world`, không patch cutover; clock được điều khiển như fixture hiện hành.

| Nhóm | Kết quả |
| --- | --- |
| Ba file M6d, contract C4 và ORM/schema | **111/111 pass**, 646,81 s; 3 warning hiện có |
| Hai ca ngoài repo: cleanup + repeat deletion, có/không review | **1 pass / 1 failure**, 17,82 s; ca review tái hiện `40P01` |
| Xác nhận lại ca review bằng HTTP POST thật qua FastAPI | **1 failure**, 10,79 s; cùng `40P01` và HTTP 503 |

Tổng **111 repo + 1 control ngoài repo pass**, hai lượt tái hiện fail được giữ riêng; không cộng chúng vào pass inventory agent. Không failure/error/skip trong nhóm repo. Reviewer không sửa production/test/migration/grants.

Lượt repo đầu bị reviewer dừng khi chưa thu được kết quả node, trong lúc PostgreSQL dùng disk báo chờ I/O. Không tính pass và không suy rằng chưa có assertion nào chạy. Lượt cuối dùng PostgreSQL data tmpfs đúng cách runner M6d của repo dùng; không đổi fsync policy, clock, timeout hoặc guard. Native PowerShell chỉ flush dấu chấm pytest thành dòng khiến log chưa có tiến độ không chứng minh process đã dừng. Một thao tác chuyển log ngay sau stop gặp file-handle chưa nhả; không ảnh hưởng lượt có JUnit cuối. Không đụng Dify để giảm tải.

## Đối chiếu artifact agent

- **1.084 artifact hash** hiện hữu/khớp; **441 source hash** trong inventory khớp head. Reviewer theo dõi rộng hơn **442 tracked hashes** gồm `.env.example`; trước/sau trùng, không đọc `.env` thật.
- Từng trạng thái cuối trong **105 nhóm JUnit** khớp **5.250 ID pass**, đủ **5.163 ID C4 + 87 mới**; không thiếu/trùng trong tập cuối. Đây là đối chiếu artifact, không phải reviewer tự chạy thêm 5.250 ca.
- Số 5.250 là hợp nhất các lượt bị ngắt/continuation/rerun, không là một invocation toàn xanh. Hai failure cuối về discovery barrier/orphan clock cùng những failure chuẩn bị cũ còn giữ riêng. Rerun cả file pass không xác lập nguyên nhân; reviewer không quy chúng cho môi trường hoặc coi đã giải thích xong.
- Web 72, typecheck/build, default HTTP 7, image/Chrome-delete 19, restore 70, shared-key MinIO 9 và bảy countercheck là chứng cứ agent có hash riêng, **reviewer không chạy lại**. Chưa dùng chúng để miễn P2 mới. F07 vẫn có gap đã công bố; không yêu cầu canonical graph trong sửa hẹp này.

## Cleanup và bàn giao

Evidence ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr62-review-20261017`, gồm `repo.xml/log`, hai phiên bản test lock ngoài repo, `lock-order.xml/log`, `http-lock-order.xml/log`, JSON SQLSTATE/outcome, artifact audit, source/baseline/cleanup.

Đã kiểm ownership/mount/consumer và gỡ đúng **3 container thử**, tài nguyên Compose và một anonymous volume của DB thử trước khi chuyển sang tmpfs; xóa ba file credentials/config. Scan bảy secret thử trên Git/log/JUnit: 0 hit. **24 container có sẵn giữ nguyên ID/name/image/mount/label**, không còn container của task. Worker/beat dev đã restart từ baseline: beat 220→309 và từ restarting sang running, worker 220→310 vẫn restarting, StartedAt đổi; chỉ ghi quan sát, không sửa hoặc suy nguyên nhân. Dify được giữ nguyên. Không đọc `.env`, query/migrate/restart dev, gọi provider/arXiv hoặc bật pricing.

Giữ report/prompt và cập nhật trạng thái **chưa commit**, đưa vào cùng PR #62 sửa P2. Không merge hoặc đăng comment GitHub trong lượt review này. C4 operational readiness cô lập tiếp tục giữ thật; chưa cutover dev/production và chưa xác minh provider/charge thật.
