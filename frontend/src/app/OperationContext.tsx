import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export type OperationKind = "blocking" | "upstream-discovery";
export type OperationToken = { id: symbol; kind: OperationKind; upstreamId?: string };

type OperationContextValue = {
  blocking: boolean;
  busy: boolean;
  discoveringUpstreamIds: ReadonlySet<string>;
  start: (kind?: OperationKind, upstreamId?: string) => () => void;
};

const OperationContext = createContext<OperationContextValue | null>(null);

export function OperationProvider({ children }: { children: ReactNode }) {
  const [operations, setOperations] = useState<OperationToken[]>([]);
  const start = useCallback((kind: OperationKind = "blocking", upstreamId?: string) => {
    const token = { id: Symbol(kind), kind, upstreamId };
    setOperations((current) => [...current, token]);
    let finished = false;
    return () => {
      if (finished) return;
      finished = true;
      setOperations((current) => current.filter((item) => item.id !== token.id));
    };
  }, []);
  const value = useMemo<OperationContextValue>(() => ({
    blocking: operations.some((item) => item.kind === "blocking"),
    busy: operations.length > 0,
    discoveringUpstreamIds: new Set(
      operations.flatMap((item) => item.kind === "upstream-discovery" && item.upstreamId ? [item.upstreamId] : []),
    ),
    start,
  }), [operations, start]);
  return <OperationContext.Provider value={value}>{children}</OperationContext.Provider>;
}

export function useOperations() {
  const context = useContext(OperationContext);
  if (!context) throw new Error("useOperations 必须在 OperationProvider 内使用");
  return context;
}
