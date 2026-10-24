"use client";

import * as React from "react";
import type { ApiClient } from "@/lib/api";
import type { DocumentDetail } from "@/lib/api-types";
import { useApi } from "./api-provider";

/** Loads one document and re-fetches it on realtime events for that document (per the contract). */
export function useDocument(id: string | null, loader?: (api: ApiClient, id: string) => Promise<DocumentDetail>) {
  const { api } = useApi();
  const [doc, setDoc] = React.useState<DocumentDetail | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const loaderRef = React.useRef(loader);
  loaderRef.current = loader;

  // The id the caller currently wants; a slow response for a previously selected invoice must not
  // replace the one now on screen (queues switch documents quickly).
  const currentId = React.useRef(id);
  currentId.current = id;

  const reload = React.useCallback(() => {
    if (!api || !id) return;
    const load = loaderRef.current ?? ((a: ApiClient, i: string) => a.getDocument(i));
    load(api, id)
      .then((d) => {
        if (currentId.current !== id) return;
        setDoc(d);
        setError(null);
      })
      .catch((e: unknown) => {
        if (currentId.current === id) setError(e);
      });
  }, [api, id]);

  React.useEffect(() => {
    setDoc(null);
    reload();
  }, [reload]);

  React.useEffect(() => {
    if (!api || !id) return;
    const sub = api.subscribe(null, (e) => {
      if (e.document_id === id && (e.type === "document.updated" || e.type === "document.status_changed")) reload();
    });
    return () => sub.close();
  }, [api, id, reload]);

  return { doc, error, reload };
}
