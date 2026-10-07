// Browser-side API client. Calls go through the same-origin proxy (/api/*).
// State-changing requests echo the `ras_csrf` cookie in X-CSRF-Token.
// The UI only reflects what the API allows; it never decides permissions.

export class ApiError extends Error {
  status: number;
  code: string;
  detail?: string;

  constructor(
    status: number,
    code: string,
    detail?: string,
  ) {
    super(code);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export type User = { id: string; email: string; display_name: string | null; role: "admin" | "user" };

function readCookie(name: string): string | null {
  for (const part of document.cookie.split("; ")) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq) === name) return decodeURIComponent(part.slice(eq + 1));
  }
  return null;
}

export async function csrfToken(): Promise<string> {
  let token = readCookie("ras_csrf");
  if (!token) {
    await fetch("/api/auth/csrf", { cache: "no-store", credentials: "same-origin" });
    token = readCookie("ras_csrf");
  }
  if (!token) throw new ApiError(0, "csrf_unavailable");
  return token;
}

export async function api<T = unknown>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { accept: "application/json" };
  if (method !== "GET") {
    headers["x-csrf-token"] = await csrfToken();
    headers["content-type"] = "application/json";
  }
  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    cache: "no-store",
    credentials: "same-origin",
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data?.error ?? "http_error", data?.message);
  return data as T;
}

