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
  notice_changed: "Thông báo xử lý đã thay đổi. Tải lại trang và xác nhận lại.",
  file_too_large: "File vượt giới hạn cho phép.",
  file_mismatch: "File không khớp với lần xác nhận.",
  invalid_pdf: "PDF không hợp lệ hoặc vượt giới hạn trang.",
  invalid_markdown: "Markdown không hợp lệ.",
  upload_unavailable: "Chưa thể lưu file. Hãy thử lại.",
  rag_generation_unavailable_unverified_pricing: "Hỏi đáp hiện chưa khả dụng vì chi phí và provider chưa được xác minh.",
  api_timeout: "Máy chủ không phản hồi kịp. Thử lại sau.",
  api_unreachable: "Không kết nối được tới máy chủ.",
  api_response_incomplete: "Phản hồi từ máy chủ bị gián đoạn. Thử lại sau.",
  api_not_configured: "Máy chủ web chưa được cấu hình đúng.",
  review_request_open: "File này đã có yêu cầu duyệt đang mở.",
  review_request_closed: "Yêu cầu này đã đóng; không thể thay đổi nữa.",
  review_file_integrity_failed: "File gốc không khớp checksum lúc gửi yêu cầu; không mở được để duyệt.",
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
