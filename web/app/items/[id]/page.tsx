"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../../lib/api";
import { ErrorText, Shell, useSession } from "../../components/session";
import type { Item } from "../../page";

type Version = {
  id: string;
  version_label: string | null;
  source_identifier: string | null;
  source_url: string | null;
  coverage: string | null;
  received_at: string;
};
type Detail = Item & { versions: Version[] };
type FileInfo = { id: string; filename: string | null; byte_size: number; verified_mime: string; page_count: number | null };
type JobInfo = { id: string; state: string; origin: string };

export default function ItemPage() {
  const user = useSession();
  const { id } = useParams<{ id: string }>();
  const [item, setItem] = useState<(Detail & { files: FileInfo[]; jobs: JobInfo[] }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!user) return;
    api<{ item: Detail & { files: FileInfo[]; jobs: JobInfo[] } }>(`/library/items/${encodeURIComponent(id)}`)
      .then((d) => setItem(d.item))
      .catch((err) => setError(describeError(err)));
  }, [user, id]);

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const title = new FormData(e.currentTarget).get("title");
    setSaved(false);
    try {
      const d = await api<{ item: Item }>(`/library/items/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: { title },
      });
      setItem((prev) => (prev ? { ...prev, ...d.item } : prev));
      setSaved(true);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <Shell user={user}>
      <p>
        <Link href="/">← Thư viện</Link>
      </p>
      <ErrorText message={error} />
      {item && (
        <>
          <h1>{item.title ?? "(chưa có tiêu đề)"}</h1>
          <p>
            Kho: {item.visibility === "common" ? "chung" : "riêng của bạn"} · Nội dung:{" "}
            {item.content_status === "indexed" ? "đã index" : "chỉ metadata"}
          </p>
          {item.can_edit && (
            <form onSubmit={save} style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <input name="title" defaultValue={item.title ?? ""} maxLength={1000} style={{ flex: 1, minWidth: 200 }} />
              <button type="submit">Lưu tiêu đề</button>
              {saved && <span>Đã lưu.</span>}
            </form>
          )}
          <h2>Phiên bản</h2>
          <ul>
            {item.versions.map((v) => (
              <li key={v.id}>
                {v.version_label ?? "(không nhãn)"}
                {v.source_identifier && ` · ${v.source_identifier}`}
                {v.coverage && ` · coverage: ${v.coverage}`} · nhận {new Date(v.received_at).toLocaleString("vi-VN")}
              </li>
            ))}
          </ul>
          {item.jobs.length > 0 && <p>Trạng thái tiếp nhận: {item.jobs.map((j) => j.state).join(", ")}</p>}
          <h2>File gốc</h2>
          <ul>{item.files.map((f) => <li key={f.id}>
            <a href={`/api/files/${encodeURIComponent(f.id)}/original`}>{f.filename ?? "Original"}</a>
            {` · ${f.byte_size} byte`}{f.page_count ? ` · ${f.page_count} trang` : ""}
          </li>)}</ul>
        </>
      )}
    </Shell>
  );
}
