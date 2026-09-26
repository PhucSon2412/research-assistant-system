"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import { ErrorText, Shell, useSession } from "../components/session";

type Hit = {
  chunk_id: string; item_id: string; version_id: string; source_file_id: string;
  title: string | null; text: string; coverage: string | null;
  locator_kind: "pdf" | "markdown"; start_page: number | null;
  start_line: number | null; end_line: number | null; section_kind: string;
};
type Result = { mode: "keyword" | "hybrid"; rerank_status: string; count: number; hits: Hit[] };

export default function SearchPage() {
  const user = useSession();
  const [query, setQuery] = useState("");
  const [itemId, setItemId] = useState("");
  const [mode, setMode] = useState<"keyword" | "hybrid">("keyword");
  const [rerank, setRerank] = useState(true);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setResult(null); setError(null);
    try {
      const response = await api<Result>("/search", { method: "POST", body: {
        query, mode, rerank, item_id: itemId.trim() || null,
      }});
      setResult(response);
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  return <Shell user={user}>
    <p><Link href="/">← Thư viện</Link></p>
    <h1>Tìm trong paper đã index</h1>
    <p>Chỉ tìm nội dung paper riêng của bạn đã index. Chế độ hybrid gửi câu hỏi tới OpenRouter/provider để tạo embedding. Nếu bật rerank, các đoạn kết quả được phép có thể được gửi tới provider.</p>
    <form onSubmit={submit} style={{ display: "grid", gap: "0.75rem", maxWidth: 700 }}>
      <label>Câu tìm kiếm <input value={query} onChange={(e) => setQuery(e.target.value)} required maxLength={1024} style={{ width: "100%" }} /></label>
      <label>ID paper riêng (để trống để tìm tất cả) <input value={itemId} onChange={(e) => setItemId(e.target.value)} style={{ width: "100%" }} /></label>
      <label>Chế độ <select value={mode} onChange={(e) => setMode(e.target.value as "keyword" | "hybrid")}>
        <option value="keyword">Chỉ từ khóa</option><option value="hybrid">Hybrid: từ khóa + vector</option>
      </select></label>
      <label><input type="checkbox" checked={rerank} onChange={(e) => setRerank(e.target.checked)} /> Rerank kết quả nếu khả dụng</label>
      <button disabled={busy || !user} type="submit">{busy ? "Đang tìm…" : "Tìm kiếm"}</button>
    </form>
    <ErrorText message={error} />
    {result && <section aria-live="polite">
      <p>Chế độ: {result.mode === "hybrid" ? "hybrid" : "chỉ từ khóa"} · Rerank: {result.rerank_status} · {result.count} đoạn</p>
      {result.hits.map((hit) => <article key={hit.chunk_id} style={{ borderTop: "1px solid #ccc", marginTop: "1rem" }}>
        <h2><Link href={`/items/${hit.item_id}`}>{hit.title || "Paper riêng"}</Link></h2>
        <p>{hit.coverage === "partial" ? "Chỉ trích xuất được một phần paper. " : ""}
          {hit.section_kind === "abstract" ? "Tóm tắt" : "Thân bài"} · {hit.locator_kind === "pdf"
            ? `trang ${hit.start_page}` : `dòng ${hit.start_line}–${hit.end_line}`}</p>
        <p style={{ whiteSpace: "pre-wrap" }}>{hit.text}</p>
        <p><Link href={`/sources/${hit.chunk_id}`}>Mở đúng chunk và vị trí nguồn</Link></p>
        <a href={`/api/files/${hit.source_file_id}/original${hit.locator_kind === "pdf" ? `#page=${hit.start_page}` : ""}`}>Mở file gốc tại vị trí nguồn</a>
        <p style={{ fontSize: "0.8rem", color: "#555" }}>Version {hit.version_id} · File {hit.source_file_id} · Chunk {hit.chunk_id}</p>
      </article>)}
    </section>}
  </Shell>;
}
