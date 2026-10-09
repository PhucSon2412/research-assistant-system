# C2d-5c2: thu hồi bản chung qua runtime

**Đã merge PR #49 `859dfe9793dd9549e3ca9b8f0102361f414d0340` ngày 2026-10-09**, tree khớp head `f8dc44c`. Chưa rerun tại merge; bằng chứng bên dưới giữ source lịch sử. Tiếp theo [C2d-5d1 HTTP index request](tasks/m6c2b2c2d5d1-common-index-request.md), worker/index/provider dispatch tách PR sau; safety pending tới C4.

## Review độc lập tại f8dc44c

**Review PR #49 tại `f8dc44c`: không phát hiện điểm chặn merge mới trong phạm vi POST revoke.** Reviewer thực chạy **295/295 API**, **66/66 web**, typecheck/build và hai Chrome qua proxy; artifact **529 API/8 mutant** của agent đối chiếu riêng. Raw lượt observer yếu bị ghi đè được giữ là giới hạn evidence. PR OPEN tại lúc review, sau đó merge `859dfe9`; source `0042`, **safety pending tới C4**. [Report](reviews/pr49-c2d5c2-common-revoke.md). Reviewer không sửa code; số agent bên dưới giữ nguyên theo từng source, không thành pass mới ở head hoặc merge.

[Task](tasks/m6c2b2c2d5c2-common-revoke.md) · [Inventory](c2d5c2-test-inventory.json) · [Publish](c2d5c1-common-publish.md)

Base đã fetch/xác minh: PR #48 MERGED, `origin/main` chứa
`1eede17c1eed82fa7a00f7018fe01b4362ce30a9`; nhánh
`feat/m6c2b2c2d5c2-common-revoke` bắt đầu đúng merge này. Giữ đầy đủ docs
orchestrator, task và report PR #48 chưa commit. Số reviewer 216 unique/64 web
và agent 2.016/7 mutants của PR #48 là lịch sử riêng, không pass của task này.
Migration source `0041 → 0042`, không query revision DB dev.
Implementation `79cfd95ebc62522c1db776de8731a1428bb78dde`; lượt 528 ca tại
`964b9f6a8d7aea47d7ed389ed7802a2b527ccf87` bổ sung test race/restore. Một ca
Chrome publish bổ sung tại `30cb7294e4752726617c8cc0a516970b00e24417` chỉ sửa
chính test Chrome đó; production/shared fixtures không đổi. Commit tiếp theo
chỉ docs; PR ghi head cuối, inventory giữ source từng lượt riêng, không suy
529 ca đã rerun tại head mới hoặc head tài liệu.
**Safety pending tới C4; chỉ một POST revoke được chuyển.**

## Boundary và quyền

`api/common_revoke.py` sở hữu Session/executor/outcome của attempt. Entry dùng
principal/session/Origin thật; acceptance kiểm lại exact session/User, latest
head/receipt/generation, active/proven/usable, Admin và raw CSRF, lấy clock sau
predicate SQL cuối. Không touch idle trong final helper.

Thứ tự khóa: Admin User NO KEY UPDATE → common item UPDATE → rights UUID →
publication → jobs UUID → version/file UUID → tombstone/bases/audit mới, flush
toàn DML/FK/unique → journal exclusive trực tiếp. Không shared rồi upgrade,
không khóa tombstone cũ sau item. Denial revoked kiểm current caller/gate rồi
409, không epoch/journal/retention lần hai. Không thêm audit denial độc lập.

Python kiểm exact staged body/tuple/job snapshot; hai routine
`ras_auth_common_revocation_mirror(text)` và `..._confirm(text)` kiểm SQL độc
lập. Trigger mint prior state/epoch và `txid_current`; tombstone, item, rights,
publication, audit/bases phải có xmin của transaction hiện tại. Không tin GUC
hay marker caller khai. Live routine chỉ nhận revocation, exact envelope/body,
hash/chain, UUID/type/code/time/key và toàn retention basis. Parser lịch sử
không đổi. Savepoint tạo xmin subtransaction bị từ chối trước append; acceptance
không dùng savepoint. Marker của hàng lịch sử vẫn NULL khi upgrade.

Mirror false và deferred constraints được kiểm trước byte đầu; current
actor/binding lại sau routine SQL ngay trước append. Confirm chỉ exact line và
hàng của cùng transaction sau fsync thành công. SECURITY DEFINER NOLOGIN,
fixed search_path, PUBLIC bị thu hồi; fresh/existing provisioning cùng allowlist.
Không raw journal/auth writes, DDL/TEMP/role switching cho runtime; không mở
recovery/migrator connection trong API.

