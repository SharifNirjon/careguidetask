"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import type { SearchMode } from "@/lib/api/types";

import { search, searchKeys, type SearchArgs } from "./api";
import { autoSearchDelay, canSubmit } from "./schemas";

/**
 * The query that actually runs ("committed") lags the input: after a pause
 * (300 ms keyword, 600 ms semantic/hybrid) or immediately on Enter. Semantic
 * and hybrid never run for fewer than 3 characters, because every new query
 * spends embedding quota.
 */
export function useCommittedQuery(input: string, mode: SearchMode, initial: string) {
  const [committed, setCommitted] = useState(initial);

  useEffect(() => {
    const delay = autoSearchDelay(mode, input);
    if (delay === null || input.trim() === committed) {
      return;
    }
    const timer = setTimeout(() => {
      setCommitted(input.trim());
    }, delay);
    return () => {
      clearTimeout(timer);
    };
  }, [input, mode, committed]);

  const submit = useCallback(() => {
    if (canSubmit(mode, input)) {
      setCommitted(input.trim());
      return true;
    }
    return false;
  }, [input, mode]);

  return { committed, submit };
}

export function useSearch(args: SearchArgs) {
  return useQuery({
    queryKey: searchKeys.query(args),
    queryFn: () => search(args),
    enabled: canSubmit(args.mode, args.q),
    placeholderData: keepPreviousData,
    staleTime: 60_000, // re-running an identical semantic query costs quota for nothing
  });
}
