"use client";

import * as React from "react";

declare global {
  interface Window {
    turnstile?: {
      render: (el: HTMLElement, opts: { sitekey: string; callback: (token: string) => void; "error-callback"?: () => void; theme?: string }) => string;
      remove: (id: string) => void;
    };
  }
}

const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

function loadScript(): Promise<void> {
  if (window.turnstile) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${SCRIPT_SRC}"]`);
    const s = existing ?? document.createElement("script");
    s.addEventListener("load", () => resolve());
    s.addEventListener("error", () => reject(new Error("turnstile script failed")));
    if (!existing) {
      s.src = SCRIPT_SRC;
      s.async = true;
      document.head.appendChild(s);
    }
  });
}

/**
 * Cloudflare Turnstile human check. In mock mode (no server) a local stand-in issues a fixed token
 * so the flow can be exercised offline; the real API verifies tokens server-side.
 */
export function Turnstile({ mock, siteKey, onToken }: { mock: boolean; siteKey: string | undefined; onToken: (token: string) => void }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [failed, setFailed] = React.useState(false);
  const [checked, setChecked] = React.useState(false);
  const cb = React.useRef(onToken);
  cb.current = onToken;

  React.useEffect(() => {
    if (mock || !siteKey) return;
    let id: string | null = null;
    let cancelled = false;
    loadScript()
      .then(() => {
        if (cancelled || !ref.current || !window.turnstile) return;
        id = window.turnstile.render(ref.current, {
          sitekey: siteKey,
          theme: "light",
          callback: (t) => cb.current(t),
          "error-callback": () => setFailed(true),
        });
      })
      .catch(() => setFailed(true));
    return () => {
      cancelled = true;
      if (id && window.turnstile) window.turnstile.remove(id);
    };
  }, [mock, siteKey]);

  if (mock)
    return (
      <label className="inline-flex items-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-3 text-base" data-testid="turnstile-mock">
        <input
          type="checkbox"
          className="h-5 w-5"
          checked={checked}
          onChange={(e) => {
            setChecked(e.target.checked);
            if (e.target.checked) cb.current("mock-turnstile-pass");
          }}
        />
        I&apos;m a person (offline stand-in for the human check)
      </label>
    );
  if (!siteKey) return <p className="text-red-700">The human check isn&apos;t configured on this site.</p>;
  return (
    <div>
      <div ref={ref} data-testid="turnstile" />
      {failed && <p className="text-red-700">The human check couldn&apos;t load. Refresh the page to try again.</p>}
    </div>
  );
}
