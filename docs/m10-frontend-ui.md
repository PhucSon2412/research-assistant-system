# M10 — giao diện web cho demo CV

**Cập nhật sửa review PR #71:** xem [mục sửa font/wrapping/CI](#sửa-review-pr-71--font-tiếng-việt-wrapping-và-ci). Các số liệu ui1 bên dưới giữ theo lượt M10 đầu.

Task: [M10](tasks/m10-frontend-ui-redesign.md). Base đã fetch/xác minh:
`0dd5a9c07ac0561c55b97da52555b2777d291817` (merge PR #70), nhánh `feat/m10-frontend-ui`.
Source migration **0052**. Không migration, không sửa `api/`, `deploy/`, `web/lib/`, route/proxy hay dependency npm.

## Thiết kế và cấu trúc

- [`web/app/globals.css`](../web/app/globals.css): token màu, spacing 4/8 px, font hệ thống, radius, shadow,
  z-index; button/input/select/textarea/checkbox, bảng, notice, focus ring và breakpoint mobile. Dùng theme sáng
  nhất quán; không tự đổi sang dark mode. Tôn trọng `prefers-reduced-motion`.
- [`components/ui.tsx`](../web/app/components/ui.tsx) + CSS Module: icon SVG nội tuyến, Brand, PageHeading, Badge,
  EmptyState, Loading, AuthFrame và ConfirmButton dùng native `<dialog>` (Escape, focus trap và trả focus).
- [`components/session.tsx`](../web/app/components/session.tsx): sidebar desktop, navigation mobile, current-page,
  account/logout, skip link và landmark `main`. Điều kiện hiện Admin vẫn là role từ session API.
- Mọi trang có style chung; không còn `style=` ad-hoc. Thư viện có upload card với nguyên văn notice API và checkbox
  riêng từng lượt. Bảng chỉ dùng dữ liệu/quyền API trả về. Empty state bổ sung chỉ hiện sau khi có response.
- Tìm kiếm có form phạm vi/chế độ và card đoạn trích. RAG vẫn ở trang item, được đặt trước metadata chi tiết;
  claim có số `[claim.citation]` dẫn đến panel nguồn. URL chunk/file và locator vẫn lấy từ response/helper cũ;
  không dựng URL object storage. Giữ đủ partial/withheld/insufficient/unavailable/error/unknown-outcome.
- Vùng xóa item nằm cuối trang, giữ nguyên xác nhận và text không hoàn tác. Admin thêm dialog trước thu hồi lời
  mời/vô hiệu hóa tài khoản; các POST và điều kiện quyền không đổi. Các bước xác nhận publish/index/revoke giữ nguyên.

Không font/ảnh/script ngoài mạng, không asset AI, không analytics hoặc dependency runtime mới.

## Kiểm chứng và evidence

Context tổng hợp ngoài repo: `%TEMP%\ras-m10-20261022\`.
Ảnh trước: `before-{login,library}-{1440,390}.png` từ image web rc2 qua Playwright CLI.
Ảnh đầy đủ, ma trận, source hash và log nằm trong context; bộ dùng cho CV ở [screenshots](screenshots/README.md).
Chỉ chứa tài khoản `research.example.org`, paper tự viết và provider giả.

| Kiểm | Kết quả |
|---|---|
| `npm.cmd ci --prefix web` | Pass, lockfile/dependency không đổi |
| `npm.cmd test --prefix web` | 79/79 pass, không sửa/làm lỏng test cũ |
| `npm.cmd run typecheck --prefix web` | Pass |
| `npm.cmd run build --prefix web` | Pass |
| `python -m unittest discover -s scripts/tests -p test_m10_web_image.py` | 4/4 pass; env bytes, image persistence, web-only/no-pull, từ chối ownership sai |
| Smoke HTTPS `scripts/m8a_smoke.py` | HTTP 68/68; Chrome 16/16, 0 outbound |
| Ma trận Chrome 1440/390 | 54 ảnh, 0 tràn ngang trang, 0 control thiếu label |
| Kiểm riêng trên image cuối 1440/390/360 | 18 lượt: login, thư viện, collection, search, RAG, Admin; 0 tràn ngang |

Python dùng Conda `C:\Users\ADMIN\.conda\envs\ras\python.exe`. PowerShell gọi `npm.cmd`/`npx.cmd` vì policy máy
không cho chạy shim `.ps1`; không đổi execution policy. CLI: `npx.cmd --yes --package @playwright/cli playwright-cli
-s=m10 open <origin C4>/login --browser chrome --headed`, snapshot → tương tác theo ref → resize → screenshot.
Ma trận lặp lại bằng Playwright Python/Chrome cài sẵn; script giữ ở `<context>\qa\`, kết quả ở
`visual-seal\` và `visual-targeted\`. Inventory đã lọc: [m10-test-inventory.json](m10-test-inventory.json).

54 ảnh phủ login/error, quên mật khẩu, invite/reset hợp lệ, thư viện rỗng/có dữ liệu, collection rỗng/có chọn,
discovery, account, nguồn, item, tiêu đề dài, RAG answered/insufficient/unavailable, search initial/results/empty,
item 404, Admin/accounts/reviews/detail/publications/dialog và loading. Hai ảnh loading không có h1 vì shell đang
chờ session; mọi trang đã tải có đúng một h1. Kiểm bằng mắt ảnh riêng và contact sheet; kiểm thêm Admin mobile sau
khi thấy nút bị ép chữ thành cột dọc. Bản cuối dùng card có nhãn tại 360/390 px. Collection chờ đủ 3 item trước ảnh chọn lọc.

Keyboard: Tab từ login vào Email có ring; upload tới các control có nhãn; search tới scope; citation RAG mở đúng
source; dialog focus ở Hủy, Tab tới Xác nhận, Escape trả focus. Xác nhận disable tài khoản tổng hợp → API 200,
rồi enable lại qua action cũ; không thao tác tài khoản demo. RAG unavailable được mô phỏng 503 tại browser;
insufficient-evidence chạy API thật với truy vấn không khớp. Không dùng kết quả giả làm chất lượng thật.

**Smoke:** mạng egress internal, CA/NSS và SMTPS capture tổng hợp, provider tắt. Web build từ source của PR.
Image API thử dùng `FROM ras-pilot-api:rc2` rồi `COPY api/app` + `COPY api/migrations` để đối chiếu đúng 223 file
source, giữ dependency rc2 (pypdf 6.21.1). **Không dùng** image API tải dependency mới từ lần build mặc định đầu tiên.
API demo vẫn là rc2 nguyên bản. Lệnh:

```powershell
& $py scripts/m8a_smoke.py --context <context-ngoài-repo> setup
# Tag API thử và web ui1 vào hai tên image riêng của context; không đổi deploy/.
& $py scripts/m8a_smoke.py --context <context> compose up -d --wait --no-build
& $py scripts/m8a_smoke.py --context <context> http
docker build -t rasm8a-browser:local -f scripts/m8a_browser.Dockerfile scripts
& $py scripts/m8a_smoke.py --context <context> browser
```

Không sửa selector hoặc assertion nào trong `m8a_browser.py`/`m8a_smoke.py`. Giữ nguyên ý nghĩa 68 HTTP và 16 Chrome check.
HTTP cuối ở `smoke-final/evidence/http-1792659923393174400` trên web `b5cea4e9…`; sau đó chỉ sửa cờ hiển thị
empty state và card Admin mobile. Chrome 16/16 đã chạy lại trên **image cuối** `36ed5b03…` tại
`chrome-1792660567117319800`; API/proxy không đổi giữa hai lượt. Không cộng các lượt chạy lặp thành tổng test mới.

**Lượt chưa đạt được giữ riêng:** web test lần đầu 77/79 vì test harness không resolve import icon trình bày trong
Upload; chuyển icon nhỏ sang SVG nội tuyến, không sửa test. Smoke đầu dừng đúng khi rc2 không có module evaluation
M7c; image thử source-current/rc2-dependencies xử lý vấn đề này. Chrome lần đầu exit 125 do thiếu image browser;
build image rồi chạy lại. Các lượt ma trận thử có selector `exact=True` không khớp label chứa select, chờ dữ liệu
chưa đủ và race route delay của harness; đã tách trang loading riêng. Không tính ảnh/load chưa hoàn tất là pass.

Tương phản theo WCAG luminance: body 12,71:1; muted/placeholder 5,47:1; accent/primary 6,31:1; sidebar 7,54:1;
warning 6,42:1; danger 6,16:1; success 6,40:1. Badge/notice có chữ, không truyền nghĩa chỉ bằng màu.
Keyboard kiểm login, upload, search/RAG và Admin dialog; chi tiết trong ma trận ngoài repo.
Provider giả chỉ chứng minh luồng hiển thị/citation, không đánh giá chất lượng trả lời thật.
Đối chiếu AST của 20 TSX thay đổi: mọi call API/fetch/CSRF, helper dựng body và helper link file/nguồn khớp base.
`git diff` của `api/`, `deploy/`, `web/lib/`, `web/app/api/` và hai file dependency npm rỗng.

## Web image demo và quay lại

[`scripts/m9_lan_demo.py`](../scripts/m9_lan_demo.py) thêm subcommand `web-image`. Chỉ nhận local tag
`ras-pilot-web:<tag>`, kiểm image tồn tại và ownership nhãn container trước khi ghi. Chỉ thay một dòng
`RAS_WEB_IMAGE` trong `pilot/pilot.env`, giữ nguyên bytes khác/ACL, lưu `web_image` vào state. Không đọc key provider
hoặc tái sinh SMTP/provider. `pilot_env()` dùng lại lựa chọn này khi `rebind`/`provider` sinh env;
drill vẫn dùng web rc2.

```powershell
docker build -t ras-pilot-web:ui1 -f web/Dockerfile web
& $py scripts/m9_lan_demo.py web-image --image ras-pilot-web:ui1 --apply
& $py scripts/m9_lan_demo.py verify

# Quay lại giao diện cũ, giữ API/dữ liệu/tài khoản:
& $py scripts/m9_lan_demo.py web-image --image ras-pilot-web:rc2 --apply
& $py scripts/m9_lan_demo.py verify
```

Apply chạy Compose với đủ `--env-file`, project `ras-pilot`, hai compose file của release/overlay;
`up -d --no-deps --wait --pull never --force-recreate web`. Không recreate API/worker/beat/edge/DB/storage,
không migrate, không `down`, không đổi 12 paper hoặc tài khoản demo. Lệnh rollback đã được chuẩn bị;
không cần rollback demo khi UI đang chạy đúng.

**Đã triển khai (2026-10-22):** `web-image --image ras-pilot-web:ui1 --apply` exit 0, state lưu `ui1`.
Web image **`sha256:36ed5b03a68961f491e33a41ef487e68b136fe0906db317218d9ca0b673cafb6`**;
API vẫn **`sha256:c0936a8189df7335187c457ff56e8c4ff21aebbe338ed7e4240e5c14ec730a06`**.
`verify` pass (SNI/CA demo → HTTPS 200, chỉ IP Ethernet nghe 8443, interface khác đóng); API `/readyz` nội bộ 200.
Đối chiếu `demo-before.json`/`demo-after.json` theo **tập mount**: chỉ web thay ID/image/start; mọi service khác
giữ ID/image/start/status/mount. So sánh list mount ban đầu báo khác do thứ tự Docker trả về; chuẩn hóa theo
destination xác nhận tập không đổi. Không gọi search/RAG/provider thật trên demo, không đổi dữ liệu/tài khoản.

Đã chép đủ ba docs bàn giao chưa commit ban đầu vào PR. Không đưa bundle M7b vào thay đổi; sandbox chỉ đọc thư mục
đó bị từ chối quyền, không phải yêu cầu xóa. Không merge PR. Raw evidence giữ ngoài Git; các stack thử được dọn riêng.
Cleanup cả ba project thử đã kiểm nhãn/consumer volume, exit 0, không còn container/volume/network của chúng;
browser CLI session `m10` đã đóng. Image web `ui1` và build cache được giữ; không dùng prune toàn máy.

## Sửa review PR #71 — font tiếng Việt, wrapping và CI

Theo [report tại f81abd8](reviews/pr71-m10-frontend-ui.md) và [task sửa](tasks/fix-pr71-vietnamese-font-wrap-ci.md).
Sửa trên cùng nhánh/PR; giữ nguyên docs reviewer. Không sửa API/proxy/quyền, dependency npm hay workflow CI.

1. `web/types/css-modules.d.ts` khai báo `*.module.css` trong Git; `tsconfig` include tường minh `types/**/*.d.ts`.
   Không phụ thuộc `next-env.d.ts`. Clone sạch ngoài repo tại `%TEMP%\ras-pr71-fix-20261022\clean`, không có
   `next-env.d.ts`/`.next/` trước và trong typecheck/test. Chạy `npm ci --no-audit --no-fund`, preflight typecheck,
   rồi đúng chuỗi CI **test → typecheck → build**: tất cả exit 0, **79/79** test, không sửa expectation.
2. Bỏ toàn bộ Georgia và letter-spacing âm. `--font-reading` dùng `"Times New Roman", Cambria, "Noto Serif",
   "DejaVu Serif", serif`; auth heading/quote và RAG `q` dùng token đó. Kiểm trên **Windows host `win32`, Chrome
   155.0.8059.85**, cả browser headed qua Playwright CLI và Playwright Python dùng Chrome cài trên Windows.
   CDP `CSS.getPlatformFontsForNode` xác nhận ui1 trộn Georgia/Times New Roman trong câu đăng nhập, mẫu thường/nghiêng
   `ồ ắ ầ ẫ ữ ỵ ẽ ở ợ Ý Ặ Ổ` và RAG. Ui2 dùng **chỉ Times New Roman**, không custom/web font, cho cả năm trường hợp.
   Ảnh trước/sau và metadata font giữ ở `<context>/{before,after}/`; ảnh CLI riêng ở root context.
3. Wrapping chung đổi sang `break-word`; chỉ `.opaque-value` (hash đã biết) giữ `anywhere`. Badge và nút dùng
   `nowrap`; bảng thư viện có min-width và cột Kho/trạng thái có min-width. Màn hẹp cuộn **trong** `.table-wrap`;
   Admin tiếp tục thành card ở mobile. Hai bảng kiểm với user thật của stack tổng hợp, ba paper riêng tiêu đề dài
   và một bản chung được approve/publish qua API, ở **1440/1024/768/390/360 px**. Sau sửa: không tràn ngang trang,
   badge không cao hơn một dòng, nút giữ nowrap. Có ảnh thư viện cuộn sang cột Kho/trạng thái trên màn hẹp.

Evidence riêng: `%TEMP%\ras-pr71-fix-20261022\`; không trộn kết quả với lượt ui1. Provider chỉ là fake trong C4;
smoke HTTPS dùng CA/SMTP thử, provider tắt. Source/API thử vẫn là rc2 + bản sao app/migrations hiện tại, dependency rc2.
Lượt setup smoke đầu bị trùng subnet Docker mới cấp cho C4; chỉ đổi subnet context thử sang dải RFC1918 đã kiểm trống.
Không đổi mạng dev/demo/VPN. Không đổi selector/assertion smoke. Kết quả smoke/deploy và inventory chốt bên dưới.

Chrome smoke lần đầu chưa phát POST/API request: helper bấm form SSR trước khi các chunk Next.js hydrate.
`scripts/m8a_browser.py` thêm **chỉ** `page.wait_for_load_state("networkidle")` sau goto login; giữ nguyên toàn bộ
selector, assertion và số POST yêu cầu. Lượt thất bại giữ riêng, không tính là pass. Không sửa runtime auth.

**Lượt chốt ui2:** clean clone 79/79 + typecheck/build; HTTPS **68/68 HTTP + 16/16 Chrome** trên **cùng image ui2**
`sha256:aeb3cc71a7bd254475b3f8cb25261a63fa70da96d786cdc43175793c3a07519b`.
Windows: 10 tổ hợp bảng/viewport, 70 badge và 30 nút kiểm nowrap, 0 tràn ngang toàn trang, 0 outbound;
font glyph 5 trường hợp như trên, 10 ảnh chọn lọc đã thay bằng ảnh ui2 trên Windows.
[Inventory sửa PR71](m10-pr71-fix-inventory.json) tách khỏi inventory ui1 lịch sử.

Demo đã chạy `web-image --image ras-pilot-web:ui2 --apply`, exit 0; HTTPS và API readiness 200. Đối chiếu snapshot
theo ID/image/start/tập mount: chỉ web đổi, API vẫn `c0936a81…`; không query/sửa paper, tài khoản hoặc gọi provider thật.
Rollback gần nhất: `python scripts/m9_lan_demo.py web-image --image ras-pilot-web:ui1 --apply`; rc2 vẫn được giữ.
`m10-test-inventory.json` là snapshot hash ở `f81abd8`; hash source/ảnh ui2 nằm ở inventory sửa, không ghi đè lịch sử.

CI GitHub phải chạy xong trên commit sửa; run ID và kết quả từng job được ghi vào mô tả PR #71 và bàn giao cuối,
không kết luận từ CI local hoặc từ run `39755540725` bị lỗi ở head cũ.
