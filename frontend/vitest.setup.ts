import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { resetApiClient } from "@/lib/api/client";
import { server } from "@/test/server";

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" }); // a request no test expected is a failure
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  resetApiClient();
});

afterAll(() => {
  server.close();
});
