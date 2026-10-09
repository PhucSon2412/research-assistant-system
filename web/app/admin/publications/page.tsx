"use client";

// Admin-only list of common copies approved from a review (M6b-1, M6b-2a), by
// state. Publishing lists a pending copy's METADATA for every account; its
// original opens only where the recorded rights allow download without
// conditions (M6b-2b1, decided by the API). A listed copy whose rights permit
// full-text search and provider processing can be indexed (M6b-2b2b); search and
// source chunks open for every account while those rights stay live (M6b-2b3a, the
// API's `search_open`), and RAG uses them as evidence (M6b-2b3b, `rag_open`). Revoking (pending or
// listed) is final and hides ordinary content from its commit; Admin history remains. Cleanup
// then removes the common object. The API enforces all of it.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, describeError, indexRequestError, publicationError, revocationError } from "../../../lib/api";
import {
  INDEX_ELIGIBILITY_TEXT, INDEX_EXTERNAL_NOTICE, OBJECT_OUTCOME_TEXT, ORIGINAL_DOWNLOAD_TEXT, PUBLICATION_STATE_TEXT,
  REVOCATION_REASONS, fieldList, isIndexRequestResult, makeIndexBody, makePublishBody, makeRevokeBody,
  type CommonCopy, type IndexRequestResult, type PublicationState,
} from "../../../lib/review";
import { ErrorText, Shell, useSession } from "../../components/session";

const yes = (value: boolean) => (value ? "có" : "không");
const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");
const STATES: PublicationState[] = ["pending", "listed", "revoked"];

function PublishForm({ copy, onDone }: { copy: CommonCopy; onDone: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const body = makePublishBody(confirmed, copy.file.checksum_sha256);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!body || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ item: CommonCopy }>(`/admin/common-copies/${encodeURIComponent(copy.id)}/publish`,
        { method: "POST", body });
      if (!result?.item || result.item.id !== copy.id) throw new Error("publication response incomplete");
      onDone();
    } catch (err) {
      setError(publicationError(err));
      setConfirmed(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} style={{ background: "#eef6ee", padding: "0.5rem" }}>
      <ErrorText message={error} />
      {copy.duplicate_published_item_id && <p><strong>Đã có bản chung đã công bố với cùng SHA-256</strong>{" "}
        (<Link href={`/items/${copy.duplicate_published_item_id}`}>xem</Link>); API sẽ từ chối công bố bản này.
        Không tự hợp nhất: thu hồi một bản nếu cần.</p>}
      <label>
        <input type="checkbox" checked={confirmed} disabled={busy} onChange={(e) => setConfirmed(e.target.checked)} />{" "}
        Tôi xác nhận công bố <strong>metadata</strong> (tiêu đề, version, URL nguồn) của bản này cho mọi tài khoản.
        Sau khi công bố, file gốc chỉ tải được nếu quyền đã ghi cho phép tải và không kèm điều kiện (API quyết
        định ở mỗi lần tải). Công bố <strong>không</strong> mở toàn văn: tìm kiếm và đoạn trích chỉ mở sau khi lập
        chỉ mục theo quyền đã ghi (hỏi đáp dùng cùng quyền); hệ thống đọc lại object chung và kiểm SHA-256 trước
        khi công bố.
      </label>{" "}
      <button type="submit" disabled={!body || busy}>Công bố metadata</button>
    </form>
  );
}

