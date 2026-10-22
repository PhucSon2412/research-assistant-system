"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError, type User } from "../../lib/api";
import { Brand, Icon, Loading } from "./ui";

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
  const pathname = usePathname();
  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      router.replace("/login");
    }
  }
  if (!user) return <Loading />;
  const links = [
    { href: "/", label: "Thư viện", icon: "library" },
    { href: "/search", label: "Tìm kiếm & hỏi đáp", icon: "search" },
    { href: "/collections", label: "Bộ sưu tập", icon: "collection" },
    { href: "/discovery", label: "Khám phá", icon: "discovery" },
    ...(user.role === "admin" ? [{ href: "/admin", label: "Quản trị", icon: "admin" }] : []),
  ];
  return (
    <div className="app">
      <a className="skip-link" href="#main-content">Chuyển đến nội dung</a>
      <aside className="sidebar">
        <Link className="brand-link" href="/" aria-label="Research Assistant — Thư viện"><Brand /></Link>
        <div><p className="nav-label">Không gian làm việc</p><nav className="main-nav" aria-label="Điều hướng chính">
          {links.map((link) => <Link key={link.href} href={link.href} aria-current={(link.href === "/" ? pathname === "/" || pathname.startsWith("/items/") : pathname.startsWith(link.href)) ? "page" : undefined}><Icon name={link.icon} />{link.label}</Link>)}
        </nav></div>
        <div className="sidebar-bottom"><nav className="main-nav" aria-label="Tài khoản"><Link href="/account" aria-current={pathname === "/account" ? "page" : undefined}><Icon name="account" />Phiên đăng nhập</Link></nav><p>Tài liệu của bạn.<br />Bằng chứng cho nghiên cứu.</p></div>
      </aside>
      <div className="workspace"><header className="topbar"><span className="topbar-label">RESEARCH ASSISTANT SYSTEM</span><div className="account"><span className="avatar" aria-hidden="true">{user.email.slice(0, 1).toUpperCase()}</span><Link href="/account">{user.email}<small>{user.role === "admin" ? "Admin" : "User"} · Phiên đăng nhập</small></Link></div><button className="button-secondary" onClick={logout}>Đăng xuất</button></header>
        <main id="main-content" className="page-content" tabIndex={-1}>{children}</main>
      </div>
    </div>
  );
}

export function ErrorText({ message }: { message: string | null }) {
  return message ? (
    <p role="alert">
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
        <input name="password" type="password" autoComplete="new-password" required minLength={12} />
      </label>
      <label>
        Nhập lại mật khẩu
        <br />
        <input name="confirm" type="password" autoComplete="new-password" required />
      </label>
    </>
  );
}
