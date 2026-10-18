// M6e: exact-byte duplicate inside the owner's private library. The API recomputes
// `existing_copy` on every read with its own ACL; it is navigation only, never a
// canonical link, and the target page re-checks permission like any other item.
export type ExistingCopy = { item_id: string; title: string | null; content_status: string };

export const DUPLICATE_KEPT =
  "Lần tải này vẫn là một item riêng: file gốc mới được giữ, nhưng không được trích xuất, lập chỉ mục hay gửi tới nhà cung cấp; "
  + "hệ thống không liên kết, hợp nhất hoặc dùng lại nội dung giữa hai item.";
export const DUPLICATE_NO_TARGET =
  "File này trùng chính xác (cùng SHA-256) với một bản trước trong kho riêng của bạn, nhưng hiện không còn bản hợp lệ để mở "
  + "(có thể đã bị xóa hoặc xử lý không thành công). " + DUPLICATE_KEPT;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function existingCopyHref(copy: ExistingCopy | null | undefined): string | null {
  return copy && typeof copy.item_id === "string" && UUID.test(copy.item_id)
    ? `/items/${encodeURIComponent(copy.item_id)}` : null;
}

export function duplicateNotice(copy: ExistingCopy | null | undefined): {
  text: string; href: string | null; label: string | null;
} {
  const href = existingCopyHref(copy);
  if (!copy || !href) return { text: DUPLICATE_NO_TARGET, href: null, label: null };
  const status = copy.content_status === "indexed" ? "đã lập chỉ mục" : "đang xử lý hoặc chờ lập chỉ mục";
  return {
    text: "File này trùng chính xác (cùng SHA-256) với một bản đang có trong kho riêng của bạn. " + DUPLICATE_KEPT,
    href,
    label: `Mở bản hiện có: ${copy.title ?? "(chưa có tiêu đề)"} (${status})`,
  };
}