Đối chiếu allowlist còn phát hiện generic `ras_auth_confirm(seq)` từ nền auth
có thể confirm mọi kind. Revision mới loại riêng `revocation` khỏi generic này,
giữ các domain auth khác; nếu không, caller runtime có thể bỏ qua confirmer hẹp
bằng seq của attempt cũ. Recovery/operator vẫn dùng đường sửa mirror hiện hành.

Revoke nhận pending/listed/indexed kể cả conditions/không permits; không đọc,
hash hay xóa object. Known commit tăng epoch một lần, đóng rights/publication,
metadata_only/active NULL, cancel unfinished jobs, tombstone/retention/audit/mirror
nguyên tử. `t_rev` là DB commit. Bản private cùng bytes không đổi. Tuple hỏng
được từ chối có kiểm soát; không tự sửa dữ liệu, cấp quyền hay bỏ gate để thu hồi.

## Failure matrix và response

| Cửa sổ | Kết quả |
| --- | --- |
| Caller/gate/binding/SQL/FK/audit trước append | Rollback, không byte mới; auth/business/503 có kiểm soát |
| Append none/torn/full nhưng fsync lỗi | Commit restriction + mirror false; `503 revocation_journal_unavailable`; gate đóng |
| Durable append, confirm/hậu kiểm lỗi trước commit | Rollback; current denial hoặc `503 revocation_pending_recovery`; intent dành cho operator recover |
| Commit ném lỗi, landed hoặc rollback | `503 revocation_outcome_uncertain`; không SELECT inference/replay/retry/DTO |
| Known commit, teardown/response lỗi | Giữ thu hồi; current denial hoặc `503 revocation_response_unavailable` |
| Known commit + confirmed journal + proof/release sạch | 200 history DTO hiện hành, count cancel thuộc attempt |

Nhánh append lỗi là lựa chọn restriction-only scoped đã chốt: authorization
boundary ngay trước byte đầu; TTL trôi trong lỗi append không rollback mất false
mirror, không normal gate/current-caller hậu kiểm và không success/payload.
Không áp ngoại lệ này cho lỗi mirror/binding/confirm/commit hoặc writer cấp quyền.
False known commit giữ lỗi journal làm chính kể cả teardown lỗi; uncertain commit
ưu tiên cao hơn. Outcome không bị rollback/close/invalidate xóa fact.

Response dùng Session/transaction mới: common item SHARE → rights/version/file
SHARE → journal shared → current Admin/session/CSRF/clock → formatter/history/
cleanup decoration → current caller/TTL sau SQL cuối → rollback/close sạch.
Denial formatter muộn cũng kiểm actor mới. Cleanup thắng trước proof trả metadata
đã scrub; count cancel không lấy lại từ worker/retry. Cleanup tombstone→item và
response item SHARE cùng chạy qua barrier thật, không upgrade khóa.

Cancel trước intent dừng/rollback; sau append_started attempt tiếp tục hoàn tất
restriction theo outcome thật, giữ executor/connection tới thread settle và không
DTO khi disconnect. Không tự replay HTTP. Journal durable có thể được operator
recover áp về sau dù HTTP lỗi; không chứng minh POST đã commit.

## UI, tương thích và giới hạn

Form bắt đầu reason trống/unticked, guard đồng bộ bằng ref và disable khi busy;
lỗi bỏ tick. Transport/502/504/body thiếu/commit uncertain nói unknown, tải lại
trạng thái, không tự POST lại. Lỗi journal nói quyền có thể đã đóng và cần recovery.
Revoke giữ proxy normal 15 s. Chrome thật kiểm hai submit đồng bộ chỉ một POST,
old original URL 404, known commit rồi proxy 504/unknown, cookie expiry forwarding.

Fixture publish/download và helper revoke trong legacy content tests dùng runtime
Admin/session/CSRF. Legacy `_revoke_rows`/`_cancel_jobs` và system cleanup/recovery
vẫn được giữ; không tuyên bố đã chuyển worker/index/search/RAG/cleanup sang boundary
mới. Corruption/restore/operator setup có nhãn và dùng engine đặc quyền riêng;
HTTP không fallback. Các hồi quy cũ được cập nhật vì có auth records trong journal,
ready reason thay open và HTTP không replay; không hạ ACL/fence để làm xanh.

Recovery snapshot trước revoke vẫn đóng gate tới replay/cleanup; item absent không
dựng metadata/bases mới, căn cứ còn đầy đủ trong mirror. File/dẫn xuất chung mất
quyền, job của snapshot còn item được cancel, private cùng hash giữ nguyên,
recover lặp không mở lại. Response đã thắng
boundary trước `t_rev` có thể stream hết; bytes/provider payload đã gửi không lấy lại.

## Kiểm chứng và cleanup

