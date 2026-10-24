"use client";

import { Menu } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";
import { setDevUser } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { useApi } from "./api-provider";

const NAV = [
  { href: "/", label: "Board" },
  { href: "/review", label: "Review" },
  { href: "/approvals", label: "Approvals" },
  { href: "/followups", label: "Vendor emails" },
  { href: "/dashboard", label: "Owner dashboard" },
  { href: "/compare", label: "Basic AI vs InvoiceDesk" },
  { href: "/scorecard", label: "Scorecard" },
  { href: "/try", label: "Try your invoice" },
];

const STAFF = [
  { email: "dana.clerk@maplerow.example", label: "Dana (clerk)" },
  { email: "priya.approver@maplerow.example", label: "Priya (approver)" },
  { email: "sam.owner@maplerow.example", label: "Sam (owner)" },
];

function IdentityPicker() {
  const { api, me } = useApi();
  if (!api) return null;
  // The picker sets X-Dev-User, which the API accepts only in offline mode (and mock mode).
  return (
    <label className="flex items-center gap-2 text-sm text-slate-600">
      <span className="hidden sm:inline">Signed in as</span>
      <select
        aria-label="Signed in as"
        className="h-9 w-[8.5rem] rounded-md sm:w-auto sm:max-w-[11rem] border border-slate-300 bg-white px-2 text-sm text-slate-900"
        value={me?.email ?? STAFF[0]?.email}
        onChange={(e) => setDevUser(e.target.value)}
      >
        {STAFF.map((s) => (
          <option key={s.email} value={s.email}>
            {s.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { api } = useApi();
  const [open, setOpen] = React.useState(false);
  const [hero, setHero] = React.useState(false);
  React.useEffect(() => setOpen(false), [pathname]);
  React.useEffect(() => {
    setHero(new URLSearchParams(window.location.search).get("hero") === "1");
  }, [pathname]);
  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));

  return (
    <div className="min-h-screen bg-slate-50">
      {api?.mode === "mock" && !hero && (
        <div className="bg-indigo-700 px-4 py-1.5 text-center text-sm text-white">
          Offline demo: sample data, nothing is sent anywhere.
        </div>
      )}
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] items-center gap-2 px-4 py-3 sm:gap-3">
          <Link href="/" className="flex min-w-0 shrink items-center gap-2 text-lg font-extrabold sm:text-xl tracking-tight text-slate-900">
            <span aria-hidden className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-blue-700 text-base text-white">
              ID
            </span>
            <span className="truncate">InvoiceDesk</span>
          </Link>
          <span className="hidden text-sm text-slate-500 lg:inline">Maple Row Property Management</span>
          <div className="ml-auto flex shrink-0 items-center gap-1 sm:gap-2">
            <IdentityPicker />
            <button
              type="button"
              className="rounded-md p-2 text-slate-700 hover:bg-slate-100 xl:hidden"
              aria-label="Menu"
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
            >
              <Menu className="h-6 w-6" />
            </button>
          </div>
        </div>
        <nav
          aria-label="Main"
          className={cn("mx-auto max-w-[1600px] px-4 pb-2", hero ? "hidden" : "xl:block", open ? "block" : "hidden")}
        >
          <ul className="flex flex-col gap-1 xl:flex-row xl:flex-wrap">
            {NAV.map((n) => (
              <li key={n.href}>
                <Link
                  href={n.href}
                  className={cn(
                    "block rounded-md px-3 py-2 text-base font-medium",
                    isActive(n.href) ? "bg-blue-50 text-blue-800" : "text-slate-700 hover:bg-slate-100",
                  )}
                  aria-current={isActive(n.href) ? "page" : undefined}
                >
                  {n.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main className="mx-auto max-w-[1600px] px-4 py-5">{children}</main>
      <footer className="mx-auto max-w-[1600px] px-4 pb-8 text-sm text-slate-500">
        Works with QuickBooks Online. All names and numbers here are fictional.
      </footer>
    </div>
  );
}
