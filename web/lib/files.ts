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

// A common item that is not indexed: which note to show depends only on
// whether the API listed a downloadable file.
export function commonItemNote(files: readonly ItemFile[]): string {
  return files.length > 0 ? COMMON_DOWNLOAD_NOTE : COMMON_METADATA_ONLY_NOTE;
}

export const COMMON_METADATA_ONLY_NOTE = "Toàn văn chưa khả dụng: bản chung này hiện chỉ có metadata. Không có file "
  + "gốc được phép tải; chưa có tìm kiếm toàn văn, đoạn trích hay hỏi đáp cho bản này.";

export const COMMON_DOWNLOAD_NOTE = "File gốc dưới đây tải được theo quyền Admin đã ghi cho đúng file này; mỗi lần "
  + "tải hệ thống kiểm lại quyền và SHA-256. Toàn văn chưa khả dụng: chưa có tìm kiếm toàn văn, đoạn trích hay hỏi "
  + "đáp cho bản chung này.";

export const NO_DOWNLOADABLE_FILE = "Không có file gốc được phép tải.";
