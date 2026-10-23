"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import { contentLabel } from "../../lib/files";
import { ErrorText, Shell, useSession } from "../components/session";

import { PageHeading, EmptyState } from "../components/ui";

type Collection = { id: string; name: string };
type Item = { id: string; title: string | null; visibility: string; content_status: string;
  fulltext_search_available?: boolean; rag_available?: boolean };


export default function CollectionsPage() {
  const user = useSession();
  const [collections, setCollections] = useState<Collection[]>([]);
  const [collectionsLoaded, setCollectionsLoaded] = useState(false);
  const [selected, setSelected] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [available, setAvailable] = useState<Item[]>([]);
  const [name, setName] = useState("");
  const [itemId, setItemId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (!user) return;
    api<{ collections: Collection[] }>("/collections")
      .then((data) => { setCollections(data.collections); setCollectionsLoaded(true); setError(null); })
      .catch((err) => setError(describeError(err)));
  }, [user, refresh]);

  useEffect(() => {
    if (!user) return;
    api<{ items: Item[] }>("/library/items?limit=100")
      .then((data) => setAvailable(data.items)).catch((err) => setError(describeError(err)));
  }, [user]);

  useEffect(() => {
    if (!selected) { setItems([]); return; }
    api<{ items: Item[]; total: number }>(`/collections/${selected}/items?limit=20&offset=${offset}`)
      .then((data) => { setItems(data.items); setTotal(data.total); }).catch((err) => setError(describeError(err)));
  }, [selected, offset, refresh]);

  async function create(event: FormEvent) {
    event.preventDefault();
    try {
      const response = await api<{ collection: Collection }>("/collections", { method: "POST", body: { name } });
      setSelected(response.collection.id); setOffset(0); setName(""); setRefresh((n) => n + 1);
    } catch (err) { setError(describeError(err)); }
  }

  async function rename() {
    if (!selected) return;
    try { await api(`/collections/${selected}`, { method: "PATCH", body: { name } }); setName(""); setRefresh((n) => n + 1); }
    catch (err) { setError(describeError(err)); }
  }

  async function remove() {
    if (!selected || !window.confirm("Xóa collection này?")) return;
    try { await api(`/collections/${selected}`, { method: "DELETE" }); setSelected(""); setOffset(0); setRefresh((n) => n + 1); }
    catch (err) { setError(describeError(err)); }
  }

  async function add(event: FormEvent) {
    event.preventDefault();
    try { await api(`/collections/${selected}/items`, { method: "POST", body: { item_id: itemId.trim() } });
      setItemId(""); setRefresh((n) => n + 1); }
    catch (err) { setError(describeError(err)); }
  }

  async function removeItem(id: string) {
    try { await api(`/collections/${selected}/items/${id}`, { method: "DELETE" }); setRefresh((n) => n + 1); }
    catch (err) { setError(describeError(err)); }
  }

  return <Shell user={user}>
    <p><Link href="/">← Thư viện</Link> · <Link href="/search">Tìm kiếm</Link></p>
    <PageHeading eyebrow="Bộ sưu tập & phạm vi" title="Collection của tôi" />
    <p>Collection nhóm paper bạn xem được. Thành viên không cấp quyền đọc nội dung; paper chung chỉ có metadata không vào tìm kiếm toàn văn.</p>
    <ErrorText message={error} />
    <form onSubmit={create} className="form-panel"><label>Tên collection mới <input value={name} maxLength={200} onChange={(e) => setName(e.target.value)} required /></label> <button>Tạo</button></form>
    <label>Collection <select value={selected} onChange={(e) => { setSelected(e.target.value); setOffset(0); }}>
      <option value="">Chọn collection</option>{collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
    </select></label>
    {collectionsLoaded && collections.length === 0 && <EmptyState title="Bắt đầu một bộ sưu tập">Nhóm tài liệu theo chủ đề để thu hẹp phạm vi nghiên cứu.</EmptyState>}
    {selected && <section>
      <p><button onClick={rename} disabled={!name.trim()}>Đổi tên theo ô tên</button> <button onClick={remove}>Xóa collection</button></p>
      <form onSubmit={add} className="form-panel"><label>Paper từ thư viện <input list="available-papers" value={itemId} onChange={(e) => setItemId(e.target.value)} required /></label>
        <datalist id="available-papers">{available.map((item) => <option key={item.id} value={item.id}>{item.title || item.id} — {contentLabel(item)}</option>)}</datalist>
        {" "}<button>Thêm paper</button></form>
      <ul>{items.map((item) => <li key={item.id}>
        <Link href={`/items/${item.id}`}>{item.title || item.id}</Link> — {contentLabel(item)}
        {" "}<button onClick={() => removeItem(item.id)}>Bỏ khỏi collection</button>
      </li>)}</ul>
      <p>{total} paper · <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Trước</button> {" "}
        <button disabled={offset + 20 >= total} onClick={() => setOffset(offset + 20)}>Sau</button></p>
    </section>}
  </Shell>;
}
