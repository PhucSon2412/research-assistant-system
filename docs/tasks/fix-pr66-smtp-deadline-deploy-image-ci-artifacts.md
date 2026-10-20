# Sửa PR #66 — deadline SMTP, image cài mới và gate artifact CI

Sửa **trên cùng PR #66**, head review `c0b20aa`, source **0052**. Đọc [report reviewer](../reviews/pr66-m8a-pilot-entry-hardening.md), [task M8a](m8a-pilot-entry-hardening.md) và [handoff](../m8a-pilot-entry-hardening.md). Giữ mọi docs reviewer/orchestrator chưa commit vào PR; không discard lịch sử lỗi hoặc đổi nhãn số pass.

Đây là ba P2 đã có repro, không giao milestone mới hoặc chuyển M8b. Triển khai sửa ngay, không chỉ giải thích giới hạn hay hỏi lại quyền coding. Không thay auth/journal/ACL/role/outcome/pricing để né tích hợp.

## 1. Deadline thật cho toàn SMTP send

`settimeout(remaining)` trước `send_message` không giới hạn các lần đọc bên trong smtplib; lời gọi có thể thành công muộn. Reviewer SMTP/TLS loopback deadline 0,6 s vẫn success ở 2,016/2,047 s khi DATA reply nhỏ giọt; nhiều lệnh trả chậm cộng dồn success 0,718 s. DNS tiêm chậm 2 s làm timeout chỉ trả sau 2,031 s.

- Một deadline bao toàn bộ DNS/connect/greeting/TLS/EHLO/AUTH/MAIL/RCPT/DATA và đóng/thu hồi tài nguyên. Response sau hạn không được tính thành công. Giữ TLS verify/hostname/CA và không có fallback plaintext.
- Không chỉ thêm kiểm clock sau một lời gọi đã block hoặc cancel coroutine còn thread gửi tiếp. Cần cơ chế kết thúc I/O/tiến trình thật có giới hạn; nhánh DNS treo không giữ worker hoặc interpreter vô hạn. Reuse bài học M7a nếu chọn subprocess; token/password/message không đưa vào argv/log hoặc file không được bảo vệ, chỉ truyền tối thiểu qua kênh riêng và thu hồi child đúng PID mình sở hữu.
- Hết hạn có thể DATA đã được server nhận: giữ `delivery_failed`/compensation invitation, reset generic, không tự retry hoặc sinh token mới. Không thay SMTP unknown outcome thành “mail chưa được nhận”. Không giữ DB lock/connection khi gửi.
- Đưa regression phù hợp từ `C:/Users/ADMIN/AppData/Local/Temp/ras-pr66-review-20261020/test_smtp_deadline.py` và `test_smtp_dns.py` vào repo. Giữ control nhanh, SMTPS/STARTTLS, reply nhỏ giọt, lệnh nội bộ cộng dồn, DNS chậm/treo, cleanup và không gửi tiếp sau timeout. Các fault là tổng hợp, ghi đúng giới hạn, không cần gửi email thật.

## 2. Đường cài mới có image tải được và health phù hợp

Orchestrator chọn baseline pilot **`cgr.dev/chainguard/minio@sha256:709099319add48dfb44aa596a7070d3ddd8dd04d7ae881765719aec29df9e510`** đang dùng CI và reviewer đã tra manifest thành công. Không cần chủ dự án chọn lại registry. Xác minh version/provenance/license và tương thích trước đưa vào baseline; không mua account/đổi nhà cung cấp storage/dịch chuyển dev hoặc dữ liệu thật.

- Đồng bộ baseline deploy/dev/test harness hoặc dùng một cấu hình image tường minh có default kiểm được; không để deploy cứng tag quay.io hỏng và chỉ CI dùng override tốt. Image/healthcheck phải chạy thật trên image tối giản, không giả định có `mc`/shell/curl. Có thể probe từ service phù hợp với readiness thật, không coi container running là S3 ready.
- Không cần purge cache toàn máy để giả máy sạch. Kiểm pull từ registry bằng digest và cài mới project/volume riêng, không fallback image cũ. Ghi rõ phần nào build cache, phần nào fetch độc lập; có thể giữ cache app nếu không ảnh hưởng kết luận dependency storage.
- Kiểm new install, upload/readback/checksum, list/delete/cleanup và một vòng restart với dữ liệu thử; upgrade có dữ liệu tổng hợp qua cấu hình mới nếu đổi binary/version/UID/storage layout. Không dùng volume dev để kiểm tương thích.
- Cập nhật compose/deploy checker/CI/runbook hợp lý. Giữ pin digest; ghi nguồn/giới hạn availability và cập nhật image, không gọi image public là production support đã được mua hoặc đã chứng minh durability.
- Nếu candidate không tương thích thì đưa repro và chọn bản build có provenance từ source phát hành phù hợp trong cùng PR; không tiếp tục quảng bá new-install bằng cache hỏng. Chỉ cần giải quyết phụ thuộc storage này, không mở redesign S3/backup.

