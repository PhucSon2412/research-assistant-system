"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, describeError, tokenFromHash } from "../../lib/api";
import { ErrorText, PasswordFields } from "../components/session";

import { AuthFrame } from "../components/ui";

type Invite = { email: string; display_name: string | null; expires_at: string };


export default function InvitePage() {
  const [token, setToken] = useState<string | null>(null);
  const [invite, setInvite] = useState<Invite | null>(null);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const readOnce = useRef(false);

  useEffect(() => {
    // Read once: StrictMode re-runs effects, and the hash is cleared below.
    if (readOnce.current) return;
    readOnce.current = true;
    const t = tokenFromHash();
    history.replaceState(null, "", window.location.pathname);
    setToken(t);
    if (!t) {
      setError("Thiếu mã trong liên kết.");
      return;
    }
    api<Invite>("/auth/invitations/inspect", { method: "POST", body: { token: t } })
      .then(setInvite)
      .catch((err) => setError(describeError(err)));
  }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    if (form.get("password") !== form.get("confirm")) return setError("Hai mật khẩu không khớp.");
    setError(null);
    try {
      await api("/auth/invitations/accept", {
        method: "POST",
        body: { token, password: form.get("password"), display_name: form.get("display_name") || null },
      });
      setDone(true);
    } catch (err) {
      setError(describeError(err));
    }
  }

  if (done) {
    return (
      <AuthFrame>
        <h1>Đã tạo tài khoản</h1>
        <Link href="/login">Đăng nhập</Link>
      </AuthFrame>
    );
  }
  return (
    <AuthFrame>
      <h1>Nhận lời mời</h1>
      {invite && (
        <form onSubmit={submit} className="form-stack">
          <p>
            Tài khoản: <strong>{invite.email}</strong> (vai trò User). Lời mời hết hạn lúc{" "}
            {new Date(invite.expires_at).toLocaleString("vi-VN")}.
          </p>
          <label>
            Tên hiển thị (tùy chọn)
            <br />
            <input name="display_name" defaultValue={invite.display_name ?? ""} maxLength={200} className="full-width" />
          </label>
          <PasswordFields />
          <button type="submit">Tạo tài khoản</button>
        </form>
      )}
      <ErrorText message={error} />
      {!invite && token && !error && <p>Đang kiểm tra lời mời…</p>}
    </AuthFrame>
  );
}
