# Sửa PR #59 — final authority khi append lỗi và release proof

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
