"use client";

import { useQueryClient } from "@tanstack/react-query";
import { LogOutIcon } from "lucide-react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";

import { logout } from "../api";

export function LogoutButton() {
  const router = useRouter();
  const queryClient = useQueryClient();
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={async () => {
        await logout();
        queryClient.clear(); // no cached invoice data survives the session
        router.replace("/login");
        router.refresh();
      }}
    >
      <LogOutIcon aria-hidden="true" /> Sign out
    </Button>
  );
}