## 3. Không publish artifact khi scan không đạt

`Upload scanned JUnit` hiện `if: always()` vẫn chạy khi scan exit 1; file bị phát hiện còn nguyên. Thiếu XML scanner cũng trả exit 0. Reviewer chưa upload dữ liệu hay phát hiện run thật đã rò secret; không dùng các điều đó để bỏ đường lỗi này.

- Upload phải phụ thuộc bằng chứng scan **thành công**, ràng buộc với đúng artifact sẽ publish. Có thể đưa output sạch riêng và chỉ publish output đó. Scan lỗi/mất context/file thiếu/unreadable không tạo dấu ready hoặc upload raw.
- Vẫn cho phép upload **JUnit sạch khi pytest fail** để debug; không dùng điều kiện chỉ toàn job success rồi làm mất evidence lỗi. Cleanup chạy mọi nhánh; failure test/scan vẫn khiến job thất bại.
- Thiếu file phải báo rõ và không được coi là clean. Không log giá trị secret hoặc upload scanner/context/credentials để debug. Không thử phát tán secret thật lên GitHub.
- Regression từ `test_ci_guard.py` trong evidence reviewer: clean control, fixture secret, thiếu file, lỗi scanner/context, pytest fail nhưng artifact sạch, và publish bị chặn khi scan fail. Kiểm workflow condition/output, không chỉ test scanner exit.
- Chạy CI với source sửa; ghi remote run/commit/conclusion chính xác. Có thể dùng fixture kiểm điều kiện publish local thay vì cố tình upload một secret để chứng minh guard.

## 4. Kiểm và bàn giao

- Reviewer đã chạy **136 API + 79 web/typecheck đạt**; 6 regression độc lập fail/3 control pass. Không gọi số test xanh là hai P2 khác đã sửa. Reuse selection theo code bị đổi và các repro; chạy SMTP/limiter/contract auth liên quan, web nếu đổi; storage/deploy smoke theo mục 2.
- Không bắt buộc rerun full hơn 5.000 hoặc mọi restore drill khi không đổi schema/journal. Giữ JUnit, source hashes, exit, failure/rerun/not-run và kiểm nguồn trong image nếu rebuild. Source giữ nguyên trong suite cuối; không mất failure lịch sử.
- Dùng Conda `ras`, project/port/volume/journal/credential thử riêng, cấu hình tường minh không fallback `.env`. Chỉ dừng child PID/Compose project do task sở hữu; **không `taskkill` theo tên python.exe**, không stop hệ thống theo tên chung. Đặt pytest basetemp trong context thử để cleanup credential fixture có ownership rõ.
- Không đọc key/`.env`, gọi provider/arXiv/mail thật, thay pricing hoặc chạm dev/Dify. Không điền phiếu human hoặc cấp lại trần live cũ. Giữ C4 readiness/gate thật.
- Commit/push cùng PR #66, mô tả tiếng Việt, giữ author/committer, không AI trailer/promo, không merge. Đưa report/prompt reviewer vào commit riêng nếu thuận tiện; cập nhật handoff/inventory/AGENTS/kế hoạch về bản sửa, không sửa lịch sử thành pass mới.

Cuối lượt bàn giao code SHA/head, từng P2 và bằng chứng trước/sau, tests/not-run, fresh-pull/deploy và remote CI, các giới hạn còn thật cùng cleanup. Chưa domain/host/SMTP/operator/journal độc lập thật thì vẫn chưa cutover; M7b chờ phiếu, D/E chưa được nghiệm thu toàn bộ bằng bản sửa này.
