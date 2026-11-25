import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

const field =
  "flex w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm shadow-xs placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(field, "h-9", className)} {...props} />;
}

/** Ctrl/Cmd+Enter submits the enclosing form (Enter alone inserts a newline). */
export function Textarea({ className, onKeyDown, ...props }: ComponentProps<"textarea">) {
  return (
    <textarea
      className={cn(field, "min-h-20", className)}
      onKeyDown={(event) => {
        onKeyDown?.(event);
        if (!event.defaultPrevented && event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          event.currentTarget.form?.requestSubmit();
        }
      }}
      {...props}
    />
  );
}

/** Native select: accessible and keyboard-friendly by default. */
export function NativeSelect({ className, ...props }: ComponentProps<"select">) {
  return <select className={cn(field, "h-9 pr-8", className)} {...props} />;
}

export function Label({ className, ...props }: ComponentProps<"label">) {
  return <label className={cn("text-sm font-medium", className)} {...props} />;
}

export function FieldMessage({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("text-destructive text-xs", className)} {...props} />;
}
