import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { appQueryClient } from "./queryClient";
import { appRouter } from "./routeConfig";
import { OperationProvider } from "./OperationContext";

export function AppProviders() {
  return (
    <QueryClientProvider client={appQueryClient}>
      <OperationProvider>
        <RouterProvider router={appRouter} />
      </OperationProvider>
    </QueryClientProvider>
  );
}
