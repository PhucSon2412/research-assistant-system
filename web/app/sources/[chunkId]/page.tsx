"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, describeError } from "../../../lib/api";
import { ErrorText, Shell, useSession } from "../../components/session";

type Source = {
  chunk_id: string; item_id: string; version_id: string; source_file_id: string;
  title: string | null; coverage: string | null; text: string;
  locator_kind: "pdf" | "markdown"; start_page: number | null;
  start_line: number | null; end_line: number | null;
  start_offset: number | null; end_offset: number | null;
};

export default function SourcePage() {
  const user = useSession();
  const { chunkId } = useParams<{ chunkId: string }>();
  const [source, setSource] = useState<Source | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!user) return;
    api<Source>(`/search/chunks/${encodeURIComponent(chunkId)}`)
      .then((value) => { setSource(value); setError(null); })
      .catch((err) => { setSource(null); setError(describeError(err)); });
  }, [user, chunkId]);
  return <Shell user={user}>
    <p><Link href="/search">← Kết quả tìm kiếm</Link></p>
    <ErrorText message={error} />
    {source && <>
      <h1>{source.title || "Paper riêng"}</h1>
      <p>Version {source.version_id} · File {source.source_file_id} · Chunk {source.chunk_id}</p>
      <p>{source.locator_kind === "pdf" ? `Trang ${source.start_page}` :
        `Dòng ${source.start_line}–${source.end_line}, offset ${source.start_offset}–${source.end_offset}`}</p>
      {source.coverage === "partial" && <p>Paper này chỉ được trích xuất một phần.</p>}
      <p style={{ whiteSpace: "pre-wrap" }}>{source.text}</p>
      <p><a href={`/api/files/${source.source_file_id}/original${source.locator_kind === "pdf" ? `#page=${source.start_page}` : ""}`}>Mở file gốc</a></p>
      <p><Link href={`/items/${source.item_id}`}>Xem paper</Link></p>
    </>}
  </Shell>;
}
