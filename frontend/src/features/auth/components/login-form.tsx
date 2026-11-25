"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { FieldMessage, Input, Label } from "@/components/ui/form-controls";

import { login, safeNextPath } from "../api";

const schema = z.object({
  username: z.string().trim().min(1, "Enter your username"),
  password: z.string().min(1, "Enter your password"),
});
type Values = z.infer<typeof schema>;

const FALLBACK_ERRORS: Partial<Record<string, string>> = {
  invalid: "Incorrect username or password.",
  throttled: "Too many attempts. Wait a minute and try again.",
};

export function LoginForm() {
  const router = useRouter();
  const search = useSearchParams();
  const next = search.get("next");
  // Set by the no-JavaScript fallback (a native POST answered with a redirect).
  const [error, setError] = useState<string | null>(() => FALLBACK_ERRORS[search.get("error") ?? ""] ?? null);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema) });

  const onSubmit = handleSubmit(async ({ username, password }) => {
    setError(null);
    const result = await login(username, password);
    if (result.ok) {
      router.replace(safeNextPath(next));
      router.refresh();
    } else {
      setError(result.message);
    }
  });

  return (
    // method/action matter only if JavaScript failed to load: the browser then
    // POSTs the form (credentials never end up in the URL) and the route redirects.
    <form method="post" action="/api/auth/login" onSubmit={onSubmit} noValidate className="space-y-4">
      <input type="hidden" name="next" value={next ?? ""} />
      <div className="space-y-1">
        <Label htmlFor="username">Username</Label>
        <Input
          id="username"
          autoComplete="username"
          aria-invalid={errors.username ? true : undefined}
          aria-describedby={errors.username ? "username-error" : undefined}
          {...register("username")}
        />
        {errors.username ? <FieldMessage id="username-error">{errors.username.message}</FieldMessage> : null}
      </div>
      <div className="space-y-1">
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          type="password"
          autoComplete="current-password"
          aria-invalid={errors.password ? true : undefined}
          aria-describedby={errors.password ? "password-error" : undefined}
          {...register("password")}
        />
        {errors.password ? <FieldMessage id="password-error">{errors.password.message}</FieldMessage> : null}
      </div>
      <p role="alert" aria-live="assertive" className="text-destructive min-h-5 text-sm">
        {error}
      </p>
      <Button type="submit" className="w-full" disabled={isSubmitting}>
        {isSubmitting ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