const MESSAGES: Record<string, string> = {
  invalid_credentials: "Email hoặc mật khẩu không đúng.",
  session_expired: "Phiên đã hết hạn, hãy đăng nhập lại.",
  not_authenticated: "Bạn cần đăng nhập.",
  invalid_token: "Liên kết không hợp lệ, đã dùng hoặc đã hết hạn.",
  weak_password: "Mật khẩu chưa đạt yêu cầu.",
  csrf_failed: "Yêu cầu bị từ chối (CSRF). Tải lại trang rồi thử lại.",
  forbidden: "Bạn không có quyền thực hiện thao tác này.",
  not_found: "Không tìm thấy.",
  account_exists: "Email này đã có tài khoản.",
  delivery_unavailable: "Chưa cấu hình kênh gửi liên kết.",
  invalid_email: "Email không hợp lệ.",
  cannot_change_own_status: "Không thể tự vô hiệu hóa tài khoản của mình.",
  invalid_intent: "Phiên xác nhận upload không hợp lệ, đã dùng hoặc hết hạn. Hãy xác nhận lại.",
  upload_intent_unavailable: "Chưa hoàn tất xác nhận upload. Hãy thử lại sau.",
  upload_intent_outcome_uncertain: "Chưa xác định được kết quả xác nhận upload. Không gửi file hoặc tự gửi lại yêu cầu; tải lại trang trước khi xác nhận lần mới.",
  upload_intent_response_unavailable: "Xác nhận upload đã được lưu nhưng chưa trả được token. Không gửi file; tải lại trang trước khi xác nhận lần mới.",
  upload_intent_changed: "Xác nhận upload không còn khớp. Không gửi file; tải lại trang và xác nhận lần mới.",
  upload_intent_expired: "Xác nhận upload đã hết hạn. Không gửi file; tải lại trang và xác nhận lần mới.",
  notice_changed: "Thông báo xử lý đã thay đổi. Tải lại trang và xác nhận lại.",
  file_too_large: "File vượt giới hạn cho phép.",
  file_mismatch: "File không khớp với lần xác nhận.",
  invalid_pdf: "PDF không hợp lệ hoặc vượt giới hạn trang.",
  invalid_markdown: "Markdown không hợp lệ.",
  upload_unavailable: "Upload chưa hoàn tất. Kiểm tra thư viện trước khi thao tác tiếp.",
  upload_outcome_uncertain: "Chưa xác định được kết quả upload. File có thể đã được nhận; kiểm tra thư viện khi đăng nhập được. Không tự gửi lại file hoặc xác nhận upload mới để thử lại.",
  upload_response_unavailable: "File có thể đã được nhận nhưng phản hồi chưa khả dụng. Kiểm tra thư viện; không tự gửi lại upload.",
  upload_integrity_failed: "File lưu không khớp lần xác nhận. Upload bị từ chối.",
  upload_body_timeout: "Quá thời gian nhận file. Upload bị dừng.",
  upload_busy: "Các lượt upload đang bận. Thử sau khi các lượt hiện tại kết thúc.",
  rag_generation_unavailable_unverified_pricing: "Hỏi đáp hiện chưa khả dụng vì chi phí và provider chưa được xác minh.",
  rag_scope_changed: "Quyền hoặc phạm vi của nguồn đã thay đổi trong lúc xử lý; câu trả lời không được hiển thị. Hãy hỏi lại.",
  api_timeout: "Máy chủ không phản hồi kịp. Thử lại sau.",
  api_unreachable: "Không kết nối được tới máy chủ.",
  api_response_incomplete: "Phản hồi từ máy chủ bị gián đoạn. Thử lại sau.",
  api_not_configured: "Máy chủ web chưa được cấu hình đúng.",
  review_request_open: "File này đã có yêu cầu duyệt đang mở.",
  review_request_closed: "Yêu cầu này đã đóng; không thể thay đổi nữa.",
  review_file_integrity_failed: "File gốc không khớp checksum lúc gửi yêu cầu; không mở được để duyệt.",
  private_file_integrity_failed: "File gốc bị thiếu hoặc không khớp SHA-256; không tải được.",
  download_audit_outcome_uncertain: "Chưa xác định được kết quả ghi nhật ký tải. Không tự tải lại; kiểm tra sau khi kết nối ổn định.",
  review_grant_inactive: "Quyền duyệt của yêu cầu này không còn hiệu lực (quyền của item đã thay đổi).",
  checksum_confirmation_mismatch: "SHA-256 xác nhận không khớp file của yêu cầu; tải lại trang và kiểm lại.",
  rights_evidence_required: "Cần URL giấy phép hoặc ghi chú bằng chứng do Admin tự kiểm.",
  rights_scope_inconsistent: "Xử lý qua provider (embedding/RAG) cần cả quyền tìm kiếm toàn văn.",
  checked_on_in_future: "Ngày kiểm không được ở tương lai.",
  approval_storage_unavailable: "Không sao chép/kiểm được file gốc sang bản chung; chưa có gì được ghi nhận. Thử lại sau.",
  // M6c-2b1: the journaled close may still have ended the request (closed_by_recovery) without any copy.
  approval_unavailable: "Không ghi được phê duyệt; không có bản chung nào được tạo. Tải lại trang để xem yêu cầu còn mở hay đã đóng.",
  approval_outcome_uncertain:
    "Chưa xác định được kết quả phê duyệt: bản chung có thể đã được tạo và quyền xem tạm có thể đã kết thúc. Tải lại trang để kiểm tra trạng thái; không tự gửi lại phê duyệt.",
  approval_response_unavailable:
    "Phê duyệt đã được lưu nhưng chưa đọc được phản hồi mới. Tải lại trang để xem trạng thái; không gửi lại phê duyệt.",
  approval_busy: "Đang có nhiều phê duyệt sao chép file cùng lúc; phê duyệt này chưa bắt đầu. Thử lại sau.",
  approval_copy_timeout:
    "Sao chép/kiểm file gốc vượt thời gian cho phép; phê duyệt chưa được ghi. Tải lại trang để kiểm tra trạng thái trước khi thử lại.",
  already_published: "Bản này đã được công bố.",
  common_copy_revoked: "Bản này đã bị thu hồi; không thể công bố hay thu hồi lại.",
  rights_not_publishable: "Quyền đã ghi không còn đúng cho bản này (đã đổi epoch/thu hồi hoặc lệch file); không công bố.",
  duplicate_published_checksum: "Đã có bản chung đã công bố với cùng SHA-256; không công bố bản trùng.",
  common_file_integrity_failed: "Object của bản chung bị thiếu hoặc không khớp SHA-256; bản vẫn ở trạng thái chờ.",
  publication_storage_unavailable: "Không đọc được object của bản chung; chưa công bố. Thử lại sau.",
  publication_unavailable: "Không ghi được việc công bố; bản vẫn ở trạng thái chờ. Thử lại sau.",
  revocation_unavailable: "Không ghi được việc thu hồi; bản chưa bị thu hồi. Thử lại ngay.",
  revocation_journal_unavailable:
    "Bản đã bị thu hồi nhưng không ghi được nhật ký thu hồi; hệ thống tạm ngừng phục vụ tới khi quản trị viên sửa nhật ký.",
  revocation_pending_recovery:
    "Nhật ký đã ghi việc thu hồi nhưng database chưa áp dụng; hệ thống tạm ngừng phục vụ tới khi quản trị viên chạy phục hồi.",
  service_recovering: "Hệ thống đang tạm ngừng phục vụ để kiểm tra/phục hồi dữ liệu thu hồi. Thử lại sau.",
  resource_scope_changed: "Danh sách hoặc quyền truy cập đã thay đổi trong lúc tải. Hãy tải lại trang.",
  resource_write_outcome_uncertain: "Chưa xác nhận kết quả thay đổi. Kiểm tra lại sau khi kết nối hoặc đăng nhập; không tự gửi lại thao tác.",
  resource_write_unavailable: "Chưa hoàn tất thay đổi. Hãy kiểm tra trạng thái và thử lại sau.",
  resource_write_response_unavailable: "Thay đổi đã được lưu nhưng chưa đọc được phản hồi. Hãy kiểm tra lại trạng thái trước khi thao tác tiếp.",
  review_closure_unavailable: "Không ghi được việc đóng yêu cầu; yêu cầu vẫn đang mở. Thử lại ngay.",
  review_write_unavailable: "Không hoàn tất được thao tác. Hãy kiểm tra lại trạng thái yêu cầu.",
  review_write_outcome_uncertain:
    "Chưa xác định được kết quả lưu. Yêu cầu có thể đã lưu hoặc quyền review có thể kết thúc qua phục hồi. Kiểm tra lại trạng thái; không tự gửi lại.",
  review_write_response_unavailable:
    "Thao tác đã lưu nhưng chưa đọc được phản hồi mới. Hãy tải lại trạng thái yêu cầu sau khi kết nối hoặc đăng nhập lại.",
  review_journal_unavailable:
    "Yêu cầu đã đóng nhưng không ghi được nhật ký; hệ thống tạm ngừng phục vụ tới khi quản trị viên sửa nhật ký.",
  review_closure_pending_recovery:
    "Nhật ký đã ghi việc đóng yêu cầu nhưng database chưa áp dụng; hệ thống tạm ngừng phục vụ tới khi quản trị viên chạy phục hồi.",
  session_journal_unavailable: "Phiên đã bị thu hồi; hệ thống đang tạm ngừng phục vụ để khôi phục nhật ký.",
  session_revocation_pending_recovery: "Hệ thống đang khôi phục việc thu hồi phiên. Thử lại sau.",
  auth_outcome_uncertain: "Chưa xác định được kết quả thao tác tài khoản. Kiểm tra trạng thái trước khi thử lại.",
  auth_journal_unavailable: "Thao tác tài khoản đã được ghi vào cơ sở dữ liệu; hệ thống đang khôi phục nhật ký. Kiểm tra trạng thái trước khi thử lại.",
  auth_closure_pending_recovery: "Hệ thống đang khôi phục việc kết thúc lời mời, token hoặc phiên. Chưa xác định kết quả thao tác tài khoản.",
  auth_unavailable: "Chưa hoàn tất thao tác tài khoản. Thử lại sau.",
  file_unavailable: "Chưa đọc được file gốc. Thử lại sau.",
};

export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    const base = MESSAGES[err.code] ?? `Lỗi (${err.code}).`;
    return err.detail ? `${base} ${err.detail}` : base;
  }
  return "Không kết nối được tới máy chủ.";
}

export function tokenFromHash(): string | null {
  // Links carry the token in the URL fragment, which is never sent to servers.
  const params = new URLSearchParams(window.location.hash.slice(1));
  return params.get("token");
}
