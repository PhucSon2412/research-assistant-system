"use client";

// Admin-only list of common copies approved from a review (M6b-1). They carry
// Admin-recorded rights but are NOT published: no other page, search or RAG
// shows them, and there is no publish action yet. The API enforces all of it.

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "../../../lib/api";
import type { PendingPublication } from "../../../lib/review";
import { ErrorText, Shell, useSession } from "../../components/session";

const yes = (value: boolean) => (value ? "có" : "không");

export default function PendingPublicationsPage() {
  const user = useSession();
  const [data, setData] = useState<{ items: PendingPublication[]; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    api<{ items: PendingPublication[]; total: number }>("/admin/pending-publications?limit=100")
      .then(setData)
      .catch((err) => setError(describeError(err)));
  }, [user]);

  return (
    <Shell user={user}>
      <p><Link href="/admin">← Quản trị</Link></p>
      <h1>Bản chung chờ công bố</h1>
      <p>
        Quyền dưới đây do Admin ghi nhận cho đúng file/checksum. <strong>Đã ghi nhận quyền chưa phải là đã công
        bố</strong>: User khác chưa thấy metadata, file, kết quả tìm kiếm, chunk hay RAG của các bản này; chưa có
        index và chưa có thao tác công bố.
      </p>
      <ErrorText message={error} />
      {data && <p>Tổng: {data.total}</p>}
      {data && data.items.length === 0 && <p>Không có bản nào đang chờ.</p>}
      {data && data.items.map((p) => <section key={p.id} id={p.id} style={{ borderTop: "1px solid #ddd" }}>
        <h2>{p.title ?? p.id}</h2>
        <p>Chờ công bố · tạo {new Date(p.created_at).toLocaleString("vi-VN")} · từ{" "}
          <Link href={`/admin/reviews/${encodeURIComponent(p.rights.origin_review_request_id)}`}>yêu cầu duyệt</Link></p>
        <ul>
          <li>File: {p.file.filename ?? "(không tên)"} · {p.file.verified_mime} · {p.file.byte_size} byte ·
            SHA-256 <code>{p.file.checksum_sha256}</code></li>
          <li>Nguồn đã kiểm: <span style={{ whiteSpace: "pre-wrap" }}>{p.rights.verified_source}</span>
            {p.rights.verified_source_url && ` · ${p.rights.verified_source_url}`}</li>
          {p.rights.license_url && <li>Giấy phép: {p.rights.license_url}</li>}
          {p.rights.evidence_note && <li>Bằng chứng: <span style={{ whiteSpace: "pre-wrap" }}>{p.rights.evidence_note}</span></li>}
          <li>Kiểm ngày {p.rights.checked_on} bởi {p.rights.reviewer_email}</li>
          <li>Phạm vi (mọi tài khoản của hệ thống): tải file gốc {yes(p.rights.scope.original_download)} ·
            tìm kiếm toàn văn {yes(p.rights.scope.fulltext_search)} · gửi qua provider{" "}
            {yes(p.rights.scope.external_processing)}
            {p.rights.scope.conditions && ` · điều kiện: ${p.rights.scope.conditions}`}</li>
        </ul>
      </section>)}
    </Shell>
  );
}
