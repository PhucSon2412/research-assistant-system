"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../../lib/api";
import { ErrorText, Shell, useSession } from "../../components/session";
import type { Item } from "../../page";

type Version = {
  id: string;
  version_label: string | null;
  source_identifier: string | null;
  source_url: string | null;
  coverage: string | null;
  received_at: string;
};
type Detail = Item & { versions: Version[] };
type FileInfo = { id: string; filename: string | null; byte_size: number; verified_mime: string; page_count: number | null };
type JobInfo = { id: string; state: string; origin: string; attempt_count: number;
  last_error_code: string | null; warnings: string[] };

const stateText: Record<string, string> = {
  received: "Đã nhận file, chờ xử lý",
  validating: "Đang kiểm tra file",
  stored: "Đã xác minh file gốc",
  extracting: "Đang trích xuất Markdown",
  chunking: "Đang chia đoạn",
  awaiting_index: "Đã trích xuất, chờ lập chỉ mục",
  duplicate: "Trùng file trong kho riêng của bạn",
  retryable_failed: "Lỗi tạm thời, sẽ thử lại",
  terminal_failed: "Không thể xử lý file",
  fulltext_rejected: "Không có phần thân đủ để xử lý toàn văn",
  cancelled: "Đã hủy xử lý",
};
const issueText: Record<string, string> = {
  duplicate_in_private_library: "File này đã được nhận trong kho riêng của bạn; bản gốc vẫn được giữ.",
  storage_unavailable: "Chưa đọc được file gốc; hệ thống sẽ thử lại.",
  checksum_mismatch: "File gốc không khớp bản đã nhận.",
  invalid_markdown: "Markdown không hợp lệ.",
  no_extractable_text: "Không tìm thấy nội dung có thể trích xuất.",
  no_body_text: "Chỉ có tiêu đề, tóm tắt hoặc danh mục tài liệu; thiếu nội dung thân bài.",
  processing_unavailable: "Xử lý tạm thời thất bại; hệ thống sẽ thử lại nếu còn lượt.",
  attempts_exhausted: "Đã hết số lần thử xử lý.",
  cancelled: "Quyền hoặc yêu cầu xử lý đã thay đổi.",
};

export default function ItemPage() {
  const user = useSession();
  const { id } = useParams<{ id: string }>();
  const [item, setItem] = useState<(Detail & { files: FileInfo[]; jobs: JobInfo[] }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!user) return;
    let active = true;
    const refresh = () => api<{ item: Detail & { files: FileInfo[]; jobs: JobInfo[] } }>(`/library/items/${encodeURIComponent(id)}`)
      .then((d) => { if (active) setItem(d.item); })
      .catch((err) => { if (active) setError(describeError(err)); });
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [user, id]);

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const title = new FormData(e.currentTarget).get("title");
    setSaved(false);
    try {
      const d = await api<{ item: Item }>(`/library/items/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: { title },
      });
      setItem((prev) => (prev ? { ...prev, ...d.item } : prev));
      setSaved(true);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <Shell user={user}>
      <p>
        <Link href="/">← Thư viện</Link>
      </p>
      <ErrorText message={error} />
      {item && (
        <>
          <h1>{item.title ?? "(chưa có tiêu đề)"}</h1>
          <p>
            Kho: {item.visibility === "common" ? "chung" : "riêng của bạn"} · Nội dung:{" "}
            {item.content_status === "indexed" ? "đã index" : "chỉ metadata"}
          </p>
          {item.can_edit && (
            <form onSubmit={save} style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <input name="title" defaultValue={item.title ?? ""} maxLength={1000} style={{ flex: 1, minWidth: 200 }} />
              <button type="submit">Lưu tiêu đề</button>
              {saved && <span>Đã lưu.</span>}
            </form>
          )}
          <h2>Phiên bản</h2>
          <ul>
            {item.versions.map((v) => (
              <li key={v.id}>
                {v.version_label ?? "(không nhãn)"}
                {v.source_identifier && ` · ${v.source_identifier}`}
                {v.coverage && ` · trích xuất: ${v.coverage === "complete" ? "đầy đủ theo parser Markdown" : v.coverage === "none" ? "không có thân bài" : "một phần"}`} · nhận {new Date(v.received_at).toLocaleString("vi-VN")}
              </li>
            ))}
          </ul>
          <h2>Tiến trình xử lý</h2>
          <ul>{item.jobs.map((j) => <li key={j.id}>
            {stateText[j.state] ?? j.state}
            {` · lần thử: ${j.attempt_count}`}
            {j.last_error_code && ` · lỗi: ${issueText[j.last_error_code] ?? j.last_error_code}`}
            {j.warnings.length > 0 && ` · cảnh báo: ${j.warnings.map((w) => issueText[w] ?? w).join(", ")}`}
          </li>)}</ul>
          <h2>File gốc</h2>
          <ul>{item.files.map((f) => <li key={f.id}>
            <a href={`/api/files/${encodeURIComponent(f.id)}/original`}>{f.filename ?? "Original"}</a>
            {` · ${f.byte_size} byte`}{f.page_count ? ` · ${f.page_count} trang` : ""}
          </li>)}</ul>
        </>
      )}
    </Shell>
  );
}
