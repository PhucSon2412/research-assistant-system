"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import { DEFAULT_RERANK, SEARCH_NOTICE, makeSearchBody } from "../../lib/search";
import { ErrorText, Shell, useSession } from "../components/session";

type Hit = {
  chunk_id: string; item_id: string; version_id: string; source_file_id: string;
  title: string | null; text: string; coverage: string | null;
  locator_kind: "pdf" | "markdown"; start_page: number | null;
  start_line: number | null; end_line: number | null; section_kind: string;
};
type Result = { mode: "keyword" | "hybrid"; rerank_status: string; count: number; hits: Hit[] };
type Collection = { id: string; name: string };
type CollectionItem = { id: string; title: string | null; content_status: string; visibility: string };

export default function SearchPage() {
  const user = useSession();
  const [query, setQuery] = useState("");
  const [itemId, setItemId] = useState("");
  const [collectionId, setCollectionId] = useState("");
  const [collections, setCollections] = useState<Collection[]>([]);
  const [collectionItems, setCollectionItems] = useState<CollectionItem[]>([]);
  const [mode, setMode] = useState<"keyword" | "hybrid">("keyword");
  const [rerank, setRerank] = useState(DEFAULT_RERANK);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!user) return;
    api<{ collections: Collection[] }>("/collections").then((data) => setCollections(data.collections)).catch((err) => setError(describeError(err)));
  }, [user]);

  useEffect(() => {
    if (!collectionId) { setCollectionItems([]); return; }
    api<{ items: CollectionItem[] }>(`/collections/${collectionId}/items?limit=100`)
      .then((data) => setCollectionItems(data.items)).catch((err) => setError(describeError(err)));
  }, [collectionId]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setResult(null); setError(null);
    try {
      const response = await api<Result>("/search", { method: "POST",
        body: makeSearchBody(query, mode, rerank, itemId, collectionId) });
      setResult(response);
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  return <Shell user={user}>
    <p><Link href="/">← Thư viện</Link> · <Link href="/collections">Quản lý collection</Link></p>
    <h1>Tìm trong paper đã index</h1>
    <p>{SEARCH_NOTICE}</p>
    <form onSubmit={submit} style={{ display: "grid", gap: "0.75rem", maxWidth: 700 }}>
      <label>Câu tìm kiếm <input value={query} onChange={(e) => setQuery(e.target.value)} required maxLength={1024} style={{ width: "100%" }} /></label>
      <label>Collection <select value={collectionId} onChange={(e) => { setCollectionId(e.target.value); setItemId(""); }}>
        <option value="">Tất cả paper riêng đã index</option>
        {collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select></label>
      <label>ID paper riêng (để trống để tìm tất cả) <input value={itemId} disabled={!!collectionId} onChange={(e) => setItemId(e.target.value)} style={{ width: "100%" }} /></label>
      <label>Chế độ <select value={mode} onChange={(e) => setMode(e.target.value as "keyword" | "hybrid")}>
        <option value="keyword">Chỉ từ khóa</option><option value="hybrid">Hybrid: từ khóa + vector</option>
      </select></label>
      <label><input type="checkbox" checked={rerank} onChange={(e) => setRerank(e.target.checked)} /> Rerank kết quả nếu khả dụng</label>
      <button disabled={busy || !user} type="submit">{busy ? "Đang tìm…" : "Tìm kiếm"}</button>
    </form>
    {collectionId && <section><h2>Paper trong collection</h2>
      <p>Paper chỉ có metadata được liệt kê ở đây nhưng không xuất hiện trong kết quả toàn văn.</p>
      <ul>{collectionItems.map((item) => <li key={item.id}><Link href={`/items/${item.id}`}>{item.title || item.id}</Link> — {item.content_status === "metadata_only" ? "Chỉ metadata" : item.visibility === "common" ? "Paper chung; chưa mở full-text" : "Đã index"}</li>)}</ul>
    </section>}
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
