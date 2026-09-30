export const DEFAULT_RERANK = false;

export const SEARCH_NOTICE =
  "Tìm trong paper riêng của bạn đã index và các bản chung mà quyền Admin đã ghi cho phép tìm kiếm toàn văn, kể cả khi chọn collection (collection chỉ thu hẹp phạm vi). Paper chung chỉ có metadata hoặc đã thu hồi không vào tìm kiếm. Chế độ hybrid gửi câu tìm kiếm tới OpenRouter/provider để tạo embedding. Nếu bật rerank, câu tìm kiếm và các candidate chunks được phép sẽ được gửi tới OpenRouter/provider; đoạn của bản chung chỉ được gửi khi quyền cho phép xử lý qua provider. Hỏi đáp (RAG) dùng cùng phạm vi, nhưng chỉ lấy đoạn của bản chung làm bằng chứng khi quyền cũng cho phép xử lý qua provider.";

// Hit/viewer label: common copy vs. the caller's own private paper (the API never returns another user's).
export function sourceKindLabel(visibility: string | undefined): string {
  return visibility === "common" ? "Bản chung" : "Paper riêng của bạn";
}

export function makeSearchBody(
  query: string,
  mode: "keyword" | "hybrid",
  rerank: boolean,
  itemId: string,
  collectionId = "",
) {
  return { query, mode, rerank, item_id: itemId.trim() || null, collection_id: collectionId.trim() || null };
}
