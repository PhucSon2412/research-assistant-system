"use client";

import { useRef, type ReactNode, type ButtonHTMLAttributes } from "react";
import styles from "./ui.module.css";

export function Icon({ name, className = "" }: { name: string; className?: string }) {
  const paths: Record<string, ReactNode> = {
    library: <><path d="M4 5h4v15H4zM10 3h4v17h-4zM16 6l4-1 3 14-4 1z" /></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
    collection: <><path d="M3 7h18v13H3zM3 7V4h7l2 3" /><path d="M8 12h8M8 16h5" /></>,
    discovery: <><circle cx="12" cy="12" r="9" /><path d="m16 8-2 6-6 2 2-6z" /></>,
    admin: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z" /><path d="m8 12 3 3 5-6" /></>,
    account: <><circle cx="12" cy="8" r="4" /><path d="M4 21v-2a8 8 0 0 1 16 0v2" /></>,
    arrow: <><path d="M4 12h16m-6-6 6 6-6 6" /></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" /></>,
    file: <><path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8M8 17h5" /></>,
    quote: <><path d="M5 6h5v7H5l-2 5M15 6h5v7h-5l-2 5" /></>,
  };
  return <svg className={`${styles.icon} ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.file}</svg>;
}

export function Brand() {
  return <span className={styles.brand}><span className={styles.brandMark}><Icon name="library" /></span><span>Research Assistant<small>Không gian nghiên cứu</small></span></span>;
}

export function PageHeading({ eyebrow, title, children }: { eyebrow: string; title: string; children?: ReactNode }) {
  return <header className={styles.pageHeading}><p className={styles.eyebrow}>{eyebrow}</p><h1>{title}</h1>{children && <p className={styles.description}>{children}</p>}</header>;
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "success" | "warning" | "danger" }) {
  return <span className={`${styles.badge} ${styles[tone]}`}>{children}</span>;
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return <div className={styles.empty}><Icon name="file" /><strong>{title}</strong>{children && <p>{children}</p>}</div>;
}

export function Loading() {
  return <div className={styles.loading} role="status" aria-live="polite"><span /><span /><span /><p>Đang tải…</p></div>;
}

export function AuthFrame({ children }: { children: ReactNode }) {
  return <main className={styles.auth}><aside className={styles.authStory}><Brand /><div><p className={styles.eyebrow}>Từ tài liệu đến hiểu biết</p><h2>Nghiên cứu có cơ sở.<br /><em>Câu trả lời có nguồn.</em></h2><p>Lưu trữ paper, tìm kiếm toàn văn và hỏi đáp gắn với từng đoạn bằng chứng.</p><div className={styles.paperArt} aria-hidden="true"><span>01 / TÀI LIỆU</span><i /><i /><i /><blockquote>Ý tưởng bắt đầu từ một câu hỏi.</blockquote><span>02 / BẰNG CHỨNG → 03 / HIỂU BIẾT</span></div></div><small>Research Assistant System</small></aside><div className={styles.authBody}><div className={styles.authCard}><div className={styles.mobileBrand}><Brand /></div>{children}</div><p className={styles.authFooter}>Không gian dành cho nghiên cứu, kết nối bằng bằng chứng.</p></div></main>;
}

// Native dialog handles focus trapping, Escape and focus return. The existing
// callback is invoked only after this presentation-level confirmation.
export function ConfirmButton({ children, message, onClick, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { message: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  return <><button {...props} type="button" onClick={() => dialog.current?.showModal()}>{children}</button><dialog ref={dialog} className={styles.dialog} aria-labelledby={props.id ? `${props.id}-title` : undefined} aria-label="Xác nhận thao tác"><h2 id={props.id ? `${props.id}-title` : undefined}>Xác nhận thao tác</h2><p>{message}</p><div className="actions"><button type="button" className="button-secondary" autoFocus onClick={() => dialog.current?.close()}>Hủy</button><button type="button" className="button-danger" onClick={(event) => { dialog.current?.close(); onClick?.(event); }}>Xác nhận</button></div></dialog></>;
}
