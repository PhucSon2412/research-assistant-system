"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError, type User } from "../../lib/api";

// Asks the API who is logged in. Showing/hiding links here is cosmetic: every
// API call is authorised by the server.
export function useSession(): User | null {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  useEffect(() => {
    api<{ user: User }>("/auth/session")
      .then((d) => setUser(d.user))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
      });
  }, [router]);
  return user;
}

export function Shell({ user, children }: { user: User | null; children: ReactNode }) {
  const router = useRouter();
  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      router.replace("/login");
    }
  }
  if (!user) return <p>Đang tải…</p>;
  return (
    <>
      <nav style={{ display: "flex", gap: "1rem", alignItems: "center", flexWrap: "wrap", marginBottom: "1.5rem" }}>
        <strong>Research Assistant</strong>
        <Link href="/">Thư viện</Link>
        <Link href="/discovery">Khám phá</Link>
        <Link href="/account">Phiên đăng nhập</Link>
        {user.role === "admin" && <Link href="/admin">Quản trị</Link>}
        <span style={{ marginLeft: "auto" }}>
          {user.email} ({user.role === "admin" ? "Admin" : "User"})
        </span>
        <button onClick={logout}>Đăng xuất</button>
      </nav>
      {children}
    </>
  );
}

export function ErrorText({ message }: { message: string | null }) {
  return message ? (
    <p role="alert" style={{ color: "#b00020" }}>
      {message}
    </p>
  ) : null;
}

export function PasswordFields() {
  return (
    <>
      <label>
        Mật khẩu mới (tối thiểu 12 ký tự)
        <br />
        <input name="password" type="password" autoComplete="new-password" required minLength={12} style={{ width: "100%" }} />
      </label>
      <label>
        Nhập lại mật khẩu
        <br />
        <input name="confirm" type="password" autoComplete="new-password" required style={{ width: "100%" }} />
      </label>
    </>
  );
}
