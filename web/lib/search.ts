export const DEFAULT_RERANK = false;

export const SEARCH_NOTICE =
  "Chỉ tìm nội dung paper riêng của bạn đã index. Chế độ hybrid gửi câu tìm kiếm tới OpenRouter/provider để tạo embedding. Nếu bật rerank, câu tìm kiếm và các candidate chunks được phép sẽ được gửi tới OpenRouter/provider.";

export function makeSearchBody(
  query: string,
  mode: "keyword" | "hybrid",
  rerank: boolean,
  itemId: string,
) {
  return { query, mode, rerank, item_id: itemId.trim() || null };
}
