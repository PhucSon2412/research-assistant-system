"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import { contentLabel, sourceOriginalHref } from "../../lib/files";
import { DEFAULT_RERANK, SEARCH_NOTICE, makeSearchBody, sourceKindLabel } from "../../lib/search";
import { ErrorText, Shell, useSession } from "../components/session";
import { Badge, EmptyState, PageHeading } from "../components/ui";

type Hit = {
  chunk_id: string; item_id: string; version_id: string; source_file_id: string;
  title: string | null; text: string; coverage: string | null;
  locator_kind: "pdf" | "markdown"; start_page: number | null;
  start_line: number | null; end_line: number | null; section_kind: string;
  // Download path only when the download gate allows it; search rights never imply it.
  visibility: "private" | "common"; original_url: string | null;
};
type Result = { mode: "keyword" | "hybrid"; rerank_status: string; rerank_withheld?: number; count: number;
  hits: Hit[];
  keyword_query?: { status: "ok" | "no_terms"; terms: number; min_match: number; dropped_terms: number } };
type Collection = { id: string; name: string };
type CollectionItem = { id: string; title: string | null; content_status: string; visibility: string;
  fulltext_search_available?: boolean; rag_available?: boolean };

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

  // "Tìm trong paper này" links from the item page carry ?item_id=.
  useEffect(() => {
    const fromLink = new URLSearchParams(window.location.search).get("item_id");
    if (fromLink) setItemId(fromLink);
  }, []);

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
    <PageHeading eyebrow="Tra cứu & bằng chứng" title="Tìm kiếm & hỏi đáp">Tìm trong paper đã index bạn có quyền. Mở một paper từ kết quả để hỏi đáp có dẫn nguồn.</PageHeading>
    <p className="notice">{SEARCH_NOTICE}</p>
    <form onSubmit={submit} className="form-panel search-form" aria-busy={busy}>
      <label>Câu tìm kiếm <input value={query} onChange={(e) => setQuery(e.target.value)} required maxLength={1024} className="full-width" /></label>
      <label>Collection <select value={collectionId} onChange={(e) => { setCollectionId(e.target.value); setItemId(""); }}>
        <option value="">Tất cả paper riêng đã index và bản chung được phép</option>
        {collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select></label>
      <label className="search-scope">ID paper (riêng của bạn hoặc bản chung được phép; để trống để tìm tất cả) <input value={itemId} disabled={!!collectionId} onChange={(e) => setItemId(e.target.value)} className="full-width" /></label>
      <label>Chế độ <select value={mode} onChange={(e) => setMode(e.target.value as "keyword" | "hybrid")}>
        <option value="keyword">Chỉ từ khóa</option><option value="hybrid">Hybrid: từ khóa + vector</option>
      </select></label>
      <label><input type="checkbox" checked={rerank} onChange={(e) => setRerank(e.target.checked)} /> Rerank kết quả nếu khả dụng</label>
      <button disabled={busy || !user} type="submit">{busy ? "Đang tìm…" : "Tìm kiếm"}</button>
    </form>
    {collectionId && <section><h2>Paper trong collection</h2>
      <p>Paper chỉ có metadata được liệt kê ở đây nhưng không xuất hiện trong kết quả toàn văn.</p>
      <ul>{collectionItems.map((item) => <li key={item.id}><Link href={`/items/${item.id}`}>{item.title || item.id}</Link> — {contentLabel(item)}</li>)}</ul>
    </section>}
    <ErrorText message={error} />
    {busy && <p role="status" aria-live="polite">Đang tìm bằng chứng trong phạm vi bạn đã chọn…</p>}
    {result && <section aria-live="polite">
      <p>Chế độ: {result.mode === "hybrid" ? "hybrid" : "chỉ từ khóa"} · Rerank: {result.rerank_status} · {result.count} đoạn</p>
      {result.hits.length === 0 && <EmptyState title="Chưa tìm thấy đoạn phù hợp">Thử thuật ngữ cụ thể hơn hoặc kiểm tra phạm vi tìm kiếm.</EmptyState>}
      {(result.rerank_withheld ?? 0) > 0 && <p className="text-small">
        {result.rerank_withheld} đoạn của bản chung không được gửi rerank (quyền không cho xử lý qua provider); giữ thứ tự tìm kiếm.</p>}
      {result.keyword_query?.status === "no_terms" &&
        <p>Truy vấn chỉ gồm từ phổ biến nên phần từ khóa không có thuật ngữ để tìm; hãy thêm thuật ngữ cụ thể.</p>}
      {result.keyword_query?.status === "ok" &&
        <p className="text-small">Từ khóa: đoạn khớp ít nhất {result.keyword_query.min_match}/{result.keyword_query.terms} thuật ngữ
          {result.keyword_query.dropped_terms > 0 && ` (bỏ ${result.keyword_query.dropped_terms} thuật ngữ vượt giới hạn)`}.</p>}
      {result.hits.map((hit) => { const original = sourceOriginalHref(hit.original_url, hit.locator_kind, hit.start_page);
        return <article key={hit.chunk_id} className="search-hit">
        <h2><Link href={`/items/${hit.item_id}`}>{hit.title || sourceKindLabel(hit.visibility)}</Link></h2>
        <p><Badge>{sourceKindLabel(hit.visibility)}</Badge> · {hit.coverage === "partial" ? "Chỉ trích xuất được một phần paper. " : ""}
          {hit.section_kind === "abstract" ? "Tóm tắt" : "Thân bài"} · {hit.locator_kind === "pdf"
            ? `trang ${hit.start_page}` : `dòng ${hit.start_line}–${hit.end_line}`}</p>
        <p className="pre-wrap">{hit.text}</p>
        <p><Link href={`/sources/${hit.chunk_id}`}>Mở đúng chunk và vị trí nguồn</Link></p>
        {original ? <a href={original}>Mở file gốc tại vị trí nguồn</a> : <p>Không có file gốc được phép tải.</p>}
        <p className="text-small">Version {hit.version_id} · File {hit.source_file_id} · Chunk {hit.chunk_id}</p>
      </article>; })}
    </section>}
  </Shell>;
}
