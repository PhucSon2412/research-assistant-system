# M13 — Trang hỏi đáp toàn thư viện và tìm kiếm vector cho kho lớn

**Giao 2026-10-29, sau merge PR #72 (`origin/main = 959e4a4`).** Một PR. Đọc [chính sách kiểm thử giai đoạn dùng cá nhân](../decisions.md#chính-sách-kiểm-thử-giai-đoạn-dùng-cá-nhân-2026-10-29) và [handoff M11](../m11-demo-knowledge-base.md) trước khi làm.

## Bối cảnh

- Kho cá nhân trên demo `ras-pilot` có 3.167 item, **1.643 indexed** và 8 collection.
- RAG đã có ở API (`POST /rag/ask` với `item_id` | `collection_id` | `scope: "all"`, `mode` là `keyword` hoặc `hybrid`), nhưng web chỉ hiện nó trong trang chi tiết từng paper. Trang "Tìm kiếm & hỏi đáp" (`web/app/search/page.tsx`) chỉ có tìm kiếm. Chủ dự án muốn tận dụng kho lớn bằng hỏi đáp toàn thư viện.
- Vector search ở `api/app/search.py` (`_rank`) quét tuần tự:
  - `embedding_chunks.vector` khai báo `Vector()`, không cố định số chiều;
  - không có index ANN;
  - câu lệnh `ORDER BY vector <=> …, chunk_id` có tie-break nên planner không dùng được index nếu thêm vào.

  `_rank` đã đo `keyword_ms`/`vector_ms`.

## Phần A — Trang "Hỏi đáp" (web)

- Route mới (ví dụ `/ask`), có mục trong navigation chính. Giữ trang search và panel RAG trong trang paper.
- Ô câu hỏi lớn, chọn phạm vi:
  - **toàn thư viện** (mặc định);
  - **một collection** (lấy danh sách từ API collection hiện có);
  - **một paper** (chọn qua tìm theo tiêu đề bằng API hiện có, hoặc chuyển sang trang paper).
- Chọn chế độ keyword/hybrid. Nếu dùng hybrid làm mặc định, giải thích trong handoff.
- Kết quả:
  - câu trả lời có trích dẫn đánh số;
  - panel nguồn hiện tiêu đề paper, trang/vị trí, đoạn trích, link tới trang paper;
  - link file gốc **chỉ** lấy từ `original_url` của API.
- Khi hệ thống từ chối/thiếu bằng chứng, nói rõ đó là thiếu nguồn, không giả là lỗi.
- Xử lý đủ các trạng thái: đang chạy, hết budget/quota, provider tắt hoặc chưa `pricing_verified`, lỗi mạng, item bị thu hồi giữa chừng, phiên hết hạn.
- **Lịch sử hỏi trong phiên trình duyệt:** chỉ phía client (`sessionStorage`), có nút xóa. Không thêm bảng hay endpoint lưu câu hỏi trên server; muốn lưu server thì ghi thành đề xuất.
- Theo skill/ràng buộc frontend M10 (`~/.codex/skills/ras-frontend` nếu có): token/CSS Module, font tiếng Việt, responsive 1440/390, không tài nguyên mạng ngoài.
- Không đổi route/proxy/quyền của API cho phần A. Nếu thiếu dữ liệu cho UI, báo trước và giữ thay đổi API tối thiểu, có test.

## Phần B — Đo và tăng tốc vector search

1. **Đo trước** trên demo, qua API thật bằng tài khoản user demo (secret file như M11, không in mật khẩu):
   - số chunk/embedding đang active;
   - p50/p95 của `vector_ms`, `keyword_ms` và tổng thời gian cho `POST /search` hybrid và `/rag/ask` scope all;
   - dùng 20–30 câu hỏi cố định trong manifest M11.

   Đọc DB demo chỉ để đếm/EXPLAIN (read-only). Không ghi.
2. **Nếu vector stage chậm** (đề xuất: p95 > 500 ms, hoặc chiếm phần lớn thời gian search), thêm **migration `0053`**:
   - index HNSW cho model/số chiều đang active. Cột không cố định chiều, nên dùng expression index với cast số chiều + partial theo `model_id`/`dimensions`;
   - operator class khớp `<=>` (cosine);
   - truy vấn trong `_rank` dùng **đúng** biểu thức đó, bỏ tie-break khỏi phần dùng index (sắp xếp phụ sau khi lấy top-k);
   - xử lý lọc ACL đi cùng ANN: kiểm phiên bản pgvector trong image đã pin. Nếu ≥ 0.8, dùng `hnsw.iterative_scan` (SET LOCAL); nếu không, over-fetch có giới hạn. **ACL vẫn nằm trong cùng SQL**: chunk trái quyền không bao giờ rời DB, không vào rerank/payload;
   - `ef_search`/`m`/`ef_construction` để trong config có test, không hardcode rải rác.
