"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../../lib/api";
import { RELATION_NOTE, citationStatusText, referenceLocator, type Reference } from "../../../lib/citations";
import { makeRagBody } from "../../../lib/rag";
import { NO_DOWNLOADABLE_FILE, commonItemNote, originalDownloadHref, type ItemFile } from "../../../lib/files";
import { ReviewRequests } from "../../components/review-request";
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
type FileInfo = ItemFile;
type JobInfo = { id: string; state: string; origin: string; attempt_count: number;
  last_error_code: string | null; warnings: string[];
  extraction_finished_at: string | null; finished_at: string | null };
type RagCitation = { chunk_id: string; quote: string; url: string;
  item_id: string; title: string | null; version_id: string; source_file_id: string;
  locator_kind: string; start_page: number | null; start_line: number | null };
type RagAnswer = { status: "answered" | "insufficient_evidence"; coverage: string | null;
  reason?: "comparison_requires_multiple_papers" | "keyword_query_without_terms";
  scope: { kind: "item" | "collection" | "all"; item_id?: string; collection_id?: string };
  claims: { text: string; citations: RagCitation[] }[] };
type Collection = { id: string; name: string };

const stateText: Record<string, string> = {
  received: "Đã nhận file, chờ xử lý",
  validating: "Đang kiểm tra file",
  stored: "Đã xác minh file gốc",
  extracting: "Đang trích xuất Markdown",
  chunking: "Đang chia đoạn",
  awaiting_index: "Đã trích xuất, chờ lập chỉ mục",
  duplicate: "Cảnh báo trùng file trong kho riêng của bạn",
  retryable_failed: "Lỗi tạm thời, sẽ thử lại",
  terminal_failed: "Không thể xử lý file",
  fulltext_rejected: "Không có phần thân đủ để xử lý toàn văn",
  cancelled: "Đã hủy xử lý",
};
const issueText: Record<string, string> = {
  duplicate_in_private_library: "Chỉ là cảnh báo trùng: chưa liên kết với bản khác hoặc dùng lại nội dung đã trích xuất; bản gốc vẫn được giữ.",
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
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<"keyword" | "hybrid">("keyword");
  const [ragScope, setRagScope] = useState("item");
  const [collections, setCollections] = useState<Collection[]>([]);
  const [rag, setRag] = useState<RagAnswer | null>(null);
  const [ragError, setRagError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [references, setReferences] = useState<Reference[] | null>(null);
  const [referencesError, setReferencesError] = useState<string | null>(null);
  const ownPrivate = item?.visibility === "private" && item.owned_by_me;

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

  useEffect(() => {
    if (!user) return;
    api<{ collections: Collection[] }>("/collections")
      .then((data) => setCollections(data.collections))
      .catch((err) => setRagError(describeError(err)));
  }, [user]);

  function loadReferences() {
    api<{ references: Reference[] }>(`/library/items/${encodeURIComponent(id)}/references`)
      .then((data) => { setReferences(data.references); setReferencesError(null); })
      .catch((err) => setReferencesError(describeError(err)));
  }

  useEffect(() => {
    if (user && ownPrivate) loadReferences();
  }, [user, id, ownPrivate]);

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

  async function ask(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setAsking(true);
    setRag(null);
    setRagError(null);
    try {
      setRag(await api<RagAnswer>("/rag/ask", { method: "POST",
        body: makeRagBody(ragScope, id, question, mode) }));
    } catch (err) {
      setRagError(describeError(err));
    } finally {
      setAsking(false);
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
            {item.content_status !== "indexed" ? "chỉ metadata"
              : item.visibility === "common" ? "đã lập chỉ mục, chưa mở tìm kiếm/hỏi đáp" : "đã index"}
          </p>
          {item.visibility === "common" && (
            <p style={{ background: "#f3f3f3", padding: "0.5rem" }}>{commonItemNote(item.files, item.content_status)}</p>
          )}
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
            {j.extraction_finished_at && ` · trích xuất xong ${new Date(j.extraction_finished_at).toLocaleString("vi-VN")}`}
            {j.finished_at && ` · job kết thúc ${new Date(j.finished_at).toLocaleString("vi-VN")}`}
            {j.last_error_code && ` · lỗi: ${issueText[j.last_error_code] ?? j.last_error_code}`}
            {j.warnings.length > 0 && ` · cảnh báo: ${j.warnings.map((w) => issueText[w] ?? w).join(", ")}`}
          </li>)}</ul>
          <h2>File gốc</h2>
          {/* Only files the API listed after its own permission check; no list, no link. */}
          {item.files.length === 0 && <p>{NO_DOWNLOADABLE_FILE}</p>}
          <ul>{item.files.map((f) => {
            const href = originalDownloadHref(f.original_url);
            return <li key={f.id}>
              {href ? <a href={href}>{f.filename ?? "Original"}</a> : (f.filename ?? "Original")}
              {` · ${f.byte_size} byte`}{f.page_count ? ` · ${f.page_count} trang` : ""}
            </li>;
          })}</ul>
          {ownPrivate && user?.role !== "admin" && <ReviewRequests itemId={item.id} files={item.files}
            versions={item.versions} />}
          {ownPrivate && <section>
            <h2>Tài liệu tham khảo được trích xuất</h2>
            <p>{RELATION_NOTE} Chỉ liên kết khi citation có đúng một arXiv ID; không đoán theo tiêu đề.
              Danh mục tham khảo không được dùng làm bằng chứng cho câu trả lời.</p>
            <button type="button" onClick={loadReferences}>Tải lại</button>
            <ErrorText message={referencesError} />
            {references && references.length === 0 && <p>Chưa có tài liệu tham khảo được trích xuất.</p>}
            {references && references.length > 0 && <ol>{references.map((ref) => {
              const link = ref.citation_link;
              return <li key={ref.id} style={{ marginBottom: "0.8rem" }}>
                <span style={{ whiteSpace: "pre-wrap" }}>{ref.raw_citation.trim()}</span><br />
                <small>{referenceLocator(ref)} · Version {ref.version_id} · File {ref.source_file_id}</small><br />
                <small>{citationStatusText(link.status)}
                  {link.arxiv_id && ` · arXiv ${link.arxiv_id}`}
                  {link.cited_version && ` · version được trích: ${link.cited_version}`}</small>
                {link.target && <><br />
                  Trích dẫn → <strong>{link.target.title}</strong> <span>[metadata-only]</span>{" "}
                  <a href={link.target.original_url} target="_blank" rel="noopener noreferrer">Mở trang gốc arXiv</a>
                  {link.target.cited_version_url && <>{" · "}<a href={link.target.cited_version_url} target="_blank"
                    rel="noopener noreferrer">bản {link.cited_version} được trích</a></>}
                  {link.target.latest_metadata_version && <small>{` · version mới nhất trong metadata: ${link.target.latest_metadata_version}`}</small>}
                </>}
              </li>;
            })}</ol>}
          </section>}
          {item.visibility === "private" && item.content_status === "indexed" && <section>
            <h2>Hỏi đáp có dẫn nguồn</h2>
            <p>Câu hỏi và các đoạn được chọn sẽ gửi qua OpenRouter/provider để tạo câu trả lời.</p>
            <form onSubmit={ask}>
              <label>Phạm vi <select value={ragScope} onChange={(e) => { setRagScope(e.target.value); setRag(null); }}>
                <option value="item">Paper này</option>
                <option value="all">Tất cả paper đã index tôi có quyền</option>
                {collections.map((collection) => <option key={collection.id} value={collection.id}>
                  Collection: {collection.name}</option>)}
              </select></label>{" "}
              <label htmlFor="rag-question">Câu hỏi</label>{" "}
              <input id="rag-question" value={question} onChange={(e) => setQuestion(e.target.value)}
                required maxLength={1024} style={{ width: "min(100%, 36rem)" }} />{" "}
              <select aria-label="Cách tìm bằng chứng" value={mode}
                onChange={(e) => setMode(e.target.value as "keyword" | "hybrid")}>
                <option value="keyword">Từ khóa</option><option value="hybrid">Kết hợp</option>
              </select>{" "}
              <button type="submit" disabled={asking}>{asking ? "Đang hỏi…" : "Hỏi"}</button>
            </form>
            <ErrorText message={ragError} />
            {rag && <div aria-live="polite">
              <p>Phạm vi đã hỏi: {rag.scope.kind === "item" ? "paper này" : rag.scope.kind === "all"
                ? "tất cả paper đã index có quyền" : `collection ${collections.find((c) => c.id === rag.scope.collection_id)?.name ?? rag.scope.collection_id}`}.</p>
              <p>Trạng thái: {rag.status === "answered" ? "Có câu trả lời" : "Chưa đủ bằng chứng"}.
                {rag.coverage === "partial" && " File chỉ được trích xuất một phần."}</p>
              {rag.reason === "comparison_requires_multiple_papers" &&
                <p>Chưa có nguồn từ ít nhất hai paper để so sánh.</p>}
              {rag.reason === "keyword_query_without_terms" &&
                <p>Câu hỏi chỉ gồm từ phổ biến nên chế độ từ khóa không có thuật ngữ để tìm; hãy thêm thuật ngữ cụ thể.</p>}
              {rag.claims.map((claim, index) => <p key={index}>
                {claim.text}{" "}
                {claim.citations.map((citation) => <span key={citation.chunk_id}>
                  <Link href={citation.url}>[{citation.title || citation.item_id} · {citation.locator_kind === "pdf" ? `trang ${citation.start_page}` : `dòng ${citation.start_line}`}]</Link>{" "}
                  <q>{citation.quote}</q>{" "}
                  <small>Version {citation.version_id} · File {citation.source_file_id}</small>{" "}
                </span>)}
              </p>)}
            </div>}
          </section>}
        </>
      )}
    </Shell>
  );
}
