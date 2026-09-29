// M5b-1 citation links: display helpers only; the API decides visibility.

export type CitationTarget = {
  discovery_paper_id: string; source: string; arxiv_id: string; title: string;
  original_url: string; cited_version_url: string | null; latest_metadata_version: string | null;
  content_status: "metadata_only"; label: "metadata-only";
};
export type CitationLink = {
  status: string; linked: boolean; relation: "cites"; method: string | null; id_form: string | null;
  arxiv_id: string | null; cited_version: string | null; matcher_version: string;
  id_span: { start: number; end: number; offset_unit: string } | null;
  target: CitationTarget | null;
};
export type Reference = {
  id: string; ordinal: number; version_id: string; source_file_id: string; raw_citation: string;
  locator_kind: "markdown" | "pdf"; page: number | null; start_offset: number; end_offset: number;
  evidence_kind: "bibliography_entry"; citation_link: CitationLink;
};

export const RELATION_NOTE =
  "Liên kết citation chỉ có nghĩa “paper này trích dẫn paper kia” (liệt kê trong danh mục tham khảo); " +
  "không biểu thị đồng ý hay xác nhận nội dung của paper được trích.";

const STATUS: Record<string, string> = {
  linked: "Đã liên kết theo arXiv ID chính xác",
  no_discovery_record: "Có arXiv ID nhưng chưa có metadata trong trang khám phá",
  discovery_record_deleted: "Bản ghi arXiv đã bị xóa khỏi nguồn (OAI tombstone); không có đích khả dụng",
  no_arxiv_id: "Chưa liên kết: không có arXiv ID",
  bare_identifier: "Chưa liên kết: số giống arXiv ID nhưng thiếu ngữ cảnh arXiv",
  invalid_arxiv_id: "Chưa liên kết: arXiv ID sai định dạng hoặc bị trích xuất lỗi",
  multiple_arxiv_ids: "Chưa liên kết: có nhiều arXiv ID",
  conflicting_versions: "Chưa liên kết: version được trích mâu thuẫn",
  not_reconciled: "Đang chờ đối chiếu",
};

export function citationStatusText(status: string): string {
  return STATUS[status] ?? status;
}

export function referenceLocator(ref: Pick<Reference, "locator_kind" | "page" | "start_offset" | "end_offset">): string {
  const span = `offset ${ref.start_offset}–${ref.end_offset}`;
  return ref.locator_kind === "pdf" ? `trang ${ref.page} · ${span} trong text trang` : `${span} trong Markdown gốc`;
}
