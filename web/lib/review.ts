// M6a review requests, M6b-1 approval and M6b-2a publish/revoke: shapes and pure
// helpers shared by the owner and Admin screens. The API decides who may submit,
// withdraw, reject, approve, publish, revoke or open a file. Approval creates a
// pending common copy; publishing lists its metadata, and its original opens only
// where the recorded rights allow (M6b-2b1); full text stays closed.

export type ReviewStatus = "open" | "withdrawn" | "rejected" | "approved";

export type ReviewTarget = {
  item: { id: string; title: string | null };
  version: { id: string; version_label: string | null; source_identifier: string | null;
    source_url: string | null; received_at: string };
  file: { id: string; filename: string | null; verified_mime: string; byte_size: number;
    page_count: number | null; checksum_sha256: string };
};
export type Declaration = { source: string; source_url: string | null; rights: string;
  license_url: string | null; verified: false; note: string };

type Closure = { id: string; status: ReviewStatus; submitted_at: string; closed_at: string | null;
  close_reason_code: string | null };

export type OwnReviewRequest = Closure & ReviewTarget & { declaration: Declaration;
  rejection_note: string | null; publication: false; pending_note: string };

// Closed or no-longer-granted requests carry no item/file/declaration fields.
export type AdminReviewRequest = Closure & { review_available: boolean; publication: false; pending_note: string;
  pending_publication_id?: string }
  & Partial<ReviewTarget & { requester_email: string; declaration: Declaration; original_url: string;
    rights_note: string }>;

export type RightsScope = { audience: "instance_users"; original_download: boolean; fulltext_search: boolean;
  external_processing: boolean; conditions: string | null };

export type PublicationState = "pending" | "listed" | "revoked";

// A common copy with Admin-recorded rights that is not published (M6b-1).
export type PendingPublication = {
  id: string; title: string | null; publication_state: PublicationState; published: boolean; content_status: string;
  fulltext_available: false; original_download: OriginalDownloadStatus; created_at: string; note: string;
  version: { id: string; source_url: string | null; received_at: string };
  file: { id: string; filename: string | null; verified_mime: string; byte_size: number; page_count: number | null;
    checksum_sha256: string };
  // verified_source is null once cleanup removed a revoked copy's metadata (M6b-2a).
  rights: { id: string; origin_kind: string; origin_review_request_id: string; file_checksum_sha256: string;
    verified_source: string | null; verified_source_url: string | null; license_url: string | null;
    evidence_note: string | null; checked_on: string; recorded_at: string; reviewer_email: string;
    scope: RightsScope; revoked_at?: string | null; revocation_reason_code?: string | null;
    metadata_removed_at?: string | null };
};

// Index status of a reviewed copy (M6b-2b2b). IDs and codes only; `search_open` stays false in this step.
export type IndexEligibility = "indexable" | "revoked" | "not_published" | "rights_not_current"
  | "audience_not_supported" | "fulltext_not_permitted" | "external_processing_not_permitted"
  | "blocked_by_conditions" | "unsupported_file_kind";

export type CopyIndexStatus = {
  eligibility: IndexEligibility; content_status: string; search_open: false; note: string | null;
  provider_ready: boolean;
  active_generation: { id: string; model_id: string; dimensions: number | null; index_version: string;
    expected_chunks: number; activated_at: string | null } | null;
  job: { id: string; state: string; attempt_count: number; last_error_code: string | null; warnings: string[];
    cancel_requested: boolean; created_at: string; extraction_finished_at: string | null;
    finished_at: string | null } | null;
};

// Any reviewed common copy, as /admin/common-copies returns it (M6b-2a).
export type CommonCopy = PendingPublication & {
  index: CopyIndexStatus;
  publication: { published_at: string; revoked_at: string | null; verified_byte_size: number } | null;
  duplicate_published_item_id?: string | null;
  tombstone: { id: string; reason_code: string; prior_publication_state: "pending" | "listed"; recorded_at: string;
    purge_due_at: string; purged_at: string | null; object_outcome: string | null; cleanup_attempts: number;
    last_cleanup_error: string | null; overdue: boolean; removed_fields: string[] | null;
    retained_fields: string[] | null } | null;
  retention_bases: { field: string; policy_source: string; scope: string; reason: string;
    confirmed_by_email: string; recorded_at: string }[];
};

// Metadata cleanup removes from a revoked copy unless a per-field basis keeps it.
export const REVOKED_METADATA_FIELDS: Record<string, string> = {
  title: "tiêu đề",
  source_url: "URL nguồn",
  original_filename: "tên file",
  verified_source: "nguồn đã kiểm",
  license_url: "URL giấy phép",
  evidence_note: "ghi chú bằng chứng",
  scope_conditions: "điều kiện sử dụng",
};

