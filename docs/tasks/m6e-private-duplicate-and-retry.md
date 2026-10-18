# M6e — upload trùng trong kho riêng và retry an toàn

## Mục tiêu và điểm xuất phát

Triển khai **một PR hoàn chỉnh** để khép phần F07 còn thiếu của gói C trong [kế hoạch MVP](../mvp-delivery-plan.md): nhận diện upload cùng bytes trong kho riêng, kể cả khi bản trước đã indexed; thông báo hữu ích và kiểm chứng retry không tạo tác dụng phụ lặp. Sau gói này chuyển sang provider/chất lượng và vận hành nhóm thử theo gói D/E.

PR #62 đã merge tại **`d9bec94c63fd8f27a8897f4f83ce8f6a063d26dc`**. Đã fetch/xác minh head review `15f1dfa` là ancestor và tree khớp; source **0052**, không suy revision DB dev. [Review lại M6d](../reviews/pr62-m6d-private-item-delete-restore.md) ghi 26 repo + 4 ca độc lập pass; artifact agent 731 pass/4.528 not-run được audit riêng. Không có test mới tại merge. Hai failure lịch sử discovery barrier/orphan clock vẫn chưa rõ nguyên nhân.

Đọc `AGENTS.md`, [yêu cầu sản phẩm](../product-requirements.md), [quyền/dữ liệu](../access-and-data.md), [quyết định](../decisions.md), [handoff M6d](../m6d-private-item-delete-restore.md), cùng hợp đồng worker/extraction/index/upload liên quan trước sửa. Fetch lại, xác minh base/head thực tế và tạo nhánh `feat/m6e-private-duplicate-retry` từ `origin/main`. Giữ mọi docs reviewer/orchestrator modified/untracked, đưa vào PR; không discard/stash đè hoặc tự commit thay đổi không rõ nguồn.

## 1. Hợp đồng MVP do orchestrator chọn theo ủy quyền

- **Nhận diện cùng file:** SHA-256 chính xác trong cùng owner/private scope, từ file đã được pipeline kiểm; metadata/title/DOI/arXiv ID không thay thế checksum. Không tiết lộ hay dùng file của owner khác hoặc common để quyết định duplicate private.
- **Hai loại thao tác:** gửi lại cùng upload intent/job là retry và không tạo thêm item/version/file/job ngoài hợp đồng một lần hiện có. Upload bằng consent/intent mới là thao tác riêng, có thể giữ một item/original mới và kết thúc `duplicate`; không gọi đó là retry tạo item ngoài ý muốn.
- **Không hợp nhất ngầm:** duplicate là kết quả nhận diện/cảnh báo, không đổi owner/quyền, không tự công bố hoặc gắn collection, không chép chunk/vector/citation từ item khác. Bản gốc của lần upload mới vẫn được giữ theo hợp đồng hiện tại. Canonical graph, nhận diện paper khác bytes và reuse dẫn xuất giữa item không thuộc gói này và không là điều kiện để nghiệm thu F07 theo hợp đồng MVP trên.
- **Thông báo hữu ích:** owner thấy rõ file trùng và có thể mở bản đang có quyền trong kho của mình nếu còn hợp lệ. Link này chỉ để điều hướng; mọi route đích vẫn kiểm ACL/file/version/deletion bình thường. Nếu không còn đích hợp lệ, không trả ID/title/URL cũ như một nguồn đang dùng được; UI nói rõ original mới còn giữ, không hứa đã liên kết hoặc đã index. Không thêm recovery/retry endpoint hoặc canonical workflow để giải quyết tình huống này.

Ghi hợp đồng vào docs và API/UI liên quan, không ghi như chủ dự án đã trực tiếp xác nhận từng lựa chọn kỹ thuật. Đây là quyết định thực hiện MVP theo ủy quyền hiện hành.

## 2. Sửa nhận diện và đường đọc

Hiện `worker._validate()` chỉ xét job trước ở `stored/extracting/chunking/awaiting_index`; bỏ sót `indexing/ready`. Cần sửa theo **điều kiện của file/item/job**, không chỉ nối thêm danh sách trạng thái rồi coi xong:

