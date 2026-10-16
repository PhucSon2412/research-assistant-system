"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../../../lib/api";
import { RELATION_NOTE, citationStatusText, referenceLocator, type Reference } from "../../../lib/citations";
import { RAG_ALL_SCOPE_OPTION, RAG_NOTICE, makeRagBody, ragReasonText, ragScopeText, ragWithheldText } from "../../../lib/rag";
import { NO_DOWNLOADABLE_FILE, commonItemNote, contentLabel, originalDownloadHref, sourceOriginalHref,
  type ItemFile } from "../../../lib/files";
import { sourceKindLabel } from "../../../lib/search";
import { ReviewRequests } from "../../components/review-request";
import { ErrorText, Shell, useSession } from "../../components/session";
import type { Item } from "../../page";
import { DeletePrivate } from "../../components/private-delete";
import { deletionStatus, type DeletionReceipt } from "../../../lib/private-delete";

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
// `original_url` is set only when the download route's own gate allows it (RAG rights never do).
type RagCitation = { chunk_id: string; quote: string; url: string;
  item_id: string; title: string | null; version_id: string; source_file_id: string;
  locator_kind: string; start_page: number | null; start_line: number | null;
  visibility: "private" | "common"; original_url: string | null };
type RagAnswer = { status: "answered" | "insufficient_evidence"; coverage: string | null;
  reason?: string; evidence_withheld?: number;
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
  const [deletion, setDeletion] = useState<DeletionReceipt | null>(null);
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
  // Derived reads (references) open for the owner's private item, or a common copy the API says is searchable.
  const readsDerived = ownPrivate || (item?.visibility === "common" && item.fulltext_search_available);

  useEffect(() => {
    setDeletion(null); setItem(null); setError(null); setRag(null); setReferences(null);
  }, [id]);

  useEffect(() => {
    if (!user || deletion) return;
    let active = true;
    const refresh = () => api<{ item: Detail & { files: FileInfo[]; jobs: JobInfo[] } }>(`/library/items/${encodeURIComponent(id)}`)
      .then((d) => { if (active) setItem(d.item); })
      .catch((err) => { if (active) setError(describeError(err)); });
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [user, id, deletion]);

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
    if (user && readsDerived) loadReferences();
  }, [user, id, readsDerived]);

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
      {deletion && <p role="status">{deletionStatus(deletion)}</p>}
      {ownPrivate && !deletion && <DeletePrivate itemId={id} onDeleted={(receipt) => {
        setDeletion(receipt); setItem(null); setRag(null); setReferences(null); setError(null);
      }} />}
      {item && (
        <>
          <h1>{item.title ?? "(chưa có tiêu đề)"}</h1>
          <p>
            Kho: {item.visibility === "common" ? "chung" : "riêng của bạn"} · Nội dung: {contentLabel(item)}
          </p>
          {item.visibility === "common" && (
            <p style={{ background: "#f3f3f3", padding: "0.5rem" }}>
              {commonItemNote(item.files, item.content_status, item.fulltext_search_available, item.rag_available)}</p>
          )}
          {item.fulltext_search_available && (
            <p><Link href={`/search?item_id=${encodeURIComponent(item.id)}`}>Tìm trong paper này</Link></p>
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
          {readsDerived && <section>
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
          {/* The API's RAG gate for this item; the server still checks rights (and pricing) on every request. */}
          {item.rag_available && <section>
            <h2>Hỏi đáp có dẫn nguồn</h2>
            <p>{RAG_NOTICE}</p>
            <form onSubmit={ask}>
              <label>Phạm vi <select value={ragScope} onChange={(e) => { setRagScope(e.target.value); setRag(null); }}>
                <option value="item">Paper này</option>
                <option value="all">{RAG_ALL_SCOPE_OPTION}</option>
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
              <p>Phạm vi đã hỏi: {ragScopeText(rag.scope.kind, rag.scope.kind === "collection"
                ? collections.find((c) => c.id === rag.scope.collection_id)?.name ?? rag.scope.collection_id : undefined)}.</p>
              <p>Trạng thái: {rag.status === "answered" ? "Có câu trả lời" : "Chưa đủ bằng chứng"}.
                {rag.coverage === "partial" && " File chỉ được trích xuất một phần."}</p>
              {ragReasonText(rag.reason) && <p>{ragReasonText(rag.reason)}</p>}
              {ragWithheldText(rag.evidence_withheld) && <p><small>{ragWithheldText(rag.evidence_withheld)}</small></p>}
              {rag.claims.map((claim, index) => <p key={index}>
                {claim.text}{" "}
                {claim.citations.map((citation) => {
                  const originalHref = sourceOriginalHref(citation.original_url, citation.locator_kind, citation.start_page);
                  return <span key={citation.chunk_id}>
                    <Link href={citation.url}>[{citation.title || citation.item_id} · {citation.locator_kind === "pdf" ? `trang ${citation.start_page}` : `dòng ${citation.start_line}`}]</Link>{" "}
                    <q>{citation.quote}</q>{" "}
                    <small>{sourceKindLabel(citation.visibility)} · Version {citation.version_id} · File {citation.source_file_id}
                      {originalHref && <> · <a href={originalHref}>file gốc</a></>}</small>{" "}
                  </span>;
                })}
              </p>)}
            </div>}
          </section>}
        </>
      )}
    </Shell>
  );
}
