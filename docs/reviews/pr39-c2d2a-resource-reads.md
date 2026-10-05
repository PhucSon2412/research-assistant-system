# Review PR #39: C2d-2a resource JSON reads

[Mục lục](../README.md) · [Task](../tasks/m6c2b2c2d2a-resource-json-reads.md) · [Bàn giao](../c2d2a-resource-json-reads.md) · [Inventory agent](../c2d2a-test-inventory.json)

**Cập nhật sau review (2026-10-05): PR #39 đã merge `f229141`; docs review được commit `e89815d`.** Review/source/test bên dưới là lịch sử của lượt kiểm độc lập, không có lần chạy lại trong lượt giao [C2d-2b](../tasks/m6c2b2c2d2b-review-publication-json-reads.md). Safety pending tới C4.

## Kết luận

Ngày 2026-10-05, fetch và review source `5d291c05ce63ac2e35f96b510a811ee44a7017d2`, base `origin/main=d06664e53383071237c21e802f439fda348a1afd` (merge PR #38). Local HEAD khớp nhánh remote; working tree sạch trước review. Không migration, head source `0039`, không truy vấn revision DB dev.

**Không phát hiện điểm chặn merge trong phạm vi 13 GET của C2d-2a; có thể merge PR #39.** Reviewer không merge, push hoặc đăng comment PR. Chỉ sửa/cập nhật docs trong lượt review; các docs khi đó chưa commit được coding agent giữ, commit `e89815d` và đưa vào merge PR #39. Safety pending tới C4; kết luận không phủ toàn C2d/T14 hoặc cutover production.

Có một sửa nhỏ về tài liệu: câu “Malformed/missing IDs vẫn 404” trong bàn giao quá rộng. UUID sai ở `/collections/{collection_id}/items` vẫn trả `422 invalid_request` (validation trường path); UUID hợp lệ không có/ngoài quyền trả 404. Library item/discovery citing-item tự parse string nên UUID sai trả 404. Hành vi code không đổi trong PR; reviewer đã sửa câu mô tả, không yêu cầu đổi API.

## Phạm vi đối chiếu

- `resource_json` chỉ ở 13 GET; writer/shared formatter không bị gắn guard. Entry resolve/touch/commit/check/rollback giữ nguyên. Read session phải sạch, `no_autoflush` và expire identity map trước mỗi observation; không business mutation, provider/storage I/O hoặc commit trong read path.
- Candidate đã lọc SQL được dùng thu thập khóa, không gửi client. Item UUID trang SHARE → owned collection UUID SHARE → citation shared nếu cần → journal shared/final caller; không lấy User/session hay khóa tài nguyên mới sau journal.
- Sau khóa, đọc lại DTO/count/page/flags bằng current Principal; hai observation bằng nhau và tập UUID đúng tập dự định khóa. Final proof/clock sau resource SQL. Churn nhả toàn transaction và thử lại một lần, rồi lỗi `409 resource_scope_changed` có thông báo web.
- Count/limit/offset giữ SQL ACL trước pagination. Common metadata/fulltext/provider/download vẫn gate riêng đúng item/version/file/generation/epoch; private extraction trước index giữ owner scope. Admin không được mở private theo API thường.
- Parent collection bảo vệ membership theo writer hiện hành; citation advisory chặn discovery commit trang/tombstone/link. Reader/writer common revoke và membership, journal/auth writer, clock tiến trong lock wait đều có test tất định và kiểm DB wait thật. Không coi READ COMMITTED là snapshot toàn scope.
- Viewer/search/RAG/file/upload/review/publication writers/worker không chuyển trong PR. Caller inventory đối chiếu AST source khớp **63/63 route**, đúng **13 GET** ghi boundary mới; không dùng trạng thái group để đánh dấu writer đã xong.

## Kiểm độc lập

| Lượt | Kết quả và điều kiện |
| --- | --- |
| Suite GET/ORM chọn trên source `5d291c0` | **313/313 pass**, 0 failure/error/skip, **692,695 s**, 3 warning hiện có |
| Thành phần | C2d-2a 301 (gồm 13 pending HTTP với gate thật), library GET 9, collection GET 1, discovery GET 1, ORM/schema 1 |
| Contract ID ngoài repo | **1 pass**, cùng runtime/receipt/entry dependency thật; library/discovery malformed 404, collection malformed 422, collection UUID vắng 404 |
| Web `npm test --prefix web` | **44/44 pass**, 0 fail/cancelled/skip/todo; **17.214,164 ms**, warning MODULE_TYPELESS_PACKAGE_JSON hiện có |
| Nguồn/selection | 213 API Python/SQL file hash giữ nguyên; JUnit khớp chính xác 313 node unique đã chọn, không thiếu/trùng |
| Docs trước cập nhật review | 51 Markdown, 763 liên kết nội bộ, 0 lỗi/control characters; diff toàn PR sạch |

PostgreSQL standalone `ras-pr39-review-db-20261005`, `pgvector/pgvector:pg16`, label `ras.review=pr39-20261005`/`ras.task=c2d2a-review-20261005`, bind `127.0.0.1:58446`. Conda ras interpreter/pip đã xác minh, không cài dependency/venv. Admin URL và DB parts được đặt explicit trước import fixture; runtime/recovery LOGIN, NOLOGIN definer/migrator thật. Runner chặn HTTPTransport sync/async ra ngoài, không đọc `.env` hoặc dùng key, không thay operational pricing/safety. Chỉ fixture cutover scoped ở nghiệp vụ; pending có nhóm thật riêng.

JUnit SHA-256 suite 313: `03f84421f599444a4b8bf5393d5c8fe86a5e00b3bbcaab3dea01b230c2f3d54e`. Evidence runner/selected-nodes/source manifest/XML/summary và journal tổng hợp ngoài repo tại `%TEMP%/ras-pr39-review-20261005`. Không cộng với 538 hoặc 6 đối chứng của agent thành full suite. Reviewer không chạy lại 1640 node còn lại trong inventory 1953, citation matcher toàn nhóm, C2d-1/C2a toàn file, đối chứng source, web typecheck/build/browser, proxy/storage/provider/backup/restore/Celery drill hoặc C4 cutover.

## Lỗi của tooling review, không là lỗi ứng dụng

- Lượt đọc JSON inventory đầu thiếu `encoding="utf-8"` trên Windows, lỗi decode; sửa script ngoài repo rồi đọc được, không chạy app.
- Ca ID bổ sung đầu giả định FastAPI trả key `detail`, nên **1 fail** ở expectation. App có handler an toàn dùng `error=invalid_request` và `fields`; giữ code/status, sửa riêng test ngoài repo rồi **1 pass**. XML lượt fail giữ riêng `id-contract-initial-fail.xml`, không tính là pass hoặc lỗi mới của ứng dụng.
- Suite chính 313 không bị dừng/sửa source và không có lượt fail/error/skip.

## Cleanup và giới hạn

Đã kiểm name/labels/image/port/mount trước `docker rm -f -v` đúng container thử; anonymous volume riêng đã mất, credentials JSON và postgres env tổng hợp đã xóa. Metadata name/ID/image/StartedAt của **9 container dev** trước/sau giữ nguyên. Không query/nâng/restart DB dev, đọc/sửa `.env`, gọi provider, sửa API hoặc đổi `pricing_verified`.

Khóa/proof bảo vệ response tại boundary; response đã qua boundary có thể tiếp tục gửi sau khi nhả khóa. Không hứa thu hồi bytes hồi tố, snapshot count bất biến khi có insert mới, bảo vệ SQL đặc quyền ngoài protocol hoặc độ bền mất điện. C2d-2b, writer/file/provider, tổng hồi quy và C3–C4 còn riêng.
