import { useQuery } from "@tanstack/react-query";
import { useRef } from "react";

import { queryKeys } from "./queryKeys";

const visiblePageRefreshIntervalMs = 30_000;

export function useVisiblePageRefresh(view: string, refresh: () => Promise<void>) {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useQuery({
    initialData: 0,
    queryFn: async () => {
      await refreshRef.current();
      return Date.now();
    },
    queryKey: queryKeys.pageRefresh(view),
    refetchInterval: visiblePageRefreshIntervalMs,
    refetchIntervalInBackground: false,
    staleTime: visiblePageRefreshIntervalMs,
  });
}