export function fieldList(fields: string[] | null): string {
  if (!fields || fields.length === 0) return "không";
  return fields.map((f) => REVOKED_METADATA_FIELDS[f] ?? f).join(", ");
}

export const PUBLICATION_STATE_TEXT: Record<PublicationState, string> = {
  pending: "Chờ công bố",
  listed: "Đã công bố metadata (toàn văn chưa khả dụng)",
  revoked: "Đã thu hồi",
};

export const REVOCATION_REASONS: Record<string, string> = {
  rights_recorded_in_error: "Quyền đã ghi bị sai",
  rights_withdrawn: "Bên giữ quyền đã thu hồi",
  rights_expired: "Quyền hết hạn",
  wrong_file_or_version: "Sai file hoặc version",
  duplicate_copy: "Bản trùng",
  other: "Lý do khác",
};

export const OBJECT_OUTCOME_TEXT: Record<string, string> = {
  deleted: "đã xóa object chung",
  already_absent: "object chung đã không còn",
  shared_kept: "giữ object vì bản hợp lệ khác còn dùng",
  no_object: "không có object",
};

// Publishing names the exact bytes shown on the page; null until the unticked box is ticked.
export function makePublishBody(confirmed: boolean, checksum: string) {
  if (!confirmed || !/^[0-9a-f]{64}$/.test(checksum)) return null;
  return { confirm_publish: true as const, file_checksum_sha256: checksum };
}

// Indexing sends the copy's extracted text to OpenRouter for embedding; null until the unticked box is ticked.
export function makeIndexBody(confirmed: boolean) {
  return confirmed ? { confirm_external_processing: true as const } : null;
}

// Why the API would refuse to index a copy now (`index.eligibility`); the API decides, the page only explains.
export const INDEX_ELIGIBILITY_TEXT: Record<IndexEligibility, string> = {
  indexable: "lập chỉ mục được (quyền cho tìm kiếm toàn văn và gửi qua provider, không kèm điều kiện)",
  revoked: "không (đã thu hồi)",
  not_published: "chưa (bản chưa công bố)",
  rights_not_current: "không (quyền đã ghi không còn khớp trạng thái hiện tại: epoch, publication hoặc file)",
  audience_not_supported: "không (phạm vi người dùng chưa hỗ trợ)",
  fulltext_not_permitted: "không (quyền không cho tìm kiếm toàn văn: không tạo dữ liệu trích xuất)",
  external_processing_not_permitted: "không (quyền cho toàn văn nhưng không cho gửi qua provider: index vector hiện "
    + "tại không hoàn tất được, và hệ thống không tự bật tìm kiếm chỉ theo từ khóa)",
  blocked_by_conditions: "không (có điều kiện sử dụng hệ thống chưa thi hành được)",
  unsupported_file_kind: "không (định dạng file chưa hỗ trợ)",
};

export const INDEX_EXTERNAL_NOTICE = "Text trích xuất từ file chung (không phải file gốc) sẽ được gửi tới "
  + "OpenRouter/provider để embedding; reference list không được gửi làm bằng chứng. Đã lập chỉ mục vẫn chưa mở "
  + "tìm kiếm/hỏi đáp cho bản chung ở bước này.";

// Revoking needs a listed reason code and the unticked box; no free text is sent.
export function makeRevokeBody(confirmed: boolean, reason: string) {
  if (!confirmed || !Object.hasOwn(REVOCATION_REASONS, reason)) return null;
  return { confirm_revoke: true as const, reason_code: reason };
}

// scope_conditions is free text the system cannot enforce, so the API keeps the
// original closed while it is set (M6b-2b1). The approve form says so up front.
export const CONDITIONS_BLOCK_NOTE = "Hệ thống chưa thi hành được điều kiện dạng text: nếu nhập điều kiện, file gốc "
  + "của bản chung sẽ không mở tải cho ai, kể cả khi chọn “Cho User tải file gốc”.";

// What the API's download gate decides for a copy's file (`original_download`).
// "open" only when the download route's own SQL gate opens the file right now.
export type OriginalDownloadStatus = "open" | "not_permitted" | "blocked_by_conditions" | "not_published" | "revoked"
  | "rights_not_current";

export const ORIGINAL_DOWNLOAD_TEXT: Record<OriginalDownloadStatus, string> = {
  open: "mở cho mọi tài khoản (mỗi lần tải kiểm lại quyền và SHA-256)",
  not_permitted: "không mở (quyền đã ghi không cho tải)",
  blocked_by_conditions: "không mở (có điều kiện sử dụng hệ thống chưa thi hành được)",
  not_published: "chưa mở (bản chưa công bố)",
  revoked: "không mở (đã thu hồi)",
  rights_not_current: "không mở (quyền đã ghi không còn khớp trạng thái hiện tại: epoch, publication hoặc file)",
};

