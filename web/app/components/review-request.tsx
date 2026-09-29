"use client";

// Owner side of M6a: ask an Admin to review one exact version/file. The API
// checks ownership, the pair and the notice; this form only reflects it.

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import {
  STATUS_TEXT, closeReasonText, fileChoiceLabel, makeReviewBody,
  type FileChoice, type OwnReviewRequest, type VersionInfo,
} from "../../lib/review";
import { ErrorText } from "./session";

const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");

export function ReviewRequests({ itemId, files, versions }: { itemId: string; files: FileChoice[];
  versions: VersionInfo[] }) {
  const [requests, setRequests] = useState<OwnReviewRequest[] | null>(null);
  const [notice, setNotice] = useState<{ version: string; text: string } | null>(null);
  const [fileId, setFileId] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [source, setSource] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [rights, setRights] = useState("");
  const [licenseUrl, setLicenseUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<{ requests: OwnReviewRequest[] }>(`/review-requests?item_id=${encodeURIComponent(itemId)}&limit=100`)
      .then((d) => setRequests(d.requests))
      .catch((err) => setError(describeError(err)));
  }, [itemId]);

  useEffect(() => {
    load();
    api<{ version: string; text: string }>("/review-requests/notice")
      .then(setNotice)
      .catch((err) => setError(describeError(err)));
  }, [load]);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const body = makeReviewBody({ itemId, file: files.find((f) => f.id === fileId), acknowledged,
      noticeVersion: notice?.version ?? "", source, sourceUrl, rights, licenseUrl });
    if (!body) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      await api("/review-requests", { method: "POST", body });
      setDone("Đã gửi yêu cầu duyệt. Paper vẫn chỉ ở kho riêng của bạn; đây chưa phải là công bố.");
      setSource(""); setSourceUrl(""); setRights(""); setLicenseUrl("");
      load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      // Consent is per request: untick after every attempt.
      setAcknowledged(false);
      setBusy(false);
    }
  }

  async function withdraw(id: string) {
    setError(null);
    setDone(null);
    try {
      await api(`/review-requests/${encodeURIComponent(id)}/withdraw`, { method: "POST" });
      setDone("Đã rút yêu cầu. Admin không mở được file này qua yêu cầu đó nữa.");
      load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <section>
      <h2>Đề nghị đưa vào kho chung (gửi Admin duyệt)</h2>
      <p>
        Gửi yêu cầu <strong>chưa phải là công bố</strong>: paper vẫn ở kho riêng, không vào kho chung, tìm kiếm
        hay hỏi đáp của người khác. Admin chỉ được xem tạm đúng version/file bạn chọn trong lúc yêu cầu còn mở.
      </p>
      <ErrorText message={error} />
      {done && <p role="status">{done}</p>}
      {requests && requests.length > 0 && <ul>{requests.map((r) => <li key={r.id} style={{ marginBottom: "0.6rem" }}>
        <strong>{STATUS_TEXT[r.status]}</strong> · gửi {fmt(r.submitted_at)}
        {r.closed_at && ` · đóng ${fmt(r.closed_at)}`}
        {closeReasonText(r.close_reason_code) && ` · ${closeReasonText(r.close_reason_code)}`}
        <br /><small>Version {r.version.version_label ?? "(không nhãn)"} ({r.version.id}) · File {r.file.filename ?? ""}
          {" "}({r.file.id}) · SHA-256 {r.file.checksum_sha256}</small>
        {r.rejection_note && <><br /><small>Ghi chú của Admin: {r.rejection_note}</small></>}
        {r.status === "open" && <> <button type="button" onClick={() => withdraw(r.id)}>Rút yêu cầu</button></>}
      </li>)}</ul>}
      {files.length === 0 ? <p>Item chưa có file gốc để gửi duyệt.</p> : (
        <form onSubmit={submit} style={{ display: "grid", gap: "0.5rem", maxWidth: "40rem" }}>
          <label>Version/file gửi duyệt
            <br /><select value={fileId} onChange={(e) => setFileId(e.target.value)} required style={{ width: "100%" }}>
              <option value="">— Chọn đúng version/file —</option>
              {files.map((f) => <option key={f.id} value={f.id}>{fileChoiceLabel(f, versions)}</option>)}
            </select>
          </label>
          <label>Nguồn của file (bạn lấy file từ đâu)
            <br /><textarea value={source} onChange={(e) => setSource(e.target.value)} required maxLength={2000}
              rows={2} style={{ width: "100%" }} />
          </label>
          <label>URL nguồn (không bắt buộc)
            <br /><input type="url" value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} maxLength={2048}
              style={{ width: "100%" }} />
          </label>
          <label>Quyền phân phối bạn biết (giấy phép, sự cho phép…)
            <br /><textarea value={rights} onChange={(e) => setRights(e.target.value)} required maxLength={2000}
              rows={2} style={{ width: "100%" }} />
          </label>
          <label>URL giấy phép/bằng chứng (không bắt buộc)
            <br /><input type="url" value={licenseUrl} onChange={(e) => setLicenseUrl(e.target.value)} maxLength={2048}
              style={{ width: "100%" }} />
          </label>
          <small>Thông tin trên là lời khai của bạn; Admin sẽ tự kiểm tra, việc bạn upload file không chứng minh quyền phân phối.</small>
          {notice && <p style={{ background: "#f4f4f4", padding: "0.5rem" }}>{notice.text}</p>}
          <label>
            <input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
            {" "}Tôi hiểu rằng gửi yêu cầu sẽ cấp cho Admin quyền xem tạm đúng version/file đã chọn để duyệt.
          </label>
          <button type="submit" disabled={busy || !acknowledged || !fileId || !notice}>
            {busy ? "Đang gửi…" : "Gửi yêu cầu duyệt"}
          </button>
        </form>
      )}
    </section>
  );
}
