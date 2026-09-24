import type { ReactNode } from "react";

export const metadata = {
  title: "Research Assistant System",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="vi">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: "2rem", lineHeight: 1.5 }}>{children}</body>
    </html>
  );
}
