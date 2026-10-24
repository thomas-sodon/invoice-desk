"use client";

import * as React from "react";
import { loadApi, type ApiClient } from "@/lib/api";
import type { Me } from "@/lib/api-types";

interface ApiCtx {
  api: ApiClient | null;
  me: Me | null;
  refreshMe: () => void;
}

const Ctx = React.createContext<ApiCtx>({ api: null, me: null, refreshMe: () => undefined });

export function ApiProvider({ children }: { children: React.ReactNode }) {
  const [api, setApi] = React.useState<ApiClient | null>(null);
  const [me, setMe] = React.useState<Me | null>(null);

  React.useEffect(() => {
    let alive = true;
    void loadApi().then((a) => alive && setApi(a));
    return () => {
      alive = false;
    };
  }, []);

  const refreshMe = React.useCallback(() => {
    if (!api) return;
    void api.me().then(setMe);
  }, [api]);

  React.useEffect(() => {
    refreshMe();
    const onIdentity = () => refreshMe();
    window.addEventListener("invoicedesk:identity", onIdentity);
    return () => window.removeEventListener("invoicedesk:identity", onIdentity);
  }, [refreshMe]);

  return <Ctx.Provider value={{ api, me, refreshMe }}>{children}</Ctx.Provider>;
}

export function useApi(): ApiCtx {
  return React.useContext(Ctx);
}

/** Runs `load` once the API is ready and whenever `deps` change; exposes data, error and reload. */
export function useApiData<T>(load: (api: ApiClient) => Promise<T>, deps: React.DependencyList = []) {
  const { api } = useApi();
  const [data, setData] = React.useState<T | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [loading, setLoading] = React.useState(true);
  const loadRef = React.useRef(load);
  loadRef.current = load;
  const [tick, setTick] = React.useState(0);

  React.useEffect(() => {
    if (!api) return;
    let alive = true;
    setLoading(true);
    loadRef
      .current(api)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setError(null);
      })
      .catch((e: unknown) => alive && setError(e))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, tick, ...deps]);

  const reload = React.useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading: loading || !api, reload, setData };
}
