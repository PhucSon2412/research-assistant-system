"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api } from "../../lib/api";
import { deletionError, type DeletionReceipt } from "../../lib/private-delete";

export function DeletePrivate({ itemId, onDeleted }: { itemId: string; onDeleted: (receipt: DeletionReceipt) => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function remove(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!confirmed || submitted) return;
    setSubmitted(true);
    try {
      onDeleted(await api<DeletionReceipt>(`/library/items/${encodeURIComponent(itemId)}/delete`, {
        method: "POST", body: { confirm_delete: true },
      }));
    } catch (error) {
      setMessage(deletionError(error));
    }
  }

  return <section aria-label="Xóa paper riêng">
    <h2>Xóa toàn item riêng</h2>
    <p>Xóa mọi version và file, không có hoàn tác. Quyền xem của Admin ở yêu cầu review đang mở sẽ kết thúc.
      Bản common đã được phê duyệt độc lập vẫn được giữ. Truy cập bị chặn ngay khi quyết định xóa được lưu;
      cleanup file và dữ liệu còn lại diễn ra sau đó.</p>
    <form onSubmit={remove}>
      <label><input type="checkbox" checked={confirmed} disabled={submitted}
        onChange={(event) => setConfirmed(event.target.checked)} /> Tôi xác nhận xóa toàn item và mọi version/file.</label>
      <button type="submit" disabled={!confirmed || submitted}>Xóa vĩnh viễn item riêng</button>
    </form>
    {message && <p role="alert">{message} <Link href="/">Tải lại thư viện</Link></p>}
  </section>;
}
