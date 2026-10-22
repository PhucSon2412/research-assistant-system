"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api, describeError } from "../../lib/api";
import { ErrorText } from "../components/session";


import { AuthFrame } from "../components/ui";

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
    <AuthFrame>
      <h1>Quên mật khẩu</h1>
      {sent ? (
        // Same message whether or not the account exists.
        <p>
          Yêu cầu đã được tiếp nhận; đây chưa phải xác nhận email đã gửi thành công. Nếu nhận được email, liên kết đặt
          lại mật khẩu dùng một lần và có thời hạn.
        </p>
      ) : (
        <form onSubmit={submit} className="form-stack">
          <label>
            Email
            <br />
            <input name="email" type="email" required className="full-width" />
          </label>
          <button type="submit">Gửi liên kết</button>
        </form>
      )}
      <ErrorText message={error} />
      <p>
        <Link href="/login">Quay lại đăng nhập</Link>
      </p>
    </AuthFrame>
  );
}
