// Original-file links on the item page (M1c private, M6b-2b1 common). The API
// lists a file, with its `original_url`, only when its own SQL gate lets the
// caller download it: the owner's private files, or the one common file whose
// live AccessRights permit download. The page never infers that from
// `visibility`, `publication_state` or a checkbox; no file listed, no button.

export type ItemFile = { id: string; version_id: string; filename: string | null; byte_size: number;
  verified_mime: string; page_count: number | null; original_url: string };

// Only the API's own path shape, opened through the same-origin streaming
// route (never the JSON proxy, never an object-store URL).
export function originalDownloadHref(originalUrl: string | null | undefined): string | null {
  if (typeof originalUrl !== "string") return null;
  return /^\/files\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/original$/.test(originalUrl)
    ? `/api${originalUrl}` : null;
}

// Same-origin link to the original at a hit's location; null unless the API gave a download path
// (search rights never imply download rights, M6b-2b3a).
export function sourceOriginalHref(originalUrl: string | null | undefined, locatorKind: string,
  startPage: number | null): string | null {
  const href = originalDownloadHref(originalUrl);
  if (!href) return null;
  return locatorKind === "pdf" && typeof startPage === "number" ? `${href}#page=${startPage}` : href;
}

type ContentFlags = { visibility: string; content_status: string; fulltext_search_available?: boolean;
  rag_available?: boolean };

// Library/collection label from the API's `fulltext_search_available` and `rag_available` (its own SQL gates),
// never from `content_status` alone: an indexed common copy is not searchable, nor asked, unless its live
// rights say so.
export function contentLabel(item: ContentFlags): string {
  if (item.fulltext_search_available === true) {
    if (item.visibility !== "common") return "Đã index";
    return item.rag_available === true ? "Bản chung: tìm kiếm toàn văn và hỏi đáp"
      : "Bản chung: tìm kiếm toàn văn (không dùng cho hỏi đáp)";
  }
  if (item.visibility === "common" && item.content_status === "indexed") return "Đã lập chỉ mục, chưa mở tìm kiếm";
  return item.content_status === "indexed" ? "Đã index" : "Chỉ metadata";
}

// A common item's note: searchable (API gate) first, with or without RAG (API gate), then
// indexed-but-closed; otherwise it depends only on whether the API listed a downloadable file.
export function commonItemNote(files: readonly ItemFile[], contentStatus = "metadata_only",
  fulltextSearchAvailable = false, ragAvailable = false): string {
  if (fulltextSearchAvailable) {
    if (ragAvailable) return files.length > 0 ? COMMON_RAG_DOWNLOAD_NOTE : COMMON_RAG_NOTE;
    return files.length > 0 ? COMMON_SEARCHABLE_DOWNLOAD_NOTE : COMMON_SEARCHABLE_NOTE;
  }
  if (contentStatus === "indexed") return COMMON_INDEXED_NOTE;
  return files.length > 0 ? COMMON_DOWNLOAD_NOTE : COMMON_METADATA_ONLY_NOTE;
}

const SEARCHABLE = "Tìm kiếm toàn văn khả dụng: mọi tài khoản tìm và mở được đoạn trích của bản chung này theo "
  + "quyền Admin đã ghi (kiểm lại ở mỗi request). ";
const RAG_OPEN = "Hỏi đáp (RAG) dùng được đoạn của bản này làm bằng chứng vì quyền cho gửi qua provider: khi bạn "
  + "hỏi, câu hỏi và các đoạn được chọn được gửi tới OpenRouter/provider. ";
const RAG_CLOSED = "Hỏi đáp (RAG) không dùng bản này: quyền không cho gửi đoạn trích tới provider. ";
const NO_DOWNLOAD = "Quyền tìm kiếm/hỏi đáp không mở tải file gốc: không có file gốc được phép tải.";
const DOWNLOAD = "File gốc dưới đây tải được theo quyền tải riêng của đúng file này; mỗi lần tải hệ thống kiểm lại "
  + "quyền và SHA-256.";

export const COMMON_SEARCHABLE_NOTE = SEARCHABLE + RAG_CLOSED + NO_DOWNLOAD;
export const COMMON_SEARCHABLE_DOWNLOAD_NOTE = SEARCHABLE + RAG_CLOSED + DOWNLOAD;
export const COMMON_RAG_NOTE = SEARCHABLE + RAG_OPEN + NO_DOWNLOAD;
export const COMMON_RAG_DOWNLOAD_NOTE = SEARCHABLE + RAG_OPEN + DOWNLOAD;

export const COMMON_INDEXED_NOTE = "Đã lập chỉ mục, chưa mở tìm kiếm: quyền, công bố hoặc index hiện tại không "
  + "cho phục vụ toàn văn của bản chung này; chưa có đoạn trích hay hỏi đáp.";

export const COMMON_METADATA_ONLY_NOTE = "Toàn văn chưa khả dụng: bản chung này hiện chỉ có metadata. Không có file "
  + "gốc được phép tải; chưa có tìm kiếm toàn văn, đoạn trích hay hỏi đáp cho bản này.";

export const COMMON_DOWNLOAD_NOTE = "File gốc dưới đây tải được theo quyền Admin đã ghi cho đúng file này; mỗi lần "
  + "tải hệ thống kiểm lại quyền và SHA-256. Toàn văn chưa khả dụng: chưa có tìm kiếm toàn văn, đoạn trích hay hỏi "
  + "đáp cho bản chung này.";

export const NO_DOWNLOADABLE_FILE = "Không có file gốc được phép tải.";