3. **Chất lượng:** recall@`vector_top_k` của ANN so với exact search trên cùng bộ câu hỏi (đề xuất ≥ 0,95), cùng latency sau. Nếu ANN không đạt, giữ exact search và ghi số liệu: đó vẫn là kết quả hợp lệ.
4. Test:
   - migration upgrade/downgrade;
   - ORM khớp schema (`test_orm_models_match_migrated_schema`);
   - truy vấn dùng index (EXPLAIN trên DB test có đủ dữ liệu giả);
   - ACL: user khác, item bị xóa/thu hồi, bản chung không đủ `permits_*` **không** xuất hiện khi đi đường ANN.

## Phần C — Kiểm live chất lượng hỏi đáp toàn thư viện

- **Ngân sách task: $10, dừng gọi mới ở $8 (80 %), ledger fail-closed** (theo chủ dự án 2026-10-29: tăng tiền cho kiểm live).
  - Gọi qua demo bằng tài khoản user demo; budget ứng dụng $10/ngày và quota hiện có giữ nguyên, nếu chạm thì chia ngày.
  - Agent không đọc file key hay `.env`.
- Bộ 40–60 câu trên kho M11, ghi thành dataset trong repo (câu hỏi + kỳ vọng, không chép toàn văn paper):
  - câu một paper;
  - câu chủ đề trải nhiều paper;
  - so sánh hai paper nêu tên;
  - câu không có đáp án trong kho.
- Chấm bằng track **AI judge** có sẵn (`app/evaluation/ai_review.py`, nhãn `ai_judge`, không bao giờ ghi `human_*`), cộng kiểm máy: citation mở được và khớp đoạn trích.
- Báo theo nhóm câu: trả lời/từ chối đúng/sai, citation hợp lệ, latency, chi phí thật mỗi câu. Đây là **baseline** cho M14 (cải thiện so sánh nhiều paper); không sửa logic RAG trong M13 trừ lỗi rõ ràng chặn trang mới.

## Phần D — Cập nhật demo

1. Backup trước khi đổi bằng `ras_ops backup` theo handoff M8b/M11; ghi thời gian và ID.
2. Build image mới: web `ras-pilot-web:ui3`, và nếu có migration thì API `ras-pilot-api:rc3`.
   - Mở rộng `scripts/m9_lan_demo.py` một lệnh đổi image API kiểu `web-image`: lưu state, có rollback;
   - giữ chế độ local `127.0.0.1:8443`.
3. Chạy migration qua đường migrate của deploy. Ghi thời gian build index trên dữ liệu thật.
4. Chỉ recreate service cần thiết. Không `down -v`, không xóa volume/thư mục, không chạy compose thiếu `--env-file`/`-p`/`-f`.
5. Đo lại latency sau khi đổi; kiểm `ras_ops check` và HTTPS local 200.
6. Rollback có hướng dẫn và đã thử ít nhất trên stack cô lập: về `ui2`/`rc2` (downgrade `0053` nếu cần) hoặc restore backup.

## Kiểm và bàn giao

- Web: test + typecheck + build. Ảnh chụp trang mới ở 1440 và 390, light/dark nếu có.
- API: test liên quan tới search/RAG/migration/ACL qua harness, cộng CI GitHub. Nếu M12 đã merge thì dùng lệnh song song của M12.
- Không áp dụng: hash-seal source, inventory JSON toàn bộ node, countercheck đột biến (trừ phần ACL của ANN nên có một ca chứng minh test bắt được lỗi bỏ lọc quyền).
- Một PR tiếng Việt, không merge, giữ author/committer, không AI co-author/trailer/promo. Handoff `docs/m13-library-qa.md` gồm:
  - số đo trước/sau;
  - quyết định ANN và recall;
  - kết quả live theo nhóm câu, chi phí ledger;
  - backup/rollback;
  - câu hỏi gợi ý để dùng.

## Ngoài phạm vi

- Hội thoại nhiều lượt có ngữ cảnh.
- Streaming.
- Tóm tắt paper tự động (cần chủ dự án yêu cầu mới).
- Lưu lịch sử phía server.
- Cải thiện thuật toán so sánh nhiều paper (M14).
- Đổi model, nới budget/quota ứng dụng.
- Đụng dev `ras`/Dify/Tailscale/Radmin.

**Trạng thái khi giao:** mới giao, chưa triển khai. Có thể chạy song song với [M12](m12-fast-ci-and-test-tiers.md); PR nào merge sau thì rebase.