1. Lập bảng eligibility theo state thực tế: bản đang xử lý, awaiting index, indexing, ready/indexed, retryable failure theo resume state; đối chứng failed/cancelled/duplicate/deleted và job/file/epoch sai binding. Bản ready có active generation hợp lệ phải được nhận diện. Bản lỗi hoặc một duplicate cảnh báo khác không tự trở thành chuỗi đích hợp lệ; giữ cảnh báo trong-flight hiện có với giới hạn được nói rõ.
2. Chọn đích ổn định khi có nhiều ứng viên, kiểm cùng owner/private và đúng item/version/file; không tự gọi đích đó là canonical. Không dùng item deleted hoặc tombstone tồn tại, kể cả chưa purge. Một file mới sau khi các bản cùng bytes đều đã deleted phải được xử lý độc lập bình thường với intent/key mới.
3. Hai worker nhận cùng hash phải có kết quả nhất quán, không chọn vòng A→B→A hoặc cả hai bị loại vì coi nhau là đích. Giữ serialization và lease/epoch fence hiện có. Retry cùng job không tạo thêm section/chunk/reference/edge/index generation/provider reservation ngoài hợp đồng hiện hành.
4. Đích điều hướng trả qua API là tùy chọn, owner-only và được kiểm quyền hiện hành ngay lúc dựng DTO. Ưu tiên thay đổi schema nhỏ nhất; chỉ thêm field/binding lưu bền nếu có lý do thực tế. Nếu lưu tham chiếu mới, cập nhật cleanup/replay/FK map, không giữ metadata/text private đã xóa hoặc làm purge bị kẹt. Không thêm quan hệ cross-owner qua checksum.
5. Đích bị xóa, owner/session thay đổi hoặc binding không còn hợp lệ trong lúc xử lý/đọc: không trả nguồn ngoài quyền, không reuse dữ liệu từ đích, không hồi sinh job/index. Định nghĩa kết quả race rõ trong handoff, giữ đường mở item/original độc lập kiểm lại quyền.

**Đối chiếu đồ thị khóa trước khi sửa.** Validation hiện có item → job → advisory owner/hash; deletion có review → item → job và cleanup tombstone → review UUID → item. Không lấy khóa đích sau advisory trong khi worker khác giữ đích và chờ cùng advisory. Nếu cần thêm khóa để bảo đảm race với delete, thiết kế lại checkpoint hẹp với thứ tự được chứng minh; không chờ storage/provider khi giữ DB/khóa. Giữ nguyên bản sửa P2 M6d, không chữa deadlock bằng retry mù hoặc tăng timeout.

## 3. API, UI và outcome

- Dùng boundary runtime, current caller/clock, SQL ACL và C4 operational readiness thật. Không fallback owner hoặc thêm bypass test vào production.
- Giữ consent từng upload, intent một lần, checksum original, lease/cancel, pricing gate và financial accounting. Duplicate phải dừng trước extraction/index/provider mới theo hợp đồng đã chọn; không tạo reservation/cost giả cho lần upload duplicate. Reuse completed call khi retry cùng file tiếp tục theo F09; outcome uncertain không được tự gọi provider lại.
- UI phân biệt bản original mới, cảnh báo trùng và bản hiện có có thể mở; không gắn nhãn indexed cho duplicate metadata-only, không hứa canonical/reuse. Giữ chống double submit và unknown outcome không auto-resend. Nội dung thông báo tiếng Việt.
- Không đổi semantics common publication chống trùng, search/ranking/evidence pack, auth/operator, journal format hoặc routing/pricing để tiện làm task. Migration chỉ khi cần, thêm revision sau head thực tế; không sửa migration đã merge.

## 4. Kiểm chứng theo rủi ro

Tái dùng runner/context hiện có, Conda `ras`, PG/runtime/journal/Redis/MinIO/project/port riêng và fake provider test-only; outbound bị chặn. Không đọc/sửa `.env`, query/migrate/restart stack dev, dừng Dify, gọi arXiv/provider thật hoặc bật pricing thật.

