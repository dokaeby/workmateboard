import { Badge } from "../../shared/ui";
import { RUN_STATUS_TONE } from "./bits-helpers";

export function StatusBadge({ status }: { status?: string }) {
  return <Badge tone={RUN_STATUS_TONE[status || ""] ?? "muted"}>{status || "-"}</Badge>;
}