Exact source, selection/JUnit unique, pass/fail/not-run, hashes, commands và các
lượt chuẩn bị/đối chứng được ghi trong inventory. Không cộng pass lịch sử hoặc các
lượt chồng nhau. Conda ras; PostgreSQL `58479`, MinIO `59479`, Chrome/socket API
`58489`/Next `53489` và Chrome publish `58486`/Next `53486`; label
`ras.task=c2d5c2-20261009`. Credentials/evidence/journal
ngoài repo. Chặn HTTP ngoài; providers chỉ fake, không đổi pricing_verified thật.
Business cutover scoped trong fixture; HTTP pending riêng không patch production gate.

Union cuối: **529/529 API unique pass**, 0 fail/error/skip; collect **4.096**,
**3.567 not-run**. Gồm 528 tại `964b9f6` và một Chrome publish tại `30cb729`.
Union JUnit khớp selection, không trùng node. **295 hashes API/web trước/sau
khớp trong từng vòng**; giữa hai vòng chỉ đổi test Chrome publish, 294 file còn
lại (gồm toàn production/shared fixtures) không đổi.

| Nhóm cuối | Pass |
| --- | ---: |
| Revoke mới + migration mới + migration/ORM + Chrome | 201 |
| Publish runtime | 191 |
| Publication | 18 |
| Recovery chọn theo caller bị ảnh hưởng | 11 |
| Cleanup | 5 |
| Common index/search/RAG revoke/cleanup | 19 |
| Download common closure/revoke | 3 |
| Original revoke | 2 |
| Review writers chọn theo journal/commit/privilege | 24 |
| Approval chọn theo response/fault/privilege | 30 |
| Auth foundation/login chọn theo grant/fsync | 24 |
| Chrome publish cũ dùng runtime revoke (vòng bổ sung) | 1 |

Chrome revoke là một node trong nhóm 201; Chrome publish là node bổ sung riêng,
không đếm đôi. Web **66/66**, typecheck và production build đạt tại `79cfd95`;
source web không đổi tới `30cb729`. **8/8 mutant**
bắt đúng assertion, 0 setup error; source được khôi phục trước lượt suite cuối.
Các nhóm/node không chọn được ghi đầy đủ trong inventory, không coi là pass.

Cleanup hoàn tất cả hai vòng: gỡ đúng **4 container/4 volume** thử sau khi đối
chiếu exact ID/label/image/port/mount/sole consumer; xóa hai bộ ba file credentials
(6 lần xóa, không còn file credentials). Chín
container có sẵn giữ ID/image reference từ snapshot đầu; image SHA/StartedAt/
mounts khớp từ snapshot inspect vòng đầu tới trước/sau cả hai cleanup, kể cả giữa
hai vòng. Snapshot đầu không
có StartedAt, không suy mốc này ngược về lúc bắt đầu. Socket API/Next đã dừng.
Evidence/JUnit/log/journal giữ ngoài repo tại
`C:\Users\ADMIN\AppData\Local\Temp\ras-c2d5c2-20261009`;
không commit artifact/paper/journal/credentials. Fingerprint credentials (kể cả
sau xóa giá trị), local docs links/control chars và diff đều sạch.

Lượt seal đầu tại `79cfd95`: core 201 pass; publish dừng ở 166 pass/1 fail vì
observer cũ chỉ chờ transactionid trong khi preflight revoke đang chờ journal.
Barrier chuyển đến writer item trước journal; không đổi logic production hay
expectation ACL. Restore bổ sung job để kiểm cancel trực tiếp. Sáu test follow-up
đạt tại source sau sửa; kết quả này và lượt đầu giữ riêng, không cộng vào lượt cuối.

Rà caller cuối phát hiện Chrome publish cũ cũng dùng helper revoke đã đổi. Sau
cleanup vòng 528, tạo vòng cô lập mới, ca này ban đầu fail tại `964b9f6` vì
Playwright sync có event loop, helper runtime gọi `asyncio.run()` trên cùng
thread. Sửa riêng test gọi helper qua executor thread, không fallback quyền;
ca Chrome đạt tại `30cb729`. XML/log lỗi giữ riêng (`prep-publish-proxy-event-loop`).

Giới hạn lưu evidence: lượt mutant lock observer yếu ban đầu còn summary và hash
JUnit gốc, nhưng tên XML/log đã bị lượt chạy lại ghi đè; không tuyên bố còn raw
XML/log của lượt yếu. Tám mutant bắt assertion sau sửa có XML/log riêng, không
tính lượt yếu là bắt lỗi. Inventory ghi giới hạn này cùng các lỗi chuẩn bị khác.

Không kiểm Compose API image/deployment, full business cutover/C3/C4, provider
charge, crash/mất điện thật, backup Compose drill, SLO/benchmark. Worker/cleanup
được kiểm tương thích hiện hành, không chuyển caller runtime. Handoff tiếp theo:
common-index/worker boundary; không tự bắt đầu C3/C4.
