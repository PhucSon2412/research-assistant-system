"use client";

// Library metadata list. Filtering by permission happens in the API; the
// scope selector can only narrow what the API already allows.

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "../lib/api";
import { ErrorText, Shell, useSession } from "./components/session";

export type Item = {
  id: string;
  visibility: "common" | "private";
  owned_by_me: boolean;
  content_status: "metadata_only" | "indexed";
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

  useEffect(() => {
    if (!user) return;
    api<Page>(`/library/items?scope=${scope}&limit=${PAGE_SIZE}&offset=${offset}`)
      .then((p) => {
        setPage(p);
        setError(null);
      })
      .catch((err) => setError(describeError(err)));
  }, [user, scope, offset]);

  return (
    <Shell user={user}>
      <h1>Thư viện</h1>
      <p style={{ color: "#555" }}>Kho chung và kho riêng của bạn. Chưa có upload ở giai đoạn này.</p>
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
      {page && (
        <>
          <p>Tổng: {page.total}</p>
          <table style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr style={{ textAlign: "left" }}>
                <th>Tiêu đề</th>
                <th>Kho</th>
                <th>Trạng thái nội dung</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.id} style={{ borderTop: "1px solid #ddd" }}>
                  <td>
                    <Link href={`/items/${item.id}`}>{item.title ?? "(chưa có tiêu đề)"}</Link>
                  </td>
                  <td>{item.visibility === "common" ? "Chung" : "Riêng"}</td>
                  <td>{item.content_status === "indexed" ? "Đã index" : "Chỉ metadata"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p style={{ display: "flex", gap: "0.5rem" }}>
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
