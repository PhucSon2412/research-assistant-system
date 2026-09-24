"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import { ErrorText } from "../components/session";

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", { method: "POST", body: { email: form.get("email"), password: form.get("password") } });
      router.replace("/");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ maxWidth: 420 }}>
      <h1>Đăng nhập</h1>
      <form onSubmit={submit} style={{ display: "grid", gap: "0.75rem" }}>
        <label>
          Email
          <br />
          <input name="email" type="email" autoComplete="username" required style={{ width: "100%" }} />
        </label>
        <label>
          Mật khẩu
          <br />
          <input name="password" type="password" autoComplete="current-password" required style={{ width: "100%" }} />
        </label>
        <button type="submit" disabled={busy}>
          Đăng nhập
        </button>
      </form>
      <ErrorText message={error} />
      <p>
        <Link href="/forgot-password">Quên mật khẩu?</Link>
      </p>
      <p style={{ color: "#555" }}>Tài khoản chỉ được tạo qua lời mời của Admin.</p>
    </main>
  );
}
