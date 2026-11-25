import {
  AlertCircleIcon,
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleDashedIcon,
  ClockIcon,
  EyeIcon,
  InfoIcon,
  LoaderIcon,
  ThumbsUpIcon,
  XCircleIcon,
  type LucideIcon,
} from "lucide-react";

import type { DocumentStatus, ReviewStatus, Severity } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/** Every status is an icon AND text: color is never the only signal (WCAG 1.4.1). */
interface Look {
  icon: LucideIcon;
  label: string;
  tone: string;
  spin?: boolean;
}

const DOCUMENT: Record<DocumentStatus, Look> = {
  uploaded: { icon: CircleDashedIcon, label: "Uploaded", tone: "text-muted-foreground" },
  queued: { icon: ClockIcon, label: "Queued", tone: "text-muted-foreground" },
  processing: { icon: LoaderIcon, label: "Processing", tone: "text-info", spin: true },
  completed: { icon: CheckCircle2Icon, label: "Completed", tone: "text-success" },
  needs_review: { icon: EyeIcon, label: "Needs review", tone: "text-warning" },
  failed: { icon: XCircleIcon, label: "Failed", tone: "text-destructive" },
};

const REVIEW: Record<ReviewStatus, Look> = {
  pending: { icon: ClockIcon, label: "Pending", tone: "text-muted-foreground" },
  auto_approved: { icon: CheckCircle2Icon, label: "Auto-approved", tone: "text-success" },
  needs_review: { icon: EyeIcon, label: "Needs review", tone: "text-warning" },
  approved: { icon: ThumbsUpIcon, label: "Approved", tone: "text-success" },
  rejected: { icon: XCircleIcon, label: "Rejected", tone: "text-destructive" },
};

const SEVERITY: Record<Severity, Look> = {
  error: { icon: AlertCircleIcon, label: "Error", tone: "text-destructive" },
  warning: { icon: AlertTriangleIcon, label: "Warning", tone: "text-warning" },
  info: { icon: InfoIcon, label: "Info", tone: "text-info" },
};

function Badge({ look, className, suffix }: { look: Look; className?: string; suffix?: string }) {
  const Icon = look.icon;
  return (
    <span className={cn("inline-flex items-center gap-1 text-sm font-medium", look.tone, className)}>
      <Icon
        className={cn("size-4 shrink-0", look.spin && "animate-spin motion-reduce:animate-none")}
        aria-hidden="true"
      />
      <span>
        {look.label}
        {suffix}
      </span>
    </span>
  );
}

export function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  return <Badge look={DOCUMENT[status]} />;
}

export function ReviewStatusBadge({ status }: { status: ReviewStatus }) {
  return <Badge look={REVIEW[status]} />;
}

export function SeverityBadge({ severity, count }: { severity: Severity; count?: number }) {
  return <Badge look={SEVERITY[severity]} suffix={count === undefined ? undefined : `: ${count}`} />;
}

export function severityLabel(severity: Severity): string {
  return SEVERITY[severity].label;
}

export const TERMINAL_STATUSES: ReadonlySet<DocumentStatus> = new Set([
  "completed",
  "needs_review",
  "failed",
]);
