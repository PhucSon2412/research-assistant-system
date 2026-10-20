"use client";

// Admin screens. The API enforces Admin-only access on every call; a User
// opening this page just gets "forbidden" from the API.

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, describeError, invitationError } from "../../lib/api";
import { ErrorText, Shell, useSession } from "../components/session";

type Invitation = { id: string; email: string; status: string; created_at: string; expires_at: string };
type Account = { id: string; email: string; role: string; status: string };
type OutboxMessage = { id: number; kind: string; recipient: string; link: string; expires_at: string };

const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");

export default function AdminPage() {
  const user = useSession();
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [outbox, setOutbox] = useState<OutboxMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [inv, acc] = await Promise.all([
        api<{ invitations: Invitation[] }>("/admin/invitations"),
        api<{ users: Account[] }>("/admin/users"),
      ]);
      setInvitations(inv.invitations);
      setAccounts(acc.users);
      try {
        setOutbox((await api<{ messages: OutboxMessage[] }>("/admin/dev-outbox")).messages);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) setOutbox(null);
        else throw err;
      }
    } catch (err) {
      setError(describeError(err));
    }
  }, []);

  useEffect(() => {
    if (user) load();
  }, [user, load]);

  async function run(action: () => Promise<unknown>, done: string, explain: (err: unknown) => string = describeError) {
    setError(null);
    setNotice(null);
    try {
      await action();
      setNotice(done);
      await load();
    } catch (err) {
      setError(explain(err));
    }
  }

  function invite(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const formEl = e.currentTarget;
    const form = new FormData(formEl);
    run(
      () => api("/admin/invitations", { method: "POST", body: { email: form.get("email") } }),
      // The channel accepting a message is not proof the invitee received it.
      "Đã tạo lời mời (vai trò User) và kênh gửi đã nhận liên kết. Đây chưa xác nhận người nhận đã nhận được email.",
      invitationError,
    ).then(() => formEl.reset());
  }

  return (
    <Shell user={user}>
      <h1>Quản trị</h1>
      <p><Link href="/admin/reviews">Yêu cầu duyệt paper riêng</Link> (chỉ xem đúng version/file User gửi) ·{" "}
        <Link href="/admin/publications">Bản chung (chờ công bố / đã công bố / đã thu hồi)</Link></p>
      <ErrorText message={error} />
      {notice && <p role="status">{notice}</p>}

      <h2>Mời người dùng</h2>
      <form onSubmit={invite} style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <input name="email" type="email" required placeholder="email@example.org" />
        <button type="submit">Gửi lời mời</button>
      </form>
      <p style={{ color: "#555" }}>Lời mời chỉ cấp vai trò User, dùng một lần, hết hạn theo cấu hình (mặc định 72 giờ).</p>
      <ul>
        {invitations.map((inv) => (
          <li key={inv.id}>
            {inv.email} · {inv.status === "closed_by_recovery" ? "Link đã đóng khi khôi phục; không khôi phục tài khoản" : inv.status} · tạo {fmt(inv.created_at)} · hết hạn {fmt(inv.expires_at)}{" "}
            {inv.status === "pending" && (
              <button onClick={() => run(() => api(`/admin/invitations/${inv.id}/revoke`, { method: "POST" }), "Đã thu hồi lời mời.")}>
                Thu hồi
              </button>
            )}
          </li>
        ))}
      </ul>

      <h2>Tài khoản</h2>
      <ul>
        {accounts.map((acc) => (
          <li key={acc.id}>
            {acc.email} · {acc.role} · {acc.status}{" "}
            {acc.id !== user?.id &&
              (acc.status === "active" ? (
                <button onClick={() => run(() => api(`/admin/users/${acc.id}/disable`, { method: "POST" }), "Đã vô hiệu hóa và thu hồi mọi phiên.")}>
                  Vô hiệu hóa
                </button>
              ) : (
                <button onClick={() => run(() => api(`/admin/users/${acc.id}/enable`, { method: "POST" }), "Đã kích hoạt lại.")}>
                  Kích hoạt
                </button>
              ))}
          </li>
        ))}
      </ul>

      {outbox && (
        <>
          <h2>Dev outbox (chỉ môi trường phát triển)</h2>
          <p style={{ color: "#8a4b00" }}>
            Liên kết mời/đặt lại mật khẩu được giữ trong bộ nhớ API để thử cục bộ. Không bật ngoài môi trường dev.
          </p>
          <ul>
            {outbox.map((m) => (
              <li key={m.id}>
                {m.kind} → {m.recipient} · hết hạn {fmt(m.expires_at)} · <a href={m.link}>mở liên kết</a>
              </li>
            ))}
          </ul>
          <button onClick={load}>Làm mới</button>
        </>
      )}
    </Shell>
  );
}
