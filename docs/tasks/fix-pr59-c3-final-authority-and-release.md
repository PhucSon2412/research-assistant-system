# Sửa PR #59 — final authority khi append lỗi và release proof

**Đã giải quyết theo review lại tại `035cef0`, 2026-10-12:** cả hai P2 và nhánh Ctrl-C release đã qua kiểm độc lập; không còn điểm chặn merge trong scope C3. [Report hiện hành](../reviews/pr59-c3-operator-auth-recovery.md#review-lại-tại-035cef0--không-còn-điểm-chặn-merge-trong-scope-c3): 106 repo + 4 ca reviewer pass. PR vẫn OPEN/chưa merge, source `0049`, safety pending tới C4. Các prompt dưới đây giữ làm lịch sử và hợp đồng hồi quy, không là task sửa còn mở. Debt/gói B/C4 tiếp sau merge, chưa được chứng nhận bằng kết quả này.

## Lượt review lại tại `6465dc0` — interruption trong release

**Task tại `6465dc0` (đã sửa và kiểm lại): sửa nhánh còn lại của P2-2 trên cùng PR #59.** P2-1 và release lỗi `Exception` đã qua kiểm độc lập; không làm lại các cơ chế đó hoặc mở C4. [Report review lại](../reviews/pr59-c3-operator-auth-recovery.md#review-lại-tại-6465dc0--còn-nhánh-p2-2-khi-ctrl-c-trong-release) ghi 95 repo pass, 11 ca reviewer pass và một ca interruption fail.

### Lỗi đã tái hiện

`_release` chỉ bắt `Exception`. Khi `Connection.rollback` trong release mutation sau known commit ném `KeyboardInterrupt`, cleanup bị ngắt và `mutate` không đổi nó thành outcome. CLI bắt như hủy TTY, exit 2 `nothing was changed`, dù receipt mới và credential mới đã commit/usable. Reviewer gọi CLI in-process với prompt tổng hợp; fault injection, không là SIGINT/ConPTY thật. Test nằm ngoài repo tại `%TEMP%\ras-pr59-rereview-20261012\test_interrupt_release.py`; chạy hai lượt đều fail đúng assertion, không setup error.

### Yêu cầu sửa

1. Cleanup của từng pha phải xử lý interruption/BaseException phù hợp, tiếp tục trả/invalidate connection và không để lỗi cleanup che outcome của thân pha. Không chỉ đổi thông báo CLI hoặc nuốt Ctrl-C rồi báo thành công.
2. Dùng `Attempt` độc lập để phân loại interruption trong mutation release: known commit giữ credential/status/case/receipt và trả non-success outcome; batch chưa xác nhận giữ exit 5; commit đã gửi chưa rõ là uncertain, không đổi thành known-not; đã append nhưng chưa commit không báo nothing-written. Không retry/proof/success sau release lỗi.
3. Prepare release bị ngắt dừng trước TTY/hash/mutation; proof sau known commit bị ngắt trả postcommit failure. Ctrl-C/EOF **trong nhập mật khẩu trước mutation** vẫn giữ hành vi hủy an toàn, không thay dữ liệu.
4. Thêm regression ít nhất cho interruption lúc rollback/close release sau known commit (kiểm receipt/verifier hoặc status thực, output/exit và không retry). Kiểm thêm nhánh release của prepare/proof và outcome unconfirmed/uncertain theo cách fault có thể xảy ra; không dùng test chỉ so exception type. Giữ ca Ctrl-C TTY hiện có và hai P2 đã sửa.
5. Chạy regression trên head review trước sửa để xác nhận lỗi, rồi bản sửa; chạy nhóm C3 core/CLI/migration/ORM và hồi quy outcome liên quan. Một đối chứng bỏ cơ chế mới phải bị test bắt; khôi phục source/hash trước lượt cuối. Không cần chạy lại full inventory chỉ để tăng số pass, nhưng ghi selection/not-run chính xác.
6. Cập nhật handoff/inventory và cửa sổ lỗi; giữ report này cùng chứng cứ agent/reviewer riêng. Commit/push cùng PR #59 tiếng Việt, không merge/AI attribution. Giữ mọi docs reviewer modified vào PR, safety pending C4, không migration/grant mới nếu không có phụ thuộc thật. Không đọc `.env`, tác động dev hay provider.

Phần dưới là prompt lượt đầu, giữ làm lịch sử và yêu cầu hồi quy cho các cơ chế đã sửa.

## Prompt lượt đầu tại `8fa504f` (lịch sử)

Review tại head `8fa504f49504d4a1c3e36830f02cfca4c730c33c`, source **0049**, PR OPEN/chưa merge. [Report](../reviews/pr59-c3-operator-auth-recovery.md) ghi tái hiện và kiểm độc lập. Sửa trên **cùng PR #59**, không bắt đầu C4, không mở safety/pricing. Giữ mọi docs review/task modified/untracked và đưa vào commit/PR.

## P2-1 — Không được bỏ kiểm authority/expiry khi ghi journal lỗi

Trong `app/operator_recovery.py::mutate`, `_recheck` sau append chỉ chạy trong `if attempt.written`. Nếu file đã ghi đủ nhưng `fsync` ném `OSError`, nhánh `written=false` vẫn đi tới COMMIT. Khi case đã hết hạn hoặc operator bị revoke trong lúc I/O đó, receipt credential/status vẫn commit. `recover` sau đó xác nhận batch, giữ credential usable/status proven vì receipt đã có; quyết định không hợp lệ ở final boundary không còn chỉ là intent hạn chế.

- Kiểm login/authority và clock/case bằng thời gian mới **sau mọi kết quả append, trước bất kỳ COMMIT nào**, kể cả absent/torn/prefix/full-fsync-error. Không chỉ chuyển cờ confirmed hoặc đổi thông báo lỗi.
- Nếu quyền/hạn mất sau khi đã bắt đầu journal thì rollback mutation/receipt; trả lỗi outcome phù hợp, giữ file để recovery chỉ hạn chế. Không báo nothing-written khi file có thể đã đổi; không tự retry hoặc nối lại quyết định cũ.
- Giữ nhánh đã chốt: append lỗi nhưng authority/case vẫn hợp lệ có thể commit cả batch false để gate đóng tới recover. Không làm mất mirror false, hợp đồng cửa sổ lỗi hay biến `journal_confirmed` thành bằng chứng commit.
- Regression cho credential và initial status: case hết hạn ngay trong `os.fsync` rồi hàm ném lỗi; thêm operator revoke trong cùng nhánh. Kiểm ngay DB **không có receipt mới**, rồi recover: vẫn quarantine/held, không dựng verifier/receipt/active. Full line/prefix đã bền tiêu case theo journal; case mới/head mới là quyết định khác. Giữ các ca fsync lỗi với case/authority còn hiệu lực và ca append thành công nhưng case hết hạn.

## P2-2 — Release lỗi không được biến proof thành thành công

`connection()` bắt lỗi rollback/close, invalidate rồi close nhưng không truyền trạng thái lỗi ra ngoài khi cleanup cuối thành công. Reviewer làm rollback của transaction prepare hoặc proof sau commit ném lỗi một lần; cả hai vẫn trả `OperatorResult` thành công. Connection không leak ở các ca này; lỗi là workflow tiếp tục/báo thành công dù release không sạch.

- Luôn thử rollback và close phù hợp, invalidate khi cần và thu hồi connection; **đồng thời báo lỗi release bằng mã an toàn**. Không nuốt lỗi vì invalidate sau đó thành công.
- Prepare/snapshot release lỗi: dừng trước TTY/hash/mutation/journal; không trả Prepared làm bằng chứng sạch.
- Mutation release lỗi: phân loại theo `Attempt` độc lập — known commit vẫn giữ dữ liệu/case/receipt, uncertain không bị rollback/close đổi thành known-not; không gọi proof rồi báo success sau lỗi release.
- Proof sau known commit release lỗi: trả non-success/outcome rõ, không in credential recovered/initial status decided, không tự submit lại. Inspect/recover dùng để đối chiếu; không đảo ngược known commit.
- Regression rollback/close/invalidate tại prepare, mutation trước/sau COMMIT và proof; kiểm output/exit, không gọi pha tiếp, không retry, connection trả hết. Các lỗi nghiệp vụ đã biết giữ mã phù hợp; lỗi cleanup không được che thông tin outcome quan trọng hoặc làm lộ exception có payload.

## Kiểm và bàn giao

Dùng Conda ras và PostgreSQL/MinIO/journal/roles cô lập; không đọc/sửa `.env`, thao tác dev, gọi provider/arXiv, mở pricing hoặc safety. Không cần sửa migration/grants cho hai lỗi Python này nếu không phát hiện phụ thuộc thật. Giữ `0049` và các guard case/receipt/hold hiện có.

1. Chạy regression mới trên code review trước sửa để thấy lỗi nghiệp vụ, rồi trên bản sửa; nếu dùng fault injection ghi rõ không là lỗi thiết bị/mất điện thật.
2. Chạy C3 core/CLI/migration/ORM, fault/clock/authority và hồi quy auth/journal phù hợp thay đổi. CLI TTY và pending API/proxy ở head hiện hành phải được giữ; không coi gate 503 là thay thế nghiệp vụ.
3. Countercheck hai bản sửa ngoài suite chính, khôi phục bytes/hash trước lượt cuối; không sửa code khi suite chạy.
4. Giữ lịch sử kết quả agent và reviewer riêng; cập nhật handoff/inventory, source/selection/JUnit/not-run, cửa sổ lỗi và runbook. Ghi riêng debt generic `bootstrap`/`invitation_accept`, không tuyên bố mọi đường recovery đã được đóng bằng C3. Debt đó và toàn inventory thuộc gói B/C4, không tự miễn nghiệm thu.
5. Commit/push cùng PR #59, cập nhật mô tả PR tiếng Việt, **không merge**, không trailer/ghi nhận AI. Báo cleanup có kiểm label/đường dẫn và drift có chứng cứ. Giữ safety pending tới C4.

Evidence reviewer nằm ngoài repo tại `%TEMP%\ras-pr59-review-20261012`; test tái hiện không sửa app/repo tests. Không chép secret/credential/evidence raw vào repo.
