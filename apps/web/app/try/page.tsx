"use client";

import { ShieldAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { UploadZone } from "@/components/board/upload-zone";
import { useApi } from "@/components/api-provider";
import { Turnstile } from "@/components/turnstile";
import { Alert } from "@/components/ui/alert";
import { DocumentViewer } from "@/components/viewer/document-viewer";
import { ApiError, errorMessage } from "@/lib/api";
import { isReplayUpload, type DocStatus, type DocumentDetail, type TryitStatus } from "@/lib/api-types";
import { STATUS_LABELS } from "@/lib/columns";
import { splitUploads } from "@/lib/upload";

const SETTLED: DocStatus[] = ["ReadyToPay", "NeedsReview", "Flagged", "Rejected", "Posted"];

export default function TryItPage() {
  const { api } = useApi();
  const [status, setStatus] = React.useState<TryitStatus | null>(null);
  const [session, setSession] = React.useState<{ uploads_remaining: number } | null>(null);
  const [docId, setDocId] = React.useState<string | null>(null);
  const [doc, setDoc] = React.useState<DocumentDetail | null>(null);
  const [replayBatch, setReplayBatch] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (!api) return;
    api.tryitStatus().then(setStatus).catch(() => setStatus(null));
  }, [api]);

  // Poll the visitor's document until the checks finish.
  React.useEffect(() => {
    if (!api || !docId) return;
    let stop = false;
    const tick = async () => {
      try {
        const d = await api.tryitDocument(docId);
        if (stop) return;
        setDoc(d);
        if (!SETTLED.includes(d.status)) setTimeout(() => void tick(), 1500);
      } catch (e) {
        if (!stop) setError(errorMessage(e));
      }
    };
    void tick();
    return () => {
      stop = true;
    };
  }, [api, docId]);

  const onToken = async (token: string) => {
    if (!api) return;
    try {
      const s = await api.tryitSession(token);
      setSession({ uploads_remaining: s.uploads_remaining });
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  const upload = async (files: File[]) => {
    if (!api) return;
    const { ok, rejected } = splitUploads(files.slice(0, 1));
    if (!ok[0]) return setError(rejected[0]?.error ?? "Choose one invoice.");
    setBusy(true);
    setError(null);
    setDoc(null);
    try {
      const r = await api.tryitUpload(ok[0]);
      if (isReplayUpload(r)) setReplayBatch(r.replay_batch_id);
      else {
        setDocId(r.document_id);
        setSession((s) => (s ? { uploads_remaining: Math.max(0, s.uploads_remaining - 1) } : s));
      }
    } catch (e) {
      setError(e instanceof ApiError && e.code === "rate_limited" ? "You've reached the upload limit for now. Try again tomorrow." : errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const capped = status?.spend_cap_reached ?? false;
  const replay = replayBatch ?? (capped ? status?.replay_batch_id : null);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-3xl font-extrabold tracking-tight">Try it on your own invoice</h1>
        <p className="text-lg text-slate-600">No sign-up. See what it reads and which checks it runs. Nothing is ever posted to accounting.</p>
      </div>

      <Alert tone="amber" className="flex items-start gap-3" data-testid="redaction-banner">
        <ShieldAlert className="mt-0.5 h-6 w-6 shrink-0" />
        <span>
          <strong>Redact first.</strong> Black out names, addresses, phone numbers and any bank or account numbers before
          you upload. Only upload invoices you&apos;re allowed to share.
        </span>
      </Alert>

      <ul className="grid gap-2 text-base text-slate-700 sm:grid-cols-2">
        <li>· One invoice at a time (JPEG, PNG, HEIC or PDF, up to 10 MB, 5 pages)</li>
        <li>· A few uploads per visitor per day</li>
        <li>· Your file is deleted automatically after 24 hours</li>
        <li>· Never posted to any accounting system</li>
      </ul>

      {replay ? (
        <Alert tone="blue" data-testid="replay-message">
          Today&apos;s free reading budget has been used up, so live uploads are paused until tomorrow. You can still watch a
          recorded run of the 25-invoice demo:{" "}
          <Link href={`/?batch=${replay}`} className="font-semibold underline">
            open the recorded run
          </Link>
          .
        </Alert>
      ) : !session ? (
        <div className="space-y-2">
          <p className="font-semibold">First, a quick human check:</p>
          <Turnstile mock={api?.mode === "mock"} siteKey={process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY} onToken={(t) => void onToken(t)} />
        </div>
      ) : (
        <div className="space-y-2">
          <UploadZone onFiles={(f) => void upload(f)} busy={busy} multiple={false} label="Drop one redacted invoice here" />
          <p className="text-sm text-slate-600">{session.uploads_remaining} upload{session.uploads_remaining === 1 ? "" : "s"} left in this session.</p>
        </div>
      )}

      {error && <Alert tone="red">{error}</Alert>}

      {docId && !doc && <p className="text-slate-600">Reading your invoice…</p>}
      {doc && (
        <div className="rounded-xl border border-slate-200 bg-white p-4 sm:p-5">
          {!SETTLED.includes(doc.status) && <p className="mb-3 text-blue-700">Checking… ({STATUS_LABELS[doc.status]})</p>}
          <DocumentViewer doc={doc} onChanged={() => undefined} variant="visitor" />
        </div>
      )}
    </div>
  );
}
