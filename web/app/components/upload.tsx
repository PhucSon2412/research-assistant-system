"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, csrfToken, describeError } from "../../lib/api";

type Notice = { version: string; text: string };

export function Upload({ onComplete }: { onComplete: () => void }) {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    api<Notice>("/uploads/notice").then(setNotice).catch((err) => setError(describeError(err)));
  }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!file || !notice || !confirmed) return;
    setBusy(true);
    setError(null);
    setResult(null);
    let putStarted = false;
    try {
      const kind = file.name.toLowerCase().endsWith(".pdf") ? "pdf" : "markdown";
      const hash = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      const sha256 = Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2, "0")).join("");
      // The intent request contains only metadata and the acknowledgement.
      const intent = await api<{ intent_token: string }>("/uploads/intents", {
        method: "POST",
        body: { acknowledged: true, notice_version: notice.version, file_kind: kind,
                filename: file.name, byte_size: file.size, sha256 },
      });
      const csrf = await csrfToken();
      putStarted = true;
      const response = await fetch("/api/uploads/original", {
        method: "PUT",
        headers: { "x-csrf-token": csrf, "x-upload-intent": intent.intent_token,
                   "content-type": "application/octet-stream" },
        body: file,
        credentials: "same-origin",
        cache: "no-store",
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new ApiError(response.status,
        response.status >= 500 && !data?.error ? "upload_outcome_uncertain" : data?.error ?? "upload_failed");
      if (!data?.item_id) throw new ApiError(502, "upload_response_unavailable");
      setResult(data.item_id);
      setFile(null);
      onComplete();
    } catch (err) {
      const uncertain = putStarted && (!(err instanceof ApiError) || err.status >= 500);
      setError(describeError(uncertain ? new ApiError(503, "upload_outcome_uncertain") : err)
        + (putStarted && !uncertain ? " Nếu file đã được nhận trước khi phiên thay đổi, hãy kiểm tra thư viện." : ""));
    } finally {
      // One explicit confirmation cannot silently carry over to the next upload.
      setConfirmed(false);
      setBusy(false);
    }
  }

  return (
    <section aria-label="Tải paper riêng" className="upload-card">
      <span className="upload-icon" aria-hidden="true"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" /></svg></span>
      <h2>Tải paper vào kho riêng</h2>
      <p>{notice?.text ?? "Đang tải thông báo xử lý ngoài…"}</p>
      <form onSubmit={submit}>
        <p><label htmlFor="upload-file">Chọn tài liệu · PDF hoặc Markdown</label><input id="upload-file" type="file" accept=".pdf,.md,.markdown" disabled={busy} onChange={(e) => {
          setFile(e.target.files?.[0] ?? null);
          setConfirmed(false);
        }} /></p>
        <label><input type="checkbox" checked={confirmed} disabled={busy || !file || !notice}
          onChange={(e) => setConfirmed(e.target.checked)} /> Tôi đồng ý xử lý ngoài cho lần upload này.</label>
        <p><button type="submit" disabled={!file || !notice || !confirmed || busy}>
          {busy ? "Đang tải…" : "Tải lên"}
        </button></p>
      </form>
      {error && <p role="alert">{error}</p>}
      {result && <p role="status">Đã nhận file. <a href={`/items/${result}`}>Xem item</a>.</p>}
    </section>
  );
}
