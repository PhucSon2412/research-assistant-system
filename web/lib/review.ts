// M6a review requests, M6b-1 approval and M6b-2a publish/revoke: shapes and pure
// helpers shared by the owner and Admin screens. The API decides who may submit,
// withdraw, reject, approve, publish, revoke or open a file. Approval creates a
// pending common copy; publishing lists its metadata only (full text stays closed).

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
  fulltext_available: false; created_at: string; note: string;
  version: { id: string; source_url: string | null; received_at: string };
  file: { id: string; filename: string | null; verified_mime: string; byte_size: number; page_count: number | null;
    checksum_sha256: string };
  rights: { id: string; origin_kind: string; origin_review_request_id: string; file_checksum_sha256: string;
    verified_source: string; verified_source_url: string | null; license_url: string | null;
    evidence_note: string | null; checked_on: string; recorded_at: string; reviewer_email: string;
    scope: RightsScope; revoked_at?: string | null; revocation_reason_code?: string | null };
};

// Any reviewed common copy, as /admin/common-copies returns it (M6b-2a).
export type CommonCopy = PendingPublication & {
  publication: { published_at: string; revoked_at: string | null; verified_byte_size: number } | null;
  duplicate_published_item_id?: string | null;
  tombstone: { id: string; reason_code: string; prior_publication_state: "pending" | "listed"; recorded_at: string;
    purge_due_at: string; purged_at: string | null; object_outcome: string | null; cleanup_attempts: number;
    last_cleanup_error: string | null; overdue: boolean } | null;
};

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

// Revoking needs a listed reason code and the unticked box; no free text is sent.
export function makeRevokeBody(confirmed: boolean, reason: string) {
  if (!confirmed || !Object.hasOwn(REVOCATION_REASONS, reason)) return null;
  return { confirm_revoke: true as const, reason_code: reason };
}

// The library item page shows this for any common item that is not indexed.
export const COMMON_METADATA_ONLY_NOTE = "Toàn văn chưa khả dụng: bản chung này hiện chỉ có metadata. Chưa có file "
  + "gốc, tìm kiếm toàn văn, đoạn trích hay hỏi đáp cho bản này.";

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
