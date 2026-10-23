"use client";

// Admin screens. The API enforces Admin-only access on every call; a User
// opening this page just gets "forbidden" from the API.

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, describeError, invitationError } from "../../lib/api";
import { ErrorText, Shell, useSession } from "../components/session";

import { PageHeading, EmptyState, Badge, ConfirmButton } from "../components/ui";

type Invitation = { id: string; email: string; status: string; created_at: string; expires_at: string };
type Account = { id: string; email: string; role: string; status: string };
type OutboxMessage = { id: number; kind: string; recipient: string; link: string; expires_at: string };


const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");

export default function AdminPage() {
  const user = useSession();
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [invitationsLoaded, setInvitationsLoaded] = useState(false);
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
      setInvitationsLoaded(true);
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
      <PageHeading eyebrow="Không gian quản trị" title="Quản trị" />
      <p><Link href="/admin/reviews">Yêu cầu duyệt paper riêng</Link> (chỉ xem đúng version/file User gửi) ·{" "}
        <Link href="/admin/publications">Bản chung (chờ công bố / đã công bố / đã thu hồi)</Link></p>
      <ErrorText message={error} />
      {notice && <p role="status">{notice}</p>}

      <h2>Mời người dùng</h2>
      <form onSubmit={invite} className="form-row">
        <label className="sr-only" htmlFor="invite-email">Email người nhận</label><input id="invite-email" name="email" type="email" required placeholder="email@example.org" />
        <button type="submit">Gửi lời mời</button>
      </form>
      <p className="text-muted">Lời mời chỉ cấp vai trò User, dùng một lần, hết hạn theo cấu hình (mặc định 72 giờ).</p>
      {invitationsLoaded && invitations.length === 0 && <EmptyState title="Chưa có lời mời" />}
      <div className="table-wrap admin-table"><table><thead><tr><th>Người nhận</th><th>Trạng thái</th><th>Thời hạn</th><th>Thao tác</th></tr></thead><tbody>
        {invitations.map((inv) => (
          <tr key={inv.id}>
            <td data-label="Người nhận">{inv.email}</td><td data-label="Trạng thái"><Badge tone={inv.status === "pending" ? "warning" : "neutral"}>{inv.status === "closed_by_recovery" ? "Link đã đóng khi khôi phục; không khôi phục tài khoản" : inv.status}</Badge></td><td data-label="Thời hạn"><small>Tạo {fmt(inv.created_at)}<br />Hết hạn {fmt(inv.expires_at)}</small></td><td data-label="Thao tác">
            {inv.status === "pending" && (
              <ConfirmButton message="Thu hồi lời mời này? Liên kết sẽ không còn dùng được." className="button-secondary" onClick={() => run(() => api(`/admin/invitations/${inv.id}/revoke`, { method: "POST" }), "Đã thu hồi lời mời.")}>
                Thu hồi
              </ConfirmButton>
            )}
            </td></tr>
        ))}
      </tbody></table></div>

      <h2>Tài khoản</h2>
      <div className="table-wrap admin-table"><table><thead><tr><th>Tài khoản</th><th>Vai trò</th><th>Trạng thái</th><th>Thao tác</th></tr></thead><tbody>
        {accounts.map((acc) => (
          <tr key={acc.id}>
            <td data-label="Tài khoản">{acc.email}</td><td data-label="Vai trò"><Badge>{acc.role}</Badge></td><td data-label="Trạng thái"><Badge tone={acc.status === "active" ? "success" : "warning"}>{acc.status}</Badge></td><td data-label="Thao tác">
            {acc.id !== user?.id &&
              (acc.status === "active" ? (
                <ConfirmButton message="Vô hiệu hóa tài khoản và thu hồi mọi phiên đăng nhập của người dùng này?" className="button-danger" onClick={() => run(() => api(`/admin/users/${acc.id}/disable`, { method: "POST" }), "Đã vô hiệu hóa và thu hồi mọi phiên.")}>
                  Vô hiệu hóa
                </ConfirmButton>
              ) : (
                <button onClick={() => run(() => api(`/admin/users/${acc.id}/enable`, { method: "POST" }), "Đã kích hoạt lại.")}>
                  Kích hoạt
                </button>
              ))}
            </td></tr>
        ))}
      </tbody></table></div>

      {outbox && (
        <>
          <h2>Dev outbox (chỉ môi trường phát triển)</h2>
          <p className="notice">
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