export type FileChoice = { id: string; version_id: string; filename: string | null };
export type VersionInfo = { id: string; version_label: string | null };

export const STATUS_TEXT: Record<ReviewStatus, string> = {
  open: "Đang chờ duyệt (chưa công bố)",
  withdrawn: "Đã rút",
  rejected: "Admin đã từ chối",
  approved: "Admin đã ghi nhận quyền (bản chung chờ công bố)",
};

export const REJECT_REASONS: Record<string, string> = {
  rights_not_established: "Chưa xác lập được quyền phân phối",
  insufficient_evidence: "Bằng chứng nguồn/quyền chưa đủ",
  wrong_file_or_version: "Sai file hoặc version",
  out_of_scope: "Ngoài phạm vi kho chung",
  other: "Lý do khác",
};

export function closeReasonText(code: string | null): string | null {
  if (!code) return null;
  if (code === "withdrawn_by_requester") return "User đã rút yêu cầu";
  if (code === "approved_by_admin") return "Admin đã phê duyệt; bản chung riêng đang chờ công bố";
  return REJECT_REASONS[code] ?? code;
}

// Every field is what the Admin checked; the form never starts from the User's declaration.
export type ApproveForm = { confirmed: boolean; checksum: string; title: string; source: string; sourceUrl: string;
  licenseUrl: string; evidence: string; checkedOn: string; originalDownload: boolean; fulltextSearch: boolean;
  externalProcessing: boolean; conditions: string };

export const EMPTY_APPROVE_FORM: Omit<ApproveForm, "checksum"> = {
  confirmed: false, title: "", source: "", sourceUrl: "", licenseUrl: "", evidence: "", checkedOn: "",
  originalDownload: false, fulltextSearch: false, externalProcessing: false, conditions: "",
};

// Returns null until the (unticked by default) confirmation is ticked and the
// required findings are present: a source, a check date and a license URL or evidence note.
export function makeApproveBody(form: ApproveForm) {
  const text = (value: string) => value.trim() || null;
  if (!form.confirmed || !/^[0-9a-f]{64}$/.test(form.checksum) || !text(form.title) || !text(form.source)
    || !/^\d{4}-\d{2}-\d{2}$/.test(form.checkedOn) || (!text(form.licenseUrl) && !text(form.evidence))
    || (form.externalProcessing && !form.fulltextSearch)) return null;
  return {
    confirm_verified: true as const,
    file_checksum_sha256: form.checksum,
    title: form.title.trim(),
    verified_source: form.source.trim(),
    verified_source_url: text(form.sourceUrl),
    license_url: text(form.licenseUrl),
    evidence_note: text(form.evidence),
    checked_on: form.checkedOn,
    scope: { audience: "instance_users" as const, original_download: form.originalDownload,
      fulltext_search: form.fulltextSearch, external_processing: form.externalProcessing,
      conditions: text(form.conditions) },
  };
}

export function fileChoiceLabel(file: FileChoice, versions: VersionInfo[]): string {
  const version = versions.find((v) => v.id === file.version_id);
  return `Version ${version?.version_label ?? "(không nhãn)"} · ${file.filename ?? "file gốc"} · ${file.id}`;
}

export type ReviewForm = { itemId: string; file: FileChoice | undefined; acknowledged: boolean;
  noticeVersion: string; source: string; sourceUrl: string; rights: string; licenseUrl: string };

// Returns null until the owner picked a file and ticked the (unticked by default) box.
export function makeReviewBody(form: ReviewForm) {
  if (!form.file || !form.acknowledged || !form.noticeVersion) return null;
  const url = (value: string) => value.trim() || null;
  return {
    acknowledged: true as const,
    notice_version: form.noticeVersion,
    item_id: form.itemId,
    version_id: form.file.version_id,
    source_file_id: form.file.id,
    declared_source: form.source.trim(),
    declared_source_url: url(form.sourceUrl),
    declared_rights: form.rights.trim(),
    declared_license_url: url(form.licenseUrl),
  };
}

// The API returns an API-relative path; the browser opens it through the
// same-origin streaming route, never the JSON proxy.
export function reviewDownloadHref(originalUrl: string): string | null {
  return /^\/admin\/review-requests\/[0-9a-f-]{36}\/files\/[0-9a-f-]{36}\/original$/.test(originalUrl)
    ? `/api${originalUrl}` : null;
}
