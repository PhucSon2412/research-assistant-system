"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import { ErrorText } from "../components/session";

export default function ForgotPasswordPage() {
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setError(null);
    try {
      await api("/auth/password-reset/request", { method: "POST", body: { email: form.get("email") } });
      setSent(true);
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <main style={{ maxWidth: 420 }}>
      <h1>Quên mật khẩu</h1>
      {sent ? (
        // Same message whether or not the account exists.
        <p>
          Nếu email thuộc một tài khoản đang hoạt động, liên kết đặt lại mật khẩu sẽ được gửi tới đó. Liên kết dùng một
          lần và có thời hạn.
        </p>
      ) : (
        <form onSubmit={submit} style={{ display: "grid", gap: "0.75rem" }}>
          <label>
            Email
            <br />
            <input name="email" type="email" required style={{ width: "100%" }} />
          </label>
          <button type="submit">Gửi liên kết</button>
        </form>
      )}
      <ErrorText message={error} />
      <p>
        <Link href="/login">Quay lại đăng nhập</Link>
      </p>
    </main>
  );
}
