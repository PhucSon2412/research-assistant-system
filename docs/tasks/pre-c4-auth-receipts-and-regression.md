# Prompt agent — B1: initial receipt và hồi quy trước C4

## Mục tiêu và điểm xuất phát

Bạn triển khai **một PR hoàn chỉnh của gói B**: đóng đường giả mạo receipt khởi tạo và xử lý nợ hồi quy từ đợt chuyển auth/runtime, để C4 có thể bắt đầu tích hợp bản chạy đầy đủ. Chủ dự án đã ủy quyền orchestrator chọn phạm vi/cách làm. Đây là task coding, không dừng ở khảo sát hoặc trả lại một kế hoạch.

PR #59 đã **MERGED** tại `041f77ef1c12b6277e7a5f46aaf32155b3673063`; head đã review `035cef02e38d87bd5e44e0bf5e887ed0ed574e09` là ancestor và tree khớp merge. Source migration **0049**, không suy revision DB dev. Review C3 giữ kết quả theo source đã chạy; xác minh merge không phải một lượt rerun. **Safety vẫn pending tới C4**; task này không mở ready, pricing hoặc traffic.

Fetch lại, xác minh base/head thực tế, tạo nhánh từ `origin/main`, gợi ý `fix/pre-c4-auth-receipts-regression`. Giữ mọi docs orchestrator/reviewer modified/untracked, gồm review PR #59 và prompt này; đưa vào PR, có thể commit riêng. Không reset/clean/stash làm mất nội dung. Không chia thêm theo endpoint; chỉ báo cần tách nếu có phụ thuộc thực tế khiến PR không thể review được.

## Đọc trước khi sửa

