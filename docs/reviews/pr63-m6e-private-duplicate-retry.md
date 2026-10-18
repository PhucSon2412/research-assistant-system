# Review PR #63 — duplicate private và retry

Ngày **2026-10-18**. PR **OPEN/chưa merge**, base **`d9bec94c63fd8f27a8897f4f83ce8f6a063d26dc`**, head **`c1f7bccddad4adc8c9edd0989710b8cfbc41618d`**, code **`5290c0b`**, source **0052**. **Không phát hiện điểm chặn merge mới trong phạm vi M6e đã kiểm.**

[Task](../tasks/m6e-private-duplicate-and-retry.md) · [Handoff agent](../m6e-private-duplicate-retry.md) · [Inventory agent](../m6e-test-inventory.json)

## Phạm vi và nhận xét code

- `app/ingest/duplicates.py` dùng chung predicate cho quyết định validate và điều hướng DTO: cùng SHA-256, cùng owner/private/origin upload, đúng job/file/version/item và epoch, không cancel/deleted/private-deletion/tombstone. Ready đòi active generation đúng job/file/version/item/epoch và active version. Chọn thứ tự `(created_at, id)`; duplicate không là đích. Không thêm schema, grant hoặc binding lưu bền.
- Worker giữ thứ tự khóa cũ: owner User → item → job → version/file → advisory owner/hash, không lấy khóa đích sau advisory. Checkpoint validation và private delete cùng owner đều khóa User trước resource, nên deletion được tuần tự hóa mà không tạo thêm cạnh khóa đích/advisory. Bản sửa cleanup tombstone → review UUID → item của PR #62 không đổi.
- `GET /library/items/{id}` chỉ tính `existing_copy` cho job duplicate trên private của caller. Đích được thêm vào tập khóa SHARE của `resource_json`, khóa theo UUID với SQL ACL rồi dựng lại DTO. Nếu candidate thay đổi trước khóa thì boundary bỏ proof cũ, rollback và thử lại hữu hạn. Link không cấp quyền và không mang binding sang request khác.
- Web dùng UUID API để dựng đường dẫn nội bộ, React render title dưới dạng text. Nội dung phân biệt item/original mới với bản đã index, không hứa hợp nhất/reuse; không đổi consent, double-submit hoặc unknown-outcome handling.

Không sửa production/test/migration/grants trong lượt review. Kiểm dựa trên hợp đồng exact-byte MVP đã giao; không coi đây là canonical dedup hoặc reuse dẫn xuất giữa item.

## Reviewer thực chạy

Conda `ras`, project riêng **`rasc3c218c92a`**, PostgreSQL/runtime roles/journal và MinIO/Redis riêng; DB data tmpfs như runner repo, không đổi fsync policy. Fake provider chỉ trên settings/transport test, HTTP ngoài bị chặn; operational C4 thật. Fixture M6e dùng MinIO; các regression P2 M6d có storage fake như khai báo trong file đó.

| Kiểm | Kết quả |
| --- | --- |
| Toàn bộ file M6e (39), chín regression khóa P2 M6d, C4 hold/readiness và ORM/schema | **50/50 pass**, 0 failure/error/skip, exit 0; 903,66 s; ba warning hiện có |
| Ca ngoài repo: đích đầu bị xóa giữa candidate DTO và khóa, còn một đích hợp lệ khác | **1/1 pass**, exit 0; 32,28 s; một warning |
| Web `npm test` | **75/75 pass**, 0 fail/cancel/skip, exit 0 |
| Web typecheck | exit 0 |

Ca độc lập dùng fixture legacy có hai bản cùng owner/bytes (tắt predicate chỉ lúc tạo fixture thứ hai, khôi phục trước hành vi được kiểm). Một DELETE runtime thật commit sau observation đầu và trước resource locks. Response cuối chọn đúng bản thứ hai, không chứa ID đích đã deleted; URL đích cũ trả 404. Original của duplicate vẫn tải đúng bytes; job duplicate không có embedding call. Không patch gate/caller/clock/SQL quyền hoặc sửa timeout để đạt kết quả.

Reviewer chạy **51 API test**, không cộng 1.462 ca của agent thành số tự chạy. Không có failure trong các lượt thực thi reviewer. Một helper test ngoài repo được hoàn thiện phần đăng ký conftest trước khi chạy, không có lượt pytest lỗi setup bị bỏ. Chỉ có kiểm source image bổ sung bên dưới, chưa chạy lại Chrome hoặc full image business flow.

