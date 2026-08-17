import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { GlobalProvider } from "@ladle/react";
import { MemoryRouter } from "react-router-dom";

import "../src/shared/styles/tokens.css";
import "../src/shared/styles/global.css";
import "../src/shared/styles/legacy.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false },
    mutations: { retry: false },
  },
});

export const Provider: GlobalProvider = ({ children }) => (
  <MemoryRouter initialEntries={["/overview"]}>
    <QueryClientProvider client={queryClient}>
      <div style={{ minHeight: "100vh", padding: 24 }}>{children}</div>
    </QueryClientProvider>
  </MemoryRouter>
);
