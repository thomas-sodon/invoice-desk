import type { Metadata, Viewport } from "next";
import { ApiProvider } from "@/components/api-provider";
import { AppShell } from "@/components/app-shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "InvoiceDesk",
  description: "An AI accounts-payable clerk: reads vendor invoices, checks them against work orders and estimates, and holds anything risky for a person.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#ffffff" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ApiProvider>
          <AppShell>{children}</AppShell>
        </ApiProvider>
      </body>
    </html>
  );
}
