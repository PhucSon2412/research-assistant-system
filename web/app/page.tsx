"use client";

// Library metadata list. Filtering by permission happens in the API; the
// scope selector can only narrow what the API already allows.

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "../lib/api";
import { contentLabel } from "../lib/files";
import { ErrorText, Shell, useSession } from "./components/session";
import { Upload } from "./components/upload";
import { Badge, EmptyState, Loading, PageHeading } from "./components/ui";

export type Item = {
  id: string;
  visibility: "common" | "private";
  owned_by_me: boolean;
  content_status: "metadata_only" | "indexed";
  // From the API's own gates (M6b-2b3a): a common copy can be indexed without being searchable,
  // and a searchable common copy is not a RAG scope yet.
  fulltext_search_available: boolean;
  rag_available: boolean;
  title: string | null;
  created_at: string;
  can_edit: boolean;
};

type Page = { items: Item[]; total: number; limit: number; offset: number };

const PAGE_SIZE = 20;

export default function LibraryPage() {
  const user = useSession();
  const [scope, setScope] = useState("all");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (!user) return;
    api<Page>(`/library/items?scope=${scope}&limit=${PAGE_SIZE}&offset=${offset}`)
      .then((p) => {
        setPage(p);
        setError(null);
      })
      .catch((err) => setError(describeError(err)));
  }, [user, scope, offset, refresh]);

  return (
    <Shell user={user}>
      <p><Link href="/search">Tìm kiếm toàn văn (paper riêng và bản chung được phép)</Link> · <Link href="/collections">Collection của tôi</Link></p>
      <PageHeading eyebrow="Tài liệu & tri thức" title="Thư viện">Kho chung và kho riêng của bạn. Một nơi để lưu trữ, tìm kiếm và kết nối bằng chứng.</PageHeading>
      {user?.role === "user" && <Upload onComplete={() => { setOffset(0); setRefresh((n) => n + 1); }} />}
      <label>
        Phạm vi{" "}
        <select
          value={scope}
          onChange={(e) => {
            setScope(e.target.value);
            setOffset(0);
          }}
        >
          <option value="all">Tất cả được phép</option>
          <option value="common">Kho chung</option>
          <option value="mine">Kho riêng của tôi</option>
        </select>
      </label>
      <ErrorText message={error} />
      {!page && !error && <Loading />}
      {page && (
        <>
          <p className="text-small">Tổng: {page.total} tài liệu</p>
          {page.total === 0 && <EmptyState title="Thư viện chưa có tài liệu">Tải paper vào kho riêng để bắt đầu nghiên cứu.</EmptyState>}
          <div className="table-wrap">
          <table className="data-table library-table">
            <thead>
              <tr>
                <th>Tiêu đề</th>
                <th>Kho</th>
                <th>Trạng thái nội dung</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.id} className="record-row">
                  <td>
                    <Link href={`/items/${item.id}`}>{item.title ?? "(chưa có tiêu đề)"}</Link>
                  </td>
                  <td><Badge>{item.visibility === "common" ? "Chung" : "Riêng"}</Badge></td>
                  <td><Badge tone={item.fulltext_search_available ? "success" : "neutral"}>{contentLabel(item)}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          <p className="actions">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              Trước
            </button>
            <button disabled={offset + PAGE_SIZE >= page.total} onClick={() => setOffset(offset + PAGE_SIZE)}>
              Sau
            </button>
          </p>
        </>
      )}
    </Shell>
  );
}
