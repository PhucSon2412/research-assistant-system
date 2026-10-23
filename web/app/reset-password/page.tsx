"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, describeError, tokenFromHash } from "../../lib/api";
import { ErrorText, PasswordFields } from "../components/session";


import { AuthFrame } from "../components/ui";

export default function ResetPasswordPage() {
  const [token, setToken] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const readOnce = useRef(false);

  useEffect(() => {
    // Read once: StrictMode re-runs effects, and the hash is cleared below.
    if (readOnce.current) return;
    readOnce.current = true;
    setToken(tokenFromHash());
    // Drop the token from the address bar/history once read.
    history.replaceState(null, "", window.location.pathname);
  }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    if (form.get("password") !== form.get("confirm")) return setError("Hai mật khẩu không khớp.");
    setError(null);
    try {
      await api("/auth/password-reset/confirm", { method: "POST", body: { token, new_password: form.get("password") } });
      setDone(true);
    } catch (err) {
      setError(describeError(err));
    }
  }

  if (done) {
    return (
      <AuthFrame>
        <h1>Đã đổi mật khẩu</h1>
        <p>Mọi phiên đăng nhập cũ đã bị thu hồi.</p>
        <Link href="/login">Đăng nhập</Link>
      </AuthFrame>
    );
  }
  return (
    <AuthFrame>
      <h1>Đặt lại mật khẩu</h1>
      {token === null ? (
        <p>Thiếu mã trong liên kết.</p>
      ) : (
        <form onSubmit={submit} className="form-stack">
          <PasswordFields />
          <button type="submit">Đổi mật khẩu</button>
        </form>
      )}
      <ErrorText message={error} />
    </AuthFrame>
  );
}
