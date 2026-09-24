"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../../lib/api";
import { ErrorText, Shell, useSession } from "../components/session";

type SessionRow = { id: string; created_at: string; last_activity_at: string; absolute_expires_at: string; current: boolean };

const fmt = (iso: string) => new Date(iso).toLocaleString("vi-VN");

export default function AccountPage() {
  const user = useSession();
  const router = useRouter();
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<{ sessions: SessionRow[] }>("/auth/sessions")
      .then((d) => setRows(d.sessions))
      .catch((err) => setError(describeError(err)));
  }, []);

  useEffect(() => {
    if (user) load();
  }, [user, load]);

  async function revoke(row: SessionRow) {
    try {
      await api(`/auth/sessions/${row.id}/revoke`, { method: "POST" });
      if (row.current) router.replace("/login");
      else load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function revokeOthers() {
    try {
      await api("/auth/sessions/revoke-others", { method: "POST" });
      load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <Shell user={user}>
      <h1>Phiên đăng nhập</h1>
      <p style={{ color: "#555" }}>
        Phiên hết hạn sau 30 phút không hoạt động hoặc 8 giờ kể từ khi đăng nhập (theo cấu hình máy chủ).
      </p>
      <ErrorText message={error} />
      <ul>
        {rows.map((row) => (
          <li key={row.id}>
            Đăng nhập {fmt(row.created_at)} · hoạt động {fmt(row.last_activity_at)} · hết hạn tối đa{" "}
            {fmt(row.absolute_expires_at)} {row.current && <strong>(phiên này)</strong>}{" "}
            <button onClick={() => revoke(row)}>Thu hồi</button>
          </li>
        ))}
      </ul>
      <button onClick={revokeOthers}>Thu hồi mọi phiên khác</button>
    </Shell>
  );
}
