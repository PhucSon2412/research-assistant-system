import type { ReactNode } from "react";
import "./globals.css";

export const metadata = {
  title: "Research Assistant System",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="vi">
      <body>{children}</body>
    </html>
  );
}