function IndexForm({ copy, onDone }: { copy: CommonCopy; onDone: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const body = makeIndexBody(confirmed);
  const index = copy.index;

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    // The ref blocks a second submit in the same tick, before `busy` re-renders.
    if (!body || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError(null);
    let accepted = false;
    try {
      const result = await api<IndexRequestResult>(
        `/admin/common-copies/${encodeURIComponent(copy.id)}/index`, { method: "POST", body });
      if (!isIndexRequestResult(result, copy.id)) throw new Error("index response incomplete");
      accepted = true;
    } catch (err) {
      setError(indexRequestError(err));
    } finally {
      sending.current = false;
      setBusy(false);
      setConfirmed(false);
    }
    // A durable job is not indexed text; refresh shows its current state. A failed
    // refresh is reported by the list itself, never as a failed POST.
    if (accepted) onDone();
  }

  return (
    <div style={{ background: "#eef0f8", padding: "0.5rem", marginTop: "0.5rem" }}>
      <p>Lập chỉ mục: {INDEX_ELIGIBILITY_TEXT[index.eligibility] ?? index.eligibility}
        {index.job && <> · job {index.job.state}{index.job.last_error_code && ` (${index.job.last_error_code})`}
          {" "}· {index.job.attempt_count} lần thử</>}
        {!index.provider_ready && " · provider chưa bật (chưa có key hoặc chưa xác minh giá): job dừng ở chờ index"}
        {" "}· tìm kiếm toàn văn: {index.search_open ? "đang mở" : "chưa mở"} · hỏi đáp: {index.rag_open ? "đang mở" : "chưa mở"}</p>
      {index.note && <p><strong>{index.note}</strong></p>}
      <ErrorText message={error} />
      {index.eligibility === "indexable" && copy.content_status !== "indexed" && (
        <form onSubmit={submit}>
          <label>
            <input type="checkbox" checked={confirmed} disabled={busy} onChange={(e) => setConfirmed(e.target.checked)} />{" "}
            Tôi xác nhận lập chỉ mục bản này. {INDEX_EXTERNAL_NOTICE}
          </label>{" "}
          <button type="submit" disabled={!body || busy}>{index.job ? "Chạy lại / xem job" : "Lập chỉ mục"}</button>
        </form>
      )}
    </div>
  );
}

function RevokeForm({ copy, onDone }: { copy: CommonCopy; onDone: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const body = makeRevokeBody(confirmed, reason);
  const pending = copy.publication_state === "pending";

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!body || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ item: CommonCopy; cancelled_jobs: number }>(
        `/admin/common-copies/${encodeURIComponent(copy.id)}/revoke`, { method: "POST", body });
      if (!result?.item || result.item.id !== copy.id || typeof result.cancelled_jobs !== "number") {
        throw new Error("revocation response incomplete");
      }
      onDone();
    } catch (err) {
      setError(revocationError(err));
      setConfirmed(false);
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} style={{ background: "#fbeeee", padding: "0.5rem", marginTop: "0.5rem" }}>
      <ErrorText message={error} />
      <label>Lý do{" "}
        <select value={reason} disabled={busy} onChange={(e) => setReason(e.target.value)} required>
          <option value="">— chọn —</option>
          {Object.entries(REVOCATION_REASONS).map(([code, label]) => <option key={code} value={code}>{label}</option>)}
        </select>
      </label>{" "}
      <label>
        <input type="checkbox" checked={confirmed} disabled={busy} onChange={(e) => setConfirmed(e.target.checked)} />{" "}
        Tôi xác nhận {pending ? "hủy bản chờ này" : "thu hồi bản đã công bố này"}. Thao tác là cuối cùng: từ lúc
        commit, các trang nội dung thông thường chặn bản này; Admin còn xem lịch sử quyết định.
        Trong hạn cleanup, hệ thống xóa object chung và metadata (tiêu đề,
        URL nguồn, tên file, nguồn/giấy phép/bằng chứng/điều kiện); trang này không giữ lại trường nào. Bản riêng
        của User không đổi.
      </label>{" "}
      <button type="submit" disabled={!body || busy}>{pending ? "Hủy bản chờ" : "Thu hồi"}</button>
    </form>
  );
}

