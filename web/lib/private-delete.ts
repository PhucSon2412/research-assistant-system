import { ApiError, describeError } from "./api.ts";

export type DeletionReceipt = { item_id: string; deleted: true; purge_due_at: string;
  cleanup_status: "pending" | "completed" };

export function deletionError(error: unknown): string {
  if (!(error instanceof ApiError) || error.status >= 500) {
    return "Chưa xác định được kết quả xóa. Item có thể đã bị đóng. Hãy tải lại thư viện để kiểm trạng thái; không tự gửi lại yêu cầu xóa.";
  }
  return describeError(error);
}

export function deletionStatus(receipt: DeletionReceipt): string {
  return receipt.cleanup_status === "completed"
    ? "Đã xóa toàn item riêng và hoàn tất cleanup."
    : "Đã xóa toàn item khỏi thư viện và chặn truy cập. File và dữ liệu còn lại đang chờ cleanup; hạn dọn: " + receipt.purge_due_at;
}
