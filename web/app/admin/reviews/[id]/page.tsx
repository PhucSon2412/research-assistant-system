"use client";

// Admin review of one request: the metadata needed to judge source/rights and
// the single original it names. There is no approve/publish action in M6a.

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../../../lib/api";
import {
  REJECT_REASONS, STATUS_TEXT, closeReasonText, reviewDownloadHref, type AdminReviewRequest,
} from "../../../../lib/review";
import { ErrorText, Shell, useSession } from "../../../components/session";

const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");

export default function ReviewDetailPage() {
  const user = useSession();
  const { id } = useParams<{ id: string }>();
  const [request, setRequest] = useState<AdminReviewRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<{ request: AdminReviewRequest }>(`/admin/review-requests/${encodeURIComponent(id)}`)
      .then((d) => setRequest(d.request))
      .catch((err) => setError(describeError(err)));
  }, [id]);

  useEffect(() => {
    if (user) load();
  }, [user, load]);

  async function reject(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!reason) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/admin/review-requests/${encodeURIComponent(id)}/reject`, { method: "POST",
        body: { reason_code: reason, note: note.trim() || null } });
      load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  const href = request?.original_url ? reviewDownloadHref(request.original_url) : null;
  return (
    <Shell user={user}>
      <p><Link href="/admin/reviews">← Yêu cầu duyệt</Link></p>
      <ErrorText message={error} />
      {request && <>
        <h1>{request.item?.title ?? "Yêu cầu duyệt"}</h1>
        <p><strong>{STATUS_TEXT[request.status]}</strong> · gửi {fmt(request.submitted_at)}
          {request.closed_at && ` · đóng ${fmt(request.closed_at)}`}
          {closeReasonText(request.close_reason_code) && ` · ${closeReasonText(request.close_reason_code)}`}</p>
        <p>{request.pending_note} Chưa có thao tác phê duyệt hay công bố trong bản này.</p>
        {!request.review_available && <p role="status">
          Không còn quyền xem nội dung của yêu cầu này. Link tải cũ cũng bị chặn.
        </p>}
        {request.review_available && request.item && request.version && request.file && request.declaration && <>
          <h2>Đúng version/file được gửi</h2>
          <ul>
            <li>Người gửi: {request.requester_email}</li>
            <li>Item: {request.item.id}</li>
            <li>Version: {request.version.version_label ?? "(không nhãn)"} · {request.version.id}
              {request.version.source_identifier && ` · ${request.version.source_identifier}`}
              {" · nhận "}{fmt(request.version.received_at)}</li>
            <li>File: {request.file.filename ?? "(không tên)"} · {request.file.verified_mime} · {request.file.byte_size} byte
              {request.file.page_count ? ` · ${request.file.page_count} trang` : ""}</li>
            <li>SHA-256 lúc gửi: <code>{request.file.checksum_sha256}</code></li>
          </ul>
          {href && <p><a href={href}>Tải file gốc để duyệt</a> (mỗi lần tải được ghi audit; API kiểm lại checksum trước khi gửi)</p>}
          <h2>Thông tin User khai báo</h2>
          <p><em>{request.declaration.note}</em></p>
          <dl>
            <dt>Nguồn</dt><dd style={{ whiteSpace: "pre-wrap" }}>{request.declaration.source}</dd>
            {request.declaration.source_url && <><dt>URL nguồn</dt><dd>{request.declaration.source_url}</dd></>}
            <dt>Quyền phân phối</dt><dd style={{ whiteSpace: "pre-wrap" }}>{request.declaration.rights}</dd>
            {request.declaration.license_url && <><dt>URL giấy phép/bằng chứng</dt>
              <dd>{request.declaration.license_url}</dd></>}
          </dl>
        </>}
        {request.status === "open" && <form onSubmit={reject}
          style={{ display: "grid", gap: "0.5rem", maxWidth: "32rem" }}>
          <h2>Từ chối</h2>
          <label>Lý do
            <br /><select value={reason} onChange={(e) => setReason(e.target.value)} required>
              <option value="">— Chọn lý do —</option>
              {Object.entries(REJECT_REASONS).map(([code, text]) => <option key={code} value={code}>{text}</option>)}
            </select>
          </label>
          <label>Ghi chú ngắn cho User (không bắt buộc, không ghi vào audit)
            <br /><input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500}
              style={{ width: "100%" }} />
          </label>
          <button type="submit" disabled={busy || !reason}>{busy ? "Đang gửi…" : "Từ chối yêu cầu"}</button>
          <small>Từ chối kết thúc ngay quyền xem tạm của Admin với mọi lần mở mới.</small>
        </form>}
      </>}
    </Shell>
  );
}