## Đối chiếu artifact và image

- **131 artifact hash** hiện hữu và khớp; **448 source hash** agent trước/sau trùng và khớp head, chín file thay đổi chính cũng khớp. Reviewer giữ **449 tracked source hashes**, gồm `.env.example`, trước/sau mọi kiểm và cleanup trùng; không đọc `.env` thật.
- **1.462 ID pass** khớp từng trạng thái trong **22 nhóm JUnit**; **3.836 not-run**, collection **5.298** không thiếu/trùng, hai tập không giao nhau. Đây là audit artifact, không phải reviewer chạy lại selection của agent.
- Năm countercheck lượt hai có XML failure nghiệp vụ và không error, source được khôi phục. Lượt đầu yếu 3/5 và các lượt preparation/fail/rerun vẫn giữ riêng; không biến mutant tương đương thành bằng chứng guard. Không chạy thêm mutant ở lượt reviewer.
- Image API giữ lại `sha256:5add3da933e7dd3e54f5fb89682dfb27c09833629352c9e2006e48b21e1c0952` và web `sha256:3fe74bb4d030a95f89a9c1d588671f6c7d4effebd5bfa00bf537e34e56cd26b5` khớp inventory. Reviewer chạy một container API `--network none`, entrypoint chỉ băm file, không chạy app/provider: **209 tracked file app/migration/config khớp host**. Tập này gồm thêm config nên không thay số 208 của tập agent.
- 19 checks Chrome của agent đều pass trong artifact có hash, outside requests 0. Reviewer không gọi đó là browser smoke mới. Ba lượt default-flow được giữ đúng lịch sử: lỗi thiếu fake-off, deadline sau recreate, rồi 4/4; không kết luận root cause của lỗi khác bằng việc rerun đạt.

## Giới hạn và bước tiếp

Không rerun full >5.000 API, web build, Chrome, Celery/image business flow hoặc full backup/restore drill. Predicate mới không lưu binding/schema/journal mới; selection reviewer giữ C4/M6d liên quan. Chưa có provider thật, charge, routing/retention, chất lượng semantic hoặc performance mới. Hai failure lịch sử M6d discovery barrier/orphan clock không nằm trong selection và vẫn chưa rõ nguyên nhân.

F07 được đáp ứng trong phạm vi exact-byte MVP đã chọn: nhận diện/cảnh báo có điều hướng và retry an toàn. Upload với intent mới vẫn tạo item/original riêng theo hợp đồng. Duplicate không tự được xử lý lại khi đích về sau lỗi/xóa; owner phải xóa duplicate rồi upload bằng intent mới. Canonical graph, paper khác bytes và reuse khác item vẫn là later. Sau merge chuyển gói D/E trong kế hoạch MVP, không mở thêm nhánh chức năng nâng cao hoặc gọi kết quả này là production readiness.

## Dọn dẹp và chứng cứ

Evidence reviewer ngoài repo: `C:\Users\ADMIN\AppData\Local\Temp\ras-pr63-review-20261018`: JUnit/log repo và ca độc lập, web/typecheck, audit artifact/image, source hashes và cleanup. Đã kiểm label/mount/consumer, gỡ đúng **3 container thử**, network và một anonymous DB volume cũ trước khi chuyển tmpfs, xóa ba file credentials/config. Container chỉ băm image đã tự gỡ bằng `--rm`; hai image agent giữ để review không bị xóa. Scan bảy secret thử trên Git/log/JUnit: **0 hit**.

**24 container có sẵn giữ nguyên ID/name/image/mount/label**, không có container lạ mới. Worker/beat dev đã restarting ở baseline và còn restarting: beat RestartCount 145→161, worker 146→162, StartedAt đổi. Ghi quan sát, không sửa hoặc suy nguyên nhân; Dify không bị thao tác. Không query/migrate/restart dev, đọc/sửa `.env`, gọi arXiv/provider hoặc đổi pricing.

Report và cập nhật trạng thái reviewer để **chưa commit**, giữ cho PR kế tiếp. Không merge PR hoặc đăng comment GitHub trong lượt review này.
