import { setupServer } from "msw/node";

/** One MSW server for every test; each test adds its own handlers with server.use(). */
export const server = setupServer();

export const API = "http://localhost:3000/api/proxy/api/v1";
