"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "../../lib/api";
import { RELATION_NOTE } from "../../lib/citations";
import { ErrorText, Shell, useSession } from "../components/session";

import { PageHeading, EmptyState, Badge } from "../components/ui";

type Paper = {
  id: string; arxiv_id: string; title: string; authors: string | null;
  original_url: string; topics: string[]; latest_version: string | null;
  source_published_at: string | null; source_updated_at: string | null;
  first_discovered_at: string; content_status: "metadata_only";
};
type Page = { items: Paper[]; total: number; limit: number; offset: number };
type Source = { topic: string; last_success_at: string | null; state: string; last_error: string | null };
type Citing = { items: { id: string; title: string | null; linked_references: number }[]; total: number };


const LIMIT = 20;
export default function DiscoveryPage() {
  const user = useSession();
  const [topic, setTopic] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [topics, setTopics] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [citing, setCiting] = useState<Record<string, Citing | string>>({});
  async function loadCiting(paperId: string) {
    try {
      const value = await api<Citing>(`/discovery/papers/${encodeURIComponent(paperId)}/citing-items?limit=20`);
      setCiting((prev) => ({ ...prev, [paperId]: value }));
    } catch (err) {
      setCiting((prev) => ({ ...prev, [paperId]: describeError(err) }));
    }
  }
  useEffect(() => {
    if (user) api<{ topics: string[] }>("/discovery/topics").then((value) => setTopics(value.topics)).catch(() => {});
  }, [user]);
  useEffect(() => {
    if (!user) return;
    api<Page>(`/discovery/papers?limit=${LIMIT}&offset=${offset}&topic=${encodeURIComponent(topic)}`)
      .then((value) => { setPage(value); setError(null); })
      .catch((err) => setError(describeError(err)));
  }, [user, topic, offset]);
  useEffect(() => {
    if (user?.role === "admin") {
      api<{ sources: Source[] }>("/discovery/sources").then((value) => setSources(value.sources)).catch(() => {});
    }
  }, [user]);
  return <Shell user={user}>
    <PageHeading eyebrow="Khám phá nghiên cứu" title="Khám phá arXiv" />
    <p>Chỉ có metadata và link tới trang gốc. Các paper này không có toàn văn trong hệ thống và không được dùng làm bằng chứng RAG.</p>
    <p>{RELATION_NOTE} Nút “Paper của tôi trích dẫn bài này” chỉ tìm trong paper riêng của bạn.</p>
    <label>Chủ đề{" "}<select value={topic} onChange={(event) => { setTopic(event.target.value); setOffset(0); }}>
      {["", ...topics].map((value) => <option key={value} value={value}>{value || "Tất cả"}</option>)}
    </select></label>
    <ErrorText message={error} />
    {page && page.items.length === 0 && <EmptyState title="Chưa có paper trong phạm vi này" />}
    {page && <><p>{page.total} paper</p><ul>
      {page.items.map((paper) => <li key={paper.id} className="spaced">
        <strong>{paper.title}</strong> <Badge>Chỉ có metadata</Badge><br />
        {paper.authors && <span>{paper.authors}<br /></span>}
        <span>Chủ đề: {paper.topics.join(", ") || "—"} · arXiv: {paper.arxiv_id}{paper.latest_version ?? ""}</span><br />
        <span>Công bố: {paper.source_published_at ? new Date(paper.source_published_at).toLocaleDateString("vi-VN") : "không rõ"} · Cập nhật nguồn: {paper.source_updated_at ? new Date(paper.source_updated_at).toLocaleDateString("vi-VN") : "không rõ"} · Mới phát hiện: {new Date(paper.first_discovered_at).toLocaleDateString("vi-VN")}</span><br />
        <a href={paper.original_url} target="_blank" rel="noopener noreferrer">Mở trang gốc trên arXiv</a>{" "}
        <button type="button" onClick={() => { void loadCiting(paper.id); }}>Paper của tôi trích dẫn bài này</button>
        {typeof citing[paper.id] === "string" && <ErrorText message={citing[paper.id] as string} />}
        {typeof citing[paper.id] === "object" && (() => {
          const value = citing[paper.id] as Citing;
          return value.total === 0 ? <p>Không có paper nào của bạn trích dẫn bài này.</p>
            : <ul>{value.items.map((item) => <li key={item.id}>
              <Link href={`/items/${item.id}`}>{item.title ?? "(chưa có tiêu đề)"}</Link>
              {` · ${item.linked_references} tài liệu tham khảo`}</li>)}
              {value.total > value.items.length && <li>… và {value.total - value.items.length} paper khác</li>}</ul>;
        })()}
      </li>)}
    </ul><p><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}>Trước</button>{" "}
      <button disabled={offset + LIMIT >= page.total} onClick={() => setOffset(offset + LIMIT)}>Sau</button></p></>}
    {user?.role === "admin" && <section><h2>Đồng bộ gần nhất</h2><ul>{sources.map((source) =>
      <li key={source.topic}>{source.topic}: {source.last_success_at ? new Date(source.last_success_at).toLocaleString("vi-VN") : "chưa hoàn tất"} ({source.state}{source.last_error ? `: ${source.last_error}` : ""})</li>)}</ul></section>}
  </Shell>;
}
