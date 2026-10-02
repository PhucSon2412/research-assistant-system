"use client";

// Admin list of review requests. Only open requests carry item/file metadata;
// closed ones show just status and dates. The API enforces all of it.

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "../../../lib/api";
import { STATUS_TEXT, closeReasonText, type AdminReviewRequest } from "../../../lib/review";
import { ErrorText, Shell, useSession } from "../../components/session";

const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");

export default function ReviewListPage() {
  const user = useSession();
  const [status, setStatus] = useState<"open" | "closed">("open");
  const [data, setData] = useState<{ requests: AdminReviewRequest[]; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    setData(null);
    api<{ requests: AdminReviewRequest[]; total: number }>(`/admin/review-requests?status=${status}&limit=100`)
      .then(setData)
      .catch((err) => setError(describeError(err)));
  }, [user, status]);

  return (
    <Shell user={user}>
      <p><Link href="/admin">← Quản trị</Link></p>
      <h1>Yêu cầu duyệt paper riêng</h1>
      <p>
        Mỗi yêu cầu chỉ cho phép xem tạm đúng version/file User đã gửi, trong lúc yêu cầu còn mở. Yêu cầu đang chờ
        duyệt <strong>chưa phải là công bố</strong>. Phê duyệt ghi nhận quyền do Admin tự kiểm và tạo một bản chung
        riêng <strong>chờ công bố</strong> (xem <Link href="/admin/publications">bản chung chờ công bố</Link>);
        chưa có thao tác công bố.
      </p>
      <div style={{ display: "flex", gap: "0.5rem" }}>
        <button type="button" disabled={status === "open"} onClick={() => setStatus("open")}>Đang mở</button>
        <button type="button" disabled={status === "closed"} onClick={() => setStatus("closed")}>Đã đóng</button>
      </div>
      <ErrorText message={error} />
      {data && <p>Tổng: {data.total}</p>}
      {data && data.requests.length === 0 && <p>Không có yêu cầu.</p>}
      {data && <ul>{data.requests.map((r) => <li key={r.id} style={{ marginBottom: "0.5rem" }}>
        <Link href={`/admin/reviews/${encodeURIComponent(r.id)}`}>{r.item?.title ?? r.id}</Link>
        {" · "}{STATUS_TEXT[r.status]} · gửi {fmt(r.submitted_at)}
        {r.requester_email && ` · ${r.requester_email}`}
        {r.file && ` · ${r.file.filename ?? "file gốc"}`}
        {r.closed_at && ` · đóng ${fmt(r.closed_at)}`}
        {closeReasonText(r.close_reason_code, r.status) && ` · ${closeReasonText(r.close_reason_code, r.status)}`}
        {r.status === "open" && !r.review_available && " · không còn quyền xem (quyền của item đã thay đổi)"}
      </li>)}</ul>}
    </Shell>
  );
}