1. **Trước/sau:** tái hiện ít nhất ca upload cùng bytes sau bản đầu đã `ready/indexed` không được cảnh báo ở base; test mới bắt đúng hành vi, sửa rồi đạt. Kiểm cả Markdown và PDF, plus same-owner/different-owner/common controls và bytes khác.
2. **Bảng eligibility:** trạng thái/binding/epoch/active generation, nhiều ứng viên, duplicate của duplicate, các đích đều deleted và file đã purge. API/DTO không lộ ID/title/URL của B/common hoặc item deleted; Admin không có đường đọc private mới.
3. **Race tất định:** hai validation cùng owner/hash; target delete trước và trong validation; repeat task/lease takeover; đọc link đồng thời target delete. Dùng barrier + quan sát khóa hoặc checkpoint; assert kết quả nghiệp vụ và không `40P01`, không chỉ khác mã lỗi. Hồi quy chín ca cleanup/repeat M6d và approve/delete liên quan nếu thay lock/checkpoint.
4. **Retry/idempotence:** PUT lặp cùng intent, hai request đồng thời, broker/task redelivery, extraction retry sau checkpoint đã commit; completed/uncertain embedding call của cùng file. So số item/version/file/job/chunk/reference/edge/generation/reservation/call trước/sau phù hợp từng thao tác; không đếm call giả là charge thật. Không tạo retry UI mới.
5. **Browser Chrome qua Next proxy, image thật:** owner upload file đầu tới indexed bằng fake provider test-only; upload lần mới cùng bytes nhận cảnh báo và mở đúng bản được phép; B độc lập, link đích sau delete bị chặn. Consent không chọn sẵn; double submit/response unknown không tự gửi lần nữa. Không thay API image bằng host API cho phần được gọi là smoke image.
6. **Cleanup/restore liên quan:** nếu có binding/schema mới, kiểm purge không kẹt/không giữ metadata bị cấm, nâng DB có dữ liệu/ORM/provisioning và một restore targeted giữ journal hiện hành không trả lại đích deleted. Nếu không đổi schema/recovery thì dùng hồi quy M6d liên quan, không buộc chạy lại mọi full drill.

Chạy selection API đủ các luồng bị tác động, web tests/typecheck/build và một số countercheck bắt assertion nghiệp vụ; khôi phục source trước seal. **Không bắt full inventory >5.000 test cho thay đổi này** trừ khi phát hiện rủi ro shared schema/gate chưa được selection giải quyết; tổng hồi quy trước phát hành vẫn giữ kế hoạch. Không loại fail mới bằng nhãn baseline, không sửa kỳ vọng chỉ để xanh. Hai failure lịch sử M6d được giữ riêng; nếu gặp lại thì giữ log và điều tra, không quy cho P2 đã sửa hoặc gọi rerun pass là biết nguyên nhân.

## 5. Bàn giao

Commit/push, mở một PR tiếng Việt để review, **không merge**. Giữ author/committer; không thêm AI trailer/promo. Đưa docs có sẵn vào PR, cập nhật trạng thái PR #62 đã merge và hợp đồng F07/MVP; giữ report cũ theo source lịch sử.

Tạo một handoff và inventory ngắn: base/code/image/schema, bảng eligibility, lock map, API/UX, test theo ID và hash, fail/rerun/not-run, fake/live, cleanup và giới hạn. Báo rõ phần nào F07 đã khép theo hợp đồng exact-byte MVP và phần later còn lại; không coi canonical graph/reuse khác item là chức năng đã làm. Không nhân bản mọi lịch sử test vào đầu mỗi docs.

Chỉ khảo sát Git/source và kiểm tài liệu trong lượt giao này; **chưa triển khai hoặc chạy test M6e**. Không ghi bất kỳ lệnh chưa chạy thành chứng cứ. Sau review/merge gói này, ưu tiên gói D provider/charge/chất lượng và E vận hành nhóm thử, không mở thêm các nhánh chức năng nâng cao.
