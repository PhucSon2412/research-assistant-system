// M1a placeholder. Login, library and upload arrive in M1b/M1c; the web
// app never decides permissions and holds no backend secrets.

export const dynamic = "force-dynamic";

async function apiStatus(): Promise<string> {
  const base = process.env.API_INTERNAL_URL;
  if (!base) return "chưa cấu hình API_INTERNAL_URL";
  try {
    const res = await fetch(`${base}/readyz`, { cache: "no-store", signal: AbortSignal.timeout(3000) });
    return res.ok ? "sẵn sàng" : `không sẵn sàng (HTTP ${res.status})`;
  } catch {
    return "không kết nối được";
  }
}

export default async function Home() {
  const status = await apiStatus();
  return (
    <main>
      <h1>Research Assistant System</h1>
      <p>Môi trường phát triển M1a: nền dữ liệu và bootstrap. Chưa có đăng nhập, thư viện hay upload.</p>
      <p>
        Trạng thái API: <strong>{status}</strong>
      </p>
    </main>
  );
}
