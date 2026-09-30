"use client";

// Admin review of one request: the metadata needed to judge source/rights, the
// single original it names, and the decision. Approving (M6b-1) records the
// Admin's own rights findings and creates a separate common copy that stays
// unpublished; nothing here publishes, indexes or sends the paper anywhere.

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../../../lib/api";
import {
  CONDITIONS_BLOCK_NOTE, EMPTY_APPROVE_FORM, REJECT_REASONS, STATUS_TEXT, closeReasonText, makeApproveBody,
  reviewDownloadHref,
  type AdminReviewRequest, type ApproveForm,
} from "../../../../lib/review";
import { ErrorText, Shell, useSession } from "../../../components/session";

const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");
const wide = { width: "100%" };

function ApproveSection({ request, onDone }: { request: AdminReviewRequest; onDone: () => void }) {
  const checksum = request.file?.checksum_sha256 ?? "";
  const [form, setForm] = useState<ApproveForm>({ ...EMPTY_APPROVE_FORM, checksum });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof ApproveForm>(key: K, value: ApproveForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const body = makeApproveBody(form);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/admin/review-requests/${encodeURIComponent(request.id)}/approve`, { method: "POST", body });
      onDone();
    } catch (err) {
      setError(describeError(err));
      set("confirmed", false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} style={{ display: "grid", gap: "0.5rem", maxWidth: "40rem" }}>
      <h2>Phê duyệt và ghi nhận quyền</h2>
      <p style={{ background: "#fff6e0", padding: "0.5rem" }}>
        Chỉ điền điều <strong>bạn đã tự kiểm</strong> cho đúng file có SHA-256 bên dưới. Lời khai của User, việc
        User upload, hay việc paper đọc miễn phí trên arXiv/Hugging Face <strong>không</strong> tự cấp quyền phân
        phối. Phê duyệt tạo một <strong>bản chung riêng đang chờ công bố</strong>: User khác chưa thấy bản này, chưa
        có index và không gửi gì tới OpenRouter/provider. Bản riêng và collection của User không đổi. Phê duyệt
        cũng kết thúc quyền xem tạm của yêu cầu này.
      </p>
      <ErrorText message={error} />
      <label>Tiêu đề bản chung
        <br /><input value={form.title} onChange={(e) => set("title", e.target.value)} required maxLength={1000}
          style={wide} />
      </label>
      <label>Nguồn file đã kiểm (nơi có đúng bản này)
        <br /><textarea value={form.source} onChange={(e) => set("source", e.target.value)} required maxLength={2000}
          rows={2} style={wide} />
      </label>
      <label>URL nguồn file đã kiểm (không bắt buộc)
        <br /><input type="url" value={form.sourceUrl} onChange={(e) => set("sourceUrl", e.target.value)}
          maxLength={2048} style={wide} />
      </label>
      <label>URL giấy phép đã kiểm
        <br /><input type="url" value={form.licenseUrl} onChange={(e) => set("licenseUrl", e.target.value)}
          maxLength={2048} style={wide} />
      </label>
      <label>Bằng chứng khác đã kiểm (thư cho phép, điều khoản…)
        <br /><textarea value={form.evidence} onChange={(e) => set("evidence", e.target.value)} maxLength={2000}
          rows={2} style={wide} />
      </label>
      <small>Cần ít nhất URL giấy phép hoặc ghi chú bằng chứng.</small>
      <label>Ngày kiểm
        <br /><input type="date" value={form.checkedOn} onChange={(e) => set("checkedOn", e.target.value)} required />
      </label>
      <fieldset>
        <legend>Phạm vi sử dụng được phép (cho mọi tài khoản của hệ thống)</legend>
        <label><input type="checkbox" checked={form.originalDownload}
          onChange={(e) => set("originalDownload", e.target.checked)} /> Cho User tải file gốc</label><br />
        <label><input type="checkbox" checked={form.fulltextSearch}
          onChange={(e) => set("fulltextSearch", e.target.checked)} /> Cho index/tìm kiếm toàn văn và xem chunk</label><br />
        <label><input type="checkbox" checked={form.externalProcessing}
          onChange={(e) => set("externalProcessing", e.target.checked)} /> Cho gửi text/chunk qua OpenRouter/provider
          (embedding, rerank, RAG)</label><br />
        <label>Điều kiện kèm theo (không bắt buộc)
          <br /><input value={form.conditions} onChange={(e) => set("conditions", e.target.value)} maxLength={1000}
            style={wide} />
        </label>
        <br /><small>{CONDITIONS_BLOCK_NOTE}</small>
      </fieldset>
      <label>
        <input type="checkbox" checked={form.confirmed} onChange={(e) => set("confirmed", e.target.checked)} />
        {" "}Tôi đã tự kiểm quyền cho đúng file có SHA-256 <code>{checksum}</code>; thông tin trên là kết quả kiểm của
        tôi, không chép từ lời khai của User.
      </label>
      <button type="submit" disabled={busy || !body}>{busy ? "Đang ghi…" : "Phê duyệt (chờ công bố)"}</button>
    </form>
  );
}

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
        <p>{request.pending_note}</p>
        {request.pending_publication_id && <p role="status">
          Quyền đã được ghi nhận nhưng <strong>chưa công bố</strong>.{" "}
          <Link href={`/admin/publications#${encodeURIComponent(request.pending_publication_id)}`}>
            Xem bản chung chờ công bố</Link>
        </p>}
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
          {request.status === "open" && <ApproveSection key={request.id} request={request} onDone={load} />}
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
