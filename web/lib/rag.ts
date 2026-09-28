export function makeRagBody(scope: string, itemId: string, question: string,
                            mode: "keyword" | "hybrid") {
  const selected = scope === "item" ? { item_id: itemId }
    : scope === "all" ? { scope: "all" } : { collection_id: scope };
  return { ...selected, question, mode };
}