export default function CommonCopiesPage() {
  const user = useSession();
  const [state, setState] = useState<PublicationState>("pending");
  const [data, setData] = useState<{ items: CommonCopy[]; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<{ items: CommonCopy[]; total: number }>(`/admin/common-copies?state=${state}&limit=100`)
      .then((d) => { setData(d); setError(null); })
      .catch((err) => setError(describeError(err)));
  }, [state]);

  useEffect(() => {
    if (user) load();
  }, [user, load]);

  return (
    <Shell user={user}>
      <p><Link href="/admin">← Quản trị</Link></p>
      <h1>Bản chung từ yêu cầu duyệt</h1>
      <p>
        Quyền dưới đây do Admin ghi nhận cho đúng file/checksum. <strong>Đã ghi nhận quyền chưa phải là đã công
        bố</strong>, và <strong>đã công bố (listed) chưa phải là có toàn văn (indexed)</strong>: công bố chỉ cho mọi
        tài khoản thấy metadata, và file gốc chỉ tải được khi quyền đã ghi cho phép tải mà không kèm điều kiện. Bản
        đã công bố có thể được lập chỉ mục khi quyền cho tìm kiếm toàn văn và gửi qua provider; khi đã lập chỉ mục,
        <strong> mọi tài khoản tìm kiếm và mở được đoạn trích</strong> theo quyền còn hiệu lực (kiểm lại ở mỗi request).
        <strong> Hỏi đáp (RAG)</strong> dùng đoạn của bản đó làm bằng chứng khi quyền còn cho gửi qua provider
        (đoạn được chọn được gửi tới provider khi User hỏi), và quyền tìm kiếm/hỏi đáp không mở tải file gốc.
      </p>
      <p style={{ display: "flex", gap: "0.5rem" }}>
        {STATES.map((s) => <button key={s} onClick={() => setState(s)} disabled={s === state}>
          {PUBLICATION_STATE_TEXT[s]}</button>)}
      </p>
      <ErrorText message={error} />
      {data && <p>Tổng: {data.total}</p>}
      {data && data.items.length === 0 && <p>Không có bản nào.</p>}
      {data && data.items.map((p) => <section key={p.id} id={p.id} style={{ borderTop: "1px solid #ddd" }}>
        <h2>{p.title ?? (p.rights.metadata_removed_at ? `(metadata đã xóa) ${p.id}` : p.id)}</h2>
        <p>{PUBLICATION_STATE_TEXT[p.publication_state]} · tạo {fmt(p.created_at)} · từ{" "}
          <Link href={`/admin/reviews/${encodeURIComponent(p.rights.origin_review_request_id)}`}>yêu cầu duyệt</Link>
          {p.publication_state === "listed" && <> · <Link href={`/items/${p.id}`}>trang thư viện</Link></>}</p>
        <p>{p.note}</p>
        <ul>
          <li>File: {p.file.filename ?? "(không tên)"} · {p.file.verified_mime} · {p.file.byte_size} byte ·
            SHA-256 <code>{p.file.checksum_sha256}</code></li>
          <li>Nguồn đã kiểm: <span style={{ whiteSpace: "pre-wrap" }}>{p.rights.verified_source ?? "(đã xóa)"}</span>
            {p.rights.verified_source_url && ` · ${p.rights.verified_source_url}`}</li>
          {p.rights.license_url && <li>Giấy phép: {p.rights.license_url}</li>}
          {p.rights.evidence_note && <li>Bằng chứng: <span style={{ whiteSpace: "pre-wrap" }}>{p.rights.evidence_note}</span></li>}
          <li>Kiểm ngày {p.rights.checked_on} bởi {p.rights.reviewer_email}</li>
          <li>Phạm vi đã ghi (mọi tài khoản của hệ thống): tải file gốc {yes(p.rights.scope.original_download)} ·
            tìm kiếm toàn văn {yes(p.rights.scope.fulltext_search)} · gửi qua provider{" "}
            {yes(p.rights.scope.external_processing)}
            {p.rights.scope.conditions && ` · điều kiện: ${p.rights.scope.conditions}`}. Lập chỉ mục cần cả tìm
            kiếm toàn văn và gửi qua provider; tìm kiếm mở theo quyền tìm kiếm toàn văn, rerank chỉ nhận đoạn khi quyền
            cho gửi qua provider; hỏi đáp chỉ dùng đoạn khi quyền cho cả tìm kiếm toàn văn và gửi qua provider.</li>
          <li>Tải file gốc qua thư viện: {ORIGINAL_DOWNLOAD_TEXT[p.original_download] ?? p.original_download}</li>
          {p.publication && <li>Công bố {fmt(p.publication.published_at)} (object đã kiểm:{" "}
            {p.publication.verified_byte_size} byte){p.publication.revoked_at && ` · thu hồi ${fmt(p.publication.revoked_at)}`}</li>}
          {p.tombstone && <li>Thu hồi {fmt(p.tombstone.recorded_at)} từ trạng thái{" "}
            {PUBLICATION_STATE_TEXT[p.tombstone.prior_publication_state]} · lý do{" "}
            {REVOCATION_REASONS[p.tombstone.reason_code] ?? p.tombstone.reason_code} · cleanup{" "}
            {p.tombstone.purged_at
              ? `xong ${fmt(p.tombstone.purged_at)} (${OBJECT_OUTCOME_TEXT[p.tombstone.object_outcome ?? ""] ?? p.tombstone.object_outcome})`
              : `chưa xong, hạn ${fmt(p.tombstone.purge_due_at)}, ${p.tombstone.cleanup_attempts} lần thử`}
            {p.tombstone.last_cleanup_error && ` · lỗi gần nhất: ${p.tombstone.last_cleanup_error}`}
            {p.tombstone.overdue && <strong> · QUÁ HẠN</strong>}</li>}
          {p.tombstone?.purged_at && <li>Metadata đã xóa: {fieldList(p.tombstone.removed_fields)} · giữ theo căn
            cứ: {fieldList(p.tombstone.retained_fields)}</li>}
          {p.retention_bases.map((b) => <li key={b.field}>Căn cứ giữ {fieldList([b.field])}: {b.policy_source} ·
            phạm vi {b.scope} · lý do {b.reason} · xác nhận {b.confirmed_by_email} {fmt(b.recorded_at)}</li>)}
        </ul>
        {p.publication_state === "pending" && <PublishForm copy={p} onDone={load} />}
        {p.publication_state === "listed" && <IndexForm copy={p} onDone={load} />}
        {p.publication_state !== "revoked" && <RevokeForm copy={p} onDone={load} />}
      </section>)}
    </Shell>
  );
}
