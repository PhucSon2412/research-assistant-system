# Review PR #40: C2d-2b review/publication JSON reads

**Cập nhật sau review (2026-10-05):** chủ dự án đã merge PR #40; GitHub CLI xác nhận MERGED, merge SHA `833db7cc1b3a4df6b683f72820fca83a17c19eb7` chứa head `11da731`. Báo cáo và các cập nhật docs review vẫn chưa commit trong workspace lúc giao [C2d-3](../tasks/m6c2b2c2d3-library-collection-json-writes.md); giữ để đưa vào PR tiếp theo, không coi chúng đã nằm trong commit merge. Kết quả dưới đây giữ nguyên source/thời điểm review; không có lượt kiểm code mới sau merge. Safety pending tới C4.

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2d2b-review-publication-json-reads.md) · [Bàn giao](../c2d2b-review-publication-json-reads.md) · [Inventory agent](../c2d2b-test-inventory.json)

## Kết luận

Ngày 2026-10-05, fetch/xác minh source `11da7316a6be27317c2d6d27c8725aafa51533b2` khớp nhánh remote `feat/m6c2b2c2d2b-review-publication-json-reads`; base `f229141e6db46f7df663cb1d14335530a4d5e9dd` (merge PR #39). Code là `749a31d`; commit head chỉ docs/bàn giao. GitHub CLI xác nhận PR #40 **OPEN**, chưa merge; working tree sạch trước review. Không migration, source giữ `0039`, không truy vấn revision DB dev.

**Không phát hiện điểm chặn merge trong phạm vi 9 GET C2d-2b; có thể merge PR #40.** Reviewer không sửa code ứng dụng, merge, push hoặc đăng comment PR. Docs review lượt này chưa commit; coding agent giữ và đưa vào PR tiếp theo (xem cập nhật sau review ở trên). Safety vẫn **pending tới C4**, kết luận không phủ toàn C2d/T14, writer/file/provider hoặc production cutover.

## Đối chiếu source và hợp đồng

- Đúng 9 wrapper GET vào `review_publication_json`; 6 GET Admin yêu cầu current role. Exact session/User, latest predicate/head/receipt/integrity thật, final clock sau SQL/lock wait; proof cùng transaction, không touch/commit hoặc User/session row lock mới.
- SQL owner/history/pending/review-copy clauses trước count/order/page ở candidate và hai lượt đọc lại. Candidate DTO không gửi client. Expire/no-autoflush, scope/locked IDs/DTO/profile phải khớp; đổi scope bỏ transaction/proof và retry một lần, sau đó 409. Không khóa toàn count scope.
- Review request SHARE UUID → private item SHARE qua exact grant → shared journal. Admin closed/inactive chỉ khóa request, không private item; không dùng ordinary visible gate thay quyền review. Cùng chiều withdraw/reject/approve; không thêm item → request.
- Common read khóa item qua pending/history gate rồi journal. Cleanup giữ tombstone → item; reader không khóa tombstone. Các aux metadata/job/publication/retention/flags được đọc lại; duplicate hint/count ngoài trang vẫn là quan sát, không phải snapshot hoặc quyền publish.
- Grant private mất trước boundary không trả title/version/file/email/declaration/original URL cũ. Closed detail vẫn có thin DTO 200 theo contract, approved có pending ID như trước; recovery closure không dựng approval/common copy. Owner vẫn chỉ đọc request của mình.
- Admin common-copy history giữ pending/listed/revoked, metadata sau cleanup theo retention; không lấy lại trường scrub từ private source. Download/fulltext/provider/RAG là gate riêng. Notes listed/revoked đã sửa đúng thực tế, formatter chung POST chỉ nhận câu chữ mới.
- AST mọi writer/storage/shared formatter giữ nguyên so base; migration/deps/identity/promotion/publication/common-index và web không đổi. 13 GET C2d-2a giữ boundary cũ; inventory chỉ chuyển đúng 9 GET mới, không đánh dấu writer/original stream hoàn tất.

## Kiểm độc lập thực chạy

| Kiểm | Kết quả/điều kiện |
| --- | --- |
| Suite chọn trên `11da731` / code `749a31d` | **261/261 pass**, 0 failure/error/skip, **551,837 s**, 3 warning hiện có |
| Thành phần | Toàn bộ **260 C2d-2b**, gồm 9 HTTP pending thật; **1 ORM/schema** |
| JUnit/node selection | Đúng 261 node unique đã chọn, không thiếu/trùng; không failure/error/skipped |
| Hash source API | **215 Python/SQL file** trước/sau không đổi |
| Inventory tĩnh | **63/63 route**, 13 boundary C2d-2a giữ nguyên, 9 boundary C2d-2b và 6 current Admin |
| Docs trước cập nhật review | **54 Markdown, 810 liên kết nội bộ**, 0 lỗi/control characters; diff toàn PR sạch |

Nhóm kiểm gồm success/empty/thin history, caller sau dependency, idle/absolute TTL, current Admin cuối, pending gate thật, A/B cùng hash và SQL owner/count/page, invalid grant/tuple, closure/approval/recovery races, common publish/revoke/gates, cleanup/retention, thứ tự reader/writer và row wait, dirty read/session, scope churn và malformed/missing ID. Nghiệp vụ chỉ scoped cutover ở fixture; integrity, runtime LOGIN/receipt/journal/SQL vẫn thật. Resource rows/corruptions và writer helpers dùng đặc quyền setup riêng; originals tổng hợp trong bộ nhớ, không phải kiểm writer HTTP/MinIO/stream.

PostgreSQL standalone `ras-pr40-review-db-20261005`, image `pgvector/pgvector:pg16`, label `ras.review=pr40-20261005`/`ras.task=c2d2b-review-20261005`, bind `127.0.0.1:58448`. Conda ras interpreter/pip đã xác minh, không cài thêm/venv. Admin URL/DB parts explicit trước fixture import, runtime/recovery LOGIN thật; HTTPTransport sync/async ra ngoài bị chặn. Không đọc `.env`/key hoặc đổi operational pricing/safety.

JUnit SHA-256: `04e9d3d08b65f4faae9f091abeda314c2547a7d06308ee3d734180853481e35c`. Runner/selection/source manifests/XML/summary ngoài repo tại `%TEMP%/ras-pr40-review-20261005`. Ba warning: Starlette TestClient/httpx deprecation và hai computed-default autogenerate của User credential/status domain, như lịch sử.

Reviewer không chạy lại 473 node khác trong suite 734 của agent, 9 đối chứng source hoặc 9 pool probe riêng; không cộng các số đó với 261 thành full suite. Theo inventory 2213 node của agent, 1952 node ngoài selection review chưa chạy trong lượt này. Web tests/typecheck/build/browser, whole mixed writer files, proxy/stream/storage/provider/worker/backup/restore/drill và C3/C4 not-run. Không có lượt suite review fail/error/skip hoặc bị dừng/sửa source.

## Tooling và cleanup

Lượt chuẩn bị kiểm tĩnh đầu không chạy được do helper validator không còn trong tool store; đọc lại script validator ngoài repo và chạy thành công. Đây là lỗi orchestration trước kiểm tĩnh, không phải lỗi ứng dụng hoặc một lượt API suite thất bại.

Đã kiểm container ID/name/labels, đúng một mount PostgreSQL và volume chỉ thuộc container thử trước dọn; xóa đúng container cùng anonymous volume riêng. Credentials JSON và postgres env tổng hợp đã xóa sau kiểm path tuyệt đối nằm trong thư mục review; evidence/journal tổng hợp giữ ngoài repo. Metadata name/ID/image/StartedAt của **9 container dev** trước/sau giữ nguyên; không query/nâng/restart DB dev.

Response đã qua boundary có thể tiếp tục gửi sau nhả khóa; bytes đã gửi không thu hồi được. Count/duplicate hint không là snapshot toàn hệ thống, SQL đặc quyền ngoài protocol và độ bền mất điện chưa được chứng minh. Writer/file/provider, tổng hồi quy C2d và C3/C4 còn riêng.