- [AGENTS.md](../../AGENTS.md), [mục lục](../README.md), [kế hoạch MVP và checklist](../mvp-delivery-plan.md).
- [Thiết kế auth restore, đặc biệt quy ước kiểm C1–C4 và T01–T23](../auth-restore-safety.md).
- [C3 handoff: debt, operator, outcome và C4](../c3-operator-auth-recovery.md), [inventory C3](../c3-test-inventory.json), [review độc lập PR #59](../reviews/pr59-c3-operator-auth-recovery.md).
- [Bootstrap/initial receipt](../c2c1-bootstrap-receipts.md), [invitation runtime](../c2c2-invitation-runtime.md), [reset/status](../c2b-reset-status.md), [quyết định](../decisions.md).
- Handoff/fixture liên quan tới từng debt được xử lý; dùng danh sách PR #50/#55 và C3 làm điểm xuất phát, không cộng số lịch sử thành số lỗi hiện tại.

Đọc SQL/routine/trigger hiện hành, provisioning role và mọi consumer initial status; đừng chỉ đọc Python hoặc tên reason. Nếu docs/code khác, tái hiện hành vi và ghi rõ thay đổi hợp đồng.

## 1. Đóng lỗ hổng receipt khởi tạo

### Vấn đề đã biết

C3 đã chặn reason `verified_recovery`/`verified_initial_status` qua đường generic. Nhưng runtime vẫn có thể dùng `ras_auth_change` đúc status receipt `bootstrap`/`invitation_accept` cho User đã có. Trigger cặp hiện kiểm khi tạo `bootstrap_state` hoặc accept invitation, chưa bảo đảm chiều ngược từ mỗi receipt. Fixture `test_c2b_reset_status.add_other(initial_reason=...)` đang dùng đường này. Một reason đúng tên không đủ chứng minh initial status đã được cấp hợp lệ.

### Đầu ra bắt buộc

1. **DB thi hành provenance của cả hai domain credential/status** khi reason là `bootstrap` hoặc `invitation_accept`. Receipt phải thuộc đúng luồng tạo account tương ứng, đúng User, installation, operation/generation, decision/time, cặp initial receipt và binding transaction. Không cho gắn reason khởi tạo vào một thay đổi tùy ý của User cũ hoặc dùng lại bằng chứng của lần tạo trước.
2. Chặn ở SQL/routine/trigger thực với login runtime thật, kể cả gọi generic trực tiếp. Reason, custom GUC, owner name do caller khai hoặc một helper Python không thay bằng chứng. Giữ ranh giới runtime/definer/migrator/recovery/operator và fixed `search_path`; không cấp thêm quyền ghi auth thô để fixture chạy.
3. Kiểm **mọi consumer chấp nhận initial status evidence**, gồm enable/predicate và operator inspect/resolve nếu có: không tiếp tục coi receipt giả đã tồn tại là bằng chứng chỉ vì reason nằm trong allowlist. Giữ enable hợp lệ dựa trên lịch sử initial receipt có provenance thật; disable → enable không tự xác minh một account held.
4. Bootstrap một lần trên `new_install` và invitation accept hợp lệ vẫn commit được User + hai initial receipt + journal + singleton/closure/audit theo hợp đồng cũ. Không thêm đường tạo Admin thứ hai, bootstrap rescue, operator-enable hoặc tự dựng account khi replay.
5. Dùng migration mới sau head thực tế (dự kiến `0050` nếu head vẫn `0049`); **không sửa migration đã merge**. Chọn constraint/routine phù hợp thứ tự ghi hiện hành, có thể kiểm hoãn tới commit nếu cần. Không dùng `xmin` lịch sử làm bằng chứng bền qua dump/restore; phân biệt kiểm cùng transaction mới với provenance lưu lâu dài.
6. Xử lý dữ liệu có sẵn rõ ràng: không sửa/xóa journal/receipt append-only hoặc tự tạo bằng chứng cho hàng cũ. Initial receipt không chứng minh được provenance phải không cấp quyền; ghi cách kiểm và cách đưa về held/quarantine hoặc recovery hợp lệ. Không biến legacy thành usable/proven bằng backfill. Bản cũ có provenance hợp lệ phải tiếp tục được nhận sau upgrade và restore.
7. Giữ outcome C3: append/fsync lỗi vẫn kiểm authority/hạn case bằng clock mới trước commit; release lỗi/Ctrl-C không đổi known commit thành “nothing was changed”. Journal replay chỉ hạn chế, không dựng verifier, User, receipt hoặc active.

Trước sửa, tái hiện lỗ hổng bằng test SQL/runtime thật và ít nhất một consumer thực (ví dụ enable sau receipt giả). Sau sửa, kiểm cả từ chối giả mạo và luồng hợp lệ; không chỉ kiểm mã lỗi.

## 2. Xử lý nợ hồi quy và fixture

Lập **một bảng debt theo node ID** từ inventory hiện hành và lịch sử, có nguyên nhân, nguồn, thay đổi, kết quả cuối. Phân biệt `failed`, `error`, `skipped`, `not-run`, `deferred-to-C4`; không gọi hàng nghìn ca chưa chạy là hàng nghìn lỗi.

Các nhóm đã biết cần đóng hoặc bàn giao C4 bằng lý do cụ thể:

- C3 từng loại 281 node: 273 journal/recovery legacy, 5 upgrade từ `0033`–`0037`, một DB connection và các node PR #55 giao nhau. Đây là số tại source/lượt cũ, phải nối theo identity để không đếm trùng.
- Fixture journal/recovery cũ dừng vì C1 pending; fixture dùng generic initial reason; helper bootstrap/approve/publish/revoke/index/cleanup đã đổi hợp đồng runtime/operator.
- Migration/snapshot cũ bị code recovery hiện hành đọc cột chưa có. Giữ `hold` tương thích trước migrate; kiểm upgrade có dữ liệu, không dùng schema head để giả một snapshot cũ hoặc chạy current business recovery trước bước migrate bắt buộc.
- Special-password/DB role connection; các debt PR #50/#55 về quyền SQL setup, readiness pending và helper revoke. Quyền tạo dữ liệu hỏng cho negative test thuộc fixture privileged cô lập, không phải runtime production.
- `test_c2c2_restore.py` gắn cứng port/label lịch sử: chuyển sang context/project cô lập nếu cần chạy, không kết nối nhầm service có sẵn.
- CLI/drill backup/restore còn gọi lối owner đã superseded. Sửa theo đúng đường runtime hoặc operator đã provision cho từng hành động, không fallback owner khi runtime guard lỗi.

Giữ kỳ vọng nghiệp vụ: quyền A/B/Admin, original checksum, nguồn/citation, thu hồi, batch journal, commit uncertain và không retry. Fixture account được phép phải đi qua luồng tạo/khôi phục có bằng chứng hợp lệ. Fixture legacy/corruption được tạo riêng, ghi rõ mục đích, và không được vô tình trở thành account đã xác minh.

Không đổi hàng loạt kỳ vọng `200` thành `503`, thêm `skip/xfail` hoặc bỏ test chỉ để xanh. Theo hợp đồng C1–C3, test nghiệp vụ tầng trực tiếp/API fixture có thể thay **duy nhất kết quả cutover pending tại test seam đã có**; runtime role, auth, journal integrity, hold, receipt, ACL và outcome phải thật. Test pending HTTP riêng không patch, vẫn phải đóng. Những ca chỉ chứng minh được bằng C4 không patch phải giữ expectation và liệt kê chính xác, kèm coverage tầng dưới; không bỏ cả file vì một fixture hỏng.

Sửa lỗi sản phẩm thực sự lộ ra trong đợt tích hợp, nếu thuộc hợp đồng auth/runtime/restore đang hoàn thiện. Lỗi pass ở base nhưng fail ở head là regression phải xử lý. Không miễn lỗi quyền/nghiệp vụ vì “đã fail ở base”. Tránh refactor hoặc thêm tính năng ngoài checklist MVP. Nếu một lỗi đòi quyết định sản phẩm mới, tiến hành phần độc lập và nêu đúng phụ thuộc; đừng tự nới yêu cầu.

## 3. Kiểm chứng cuối và inventory đầy đủ

Tái sử dụng runner/fixture có sẵn; không xây thêm hệ thống seal chỉ để tạo báo cáo. Dùng PostgreSQL/journal/role thật và MinIO/Redis khi phép thử cần, trên tài nguyên cô lập.

### Nhóm trọng yếu

- SQL generic/raw runtime: reason khởi tạo ở User cũ; sai User/domain/operation/generation/installation/transaction; thiếu hoặc chỉ có một receipt; thiếu parent; parent cũ; NULL/missing binding; receipt đúng tên nhưng không đủ provenance. Không có receipt/quyền mới sau ca từ chối.
- Bootstrap/invitation accept hợp lệ, concurrent accept/bootstrap, journal prefix/fsync/rollback/uncertain, release lỗi; các SQL guard mới không phá thứ tự User/token/session/resource → journal hoặc giữ DB qua hash/TTY/storage/provider.
- Enable dùng initial evidence hợp lệ sau disable; held không được enable nhờ receipt giả, password reset không tự enable; C3 credential/status riêng, latest disabled không được mở; CAS rehash giữ generation.
- Upgrade DB `0049` có dữ liệu hợp lệ và dữ liệu không chứng minh được; ORM/schema/grants/downgrade; các snapshot lịch sử trong test có sẵn. Không migration I/O journal.
- Hai vòng dump/restore/recover thực trên context cô lập, journal giữ ngoài dump: initial receipt hợp lệ vẫn được đối chiếu; receipt mất không dựng lại; legacy/bad provenance không cấp quyền; case operator đã dùng không dùng lại. Hồi quy common/review/session/reset/invitation và delete fence.

### Hồi quy tổng

1. Collect **toàn API inventory** ổn định tại source cuối. Chạy mọi node có thể thực thi đúng hợp đồng trước C4, chia batch nếu cần. Không dùng danh sách deselect lịch sử làm mặc định vĩnh viễn.
2. Nối JUnit với collection bằng node identity: mỗi node có một trạng thái cuối, không thiếu/trùng. Lượt fail rồi rerun pass giữ cả hai; lượt cuối tính một lần. Danh sách C4-deferred/skip/not-run có từng ID và lý do; chỉ được gọi “full suite pass” nếu thực sự chạy toàn bộ và đạt.
3. Giữ source cố định trong lượt cuối, hash trước/sau; không chạy mutant hoặc sửa file khi suite đang chạy. Đối chứng một số guard vừa sửa bằng assertion nghiệp vụ, khôi phục trước suite cuối. Không chạy lại hàng loạt mutant lịch sử khi không có rủi ro mới.
4. Windows: truyền selection/deselect qua file `@args` hoặc cách tương đương để tránh `WinError 206`. Theo dõi exit code/process cùng log; tiến trình đã chết thì báo lỗi, không chờ vô hạn một file kết quả không thể xuất hiện. Dùng timeout cho barrier/lock/subprocess phù hợp, không timeout mù cho toàn suite dài.
5. Web tests/typecheck/build nếu có thay đổi web hoặc cần xác nhận tương thích auth/proxy; browser smoke chỉ cho phần bị ảnh hưởng. HTTP nghiệp vụ hoàn chỉnh không patch, readiness ready và toàn hệ Compose thuộc C4, chưa tính đạt trong task này.

**Điều kiện bàn giao:** lỗ hổng initial receipt đóng ở cả writer và consumer; luồng hợp lệ còn chạy; các debt đã biết có kết quả xử lý cụ thể. Không còn lỗi auth/quyền/journal đã biết bị miễn bằng nhãn baseline. Nếu còn failure hoặc node không chạy được, báo là chưa hoàn tất phần tương ứng và chỉ rõ blocker C4; không tự tuyên bố gói B đã xong.

## 4. Môi trường và giới hạn

- Chỉ dùng Conda `C:\Users\ADMIN\.conda\envs\ras`; kiểm interpreter/pip trước chạy. Không tạo venv.
- **Không đọc/sửa `.env`, key hoặc dữ liệu thật; không query/migrate/restart stack dev.** Secrets test sinh riêng ngoài repo; không fallback `.env`. Chặn HTTP ngoài các service test loopback/container được khai báo; không gọi arXiv/OpenRouter thật, không đổi `pricing_verified`.
- Chụp baseline tài nguyên Docker, chọn project/port riêng, kiểm label/đường dẫn trước xóa. Chỉ dọn tài nguyên do task tạo. Không dùng prune hoặc đổi project `ras`. Ghi drift quan sát được, không suy đoán nguyên nhân hoặc sửa restart dev trong task này.
- Không mở safety/version ready, thêm bypass pending/hold, đổi routing/model/budget, triển khai private deletion, streaming hay tính năng mới. Không coi provider giả/all-503 là nghiệm thu MVP.
- Cùng mất/rollback DB và journal có thể không phát hiện; fsync/fault injection không chứng minh độ bền mất điện. Production storage, operator người thật và triển khai vẫn cần bằng chứng riêng.

## 5. Bàn giao và PR

- Một handoff/inventory tập trung, gợi ý `docs/pre-c4-auth-receipts-regression.md` và `docs/pre-c4-test-inventory.json`: migration/routine/predicate/provenance, debt đã đóng/còn lại theo node, source/JUnit/hash, lỗi chuẩn bị, lệnh thực chạy, cleanup và giới hạn.
- Cập nhật ngắn kế hoạch MVP, decisions, auth restore, mục lục, README và AGENTS theo trạng thái mới. Giữ report cũ đúng source; không nhân bản toàn lịch sử kiểm ở đầu mọi file hoặc ghi lệnh dự kiến như đã chạy.
- Bàn giao **một gói C4 tích hợp**: image/revision/roles, writer/consumer, cutover predicate còn pending, fixture seam được dùng và các ca phải chạy không patch; trình tự new install/upgrade/hold/migrate/recover/operator, hai vòng restore và browser. Tách yêu cầu môi trường thử khỏi lựa chọn storage/ủy quyền production. Chưa triển khai cutover dev.
- Commit/push và mở **PR tiếng Việt**, không merge. Không thêm AI vào tác giả/committer/trailer hoặc dòng quảng bá PR. Báo base/code/head, thay đổi, số pass/fail/error/skip/not-run/deferred, phần chưa hoàn thành và đường dẫn chứng cứ.

Đích của task là đưa dự án tới C4 với guard đúng và hồi quy đã đối chiếu, không kéo dài M6 bằng các PR vi mô mới.
