"use client";

import { Tabs as TabsPrimitive, Tooltip as TooltipPrimitive } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return <div aria-hidden="true" className={cn("bg-muted animate-pulse rounded-md", className)} {...props} />;
}

export function Kbd({ className, ...props }: ComponentProps<"kbd">) {
  return (
    <kbd
      className={cn("border-border bg-muted rounded border px-1.5 py-0.5 font-mono text-xs", className)}
      {...props}
    />
  );
}

export function Card({ className, ...props }: ComponentProps<"section">) {
  return (
    <section className={cn("border-border bg-background rounded-lg border p-4", className)} {...props} />
  );
}

export const TooltipProvider = TooltipPrimitive.Provider;

/** Hover AND keyboard focus show the tooltip (Radix), so the information is never mouse-only. */
export function Tip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={4}
          className="bg-foreground text-background z-50 rounded-md px-2 py-1 text-xs shadow"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

export const Tabs = TabsPrimitive.Root;

export function TabsList({ className, ...props }: ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn("bg-muted inline-flex h-9 items-center gap-1 rounded-md p-1", className)}
      {...props}
    />
  );
}

export function TabsTrigger({ className, ...props }: ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        "focus-visible:outline-ring data-[state=active]:bg-background rounded px-3 py-1 text-sm font-medium focus-visible:outline-2 data-[state=active]:shadow-xs",
        className,
      )}
      {...props}
    />
  );
}

export const TabsContent = TabsPrimitive.Content;
