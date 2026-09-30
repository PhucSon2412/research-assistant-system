export function makeRagBody(scope: string, itemId: string, question: string,
                            mode: "keyword" | "hybrid") {
  const selected = scope === "item" ? { item_id: itemId }
    : scope === "all" ? { scope: "all" } : { collection_id: scope };
  return { ...selected, question, mode };
}

// What RAG may use (M6b-2b3b): the caller's own indexed private papers and the common copies whose live rights
// permit full-text search and provider processing. The API decides on every request; these texts describe it.
export const RAG_NOTICE = "Câu hỏi và các đoạn được chọn làm bằng chứng sẽ gửi qua OpenRouter/provider để tạo câu "
  + "trả lời. Phạm vi gồm paper riêng đã index của bạn và các bản chung mà quyền Admin đã ghi cho phép tìm kiếm toàn "
  + "văn và xử lý qua provider; collection chỉ thu hẹp phạm vi, không cấp quyền. Đoạn của bản chung không được phép "
  + "gửi provider không được dùng làm bằng chứng; danh mục tài liệu tham khảo cũng không.";

export const RAG_ALL_SCOPE_OPTION = "Tất cả paper riêng đã index của tôi và bản chung được phép hỏi đáp";

export function ragScopeText(kind: "item" | "collection" | "all", collectionName?: string): string {
  if (kind === "item") return "paper này";
  if (kind === "all") return "tất cả paper riêng đã index của bạn và các bản chung được phép hỏi đáp";
  return `collection ${collectionName ?? ""}`.trim() + " (chỉ các paper được phép trong đó)";
}

export type RagReason = "comparison_requires_multiple_papers" | "keyword_query_without_terms"
  | "evidence_not_permitted_for_provider";

const REASON_TEXT: Record<RagReason, string> = {
  comparison_requires_multiple_papers: "Chưa có nguồn hợp lệ từ đúng hai paper để so sánh.",
  keyword_query_without_terms: "Câu hỏi chỉ gồm từ phổ biến nên chế độ từ khóa không có thuật ngữ để tìm; hãy "
    + "thêm thuật ngữ cụ thể.",
  evidence_not_permitted_for_provider: "Đoạn tìm thấy thuộc bản chung mà quyền không cho gửi tới provider, nên "
    + "không được dùng làm bằng chứng.",
};

export function ragReasonText(reason: string | undefined): string | null {
  return reason !== undefined && Object.hasOwn(REASON_TEXT, reason) ? REASON_TEXT[reason as RagReason] : null;
}

export function ragWithheldText(withheld: number | undefined): string | null {
  return typeof withheld === "number" && withheld > 0
    ? `${withheld} đoạn tìm thấy thuộc bản chung không được phép gửi tới provider nên không được dùng làm bằng chứng.`
    : null;
}
