/**
 * Staff identity for API calls. Production uses a Supabase JWT (Bearer). Offline/dev mode may send
 * X-Dev-User with a staff email (accepted by the API only when INVOICEDESK_MODE=offline).
 */
const JWT_KEY = "invoicedesk.jwt";
const DEV_USER_KEY = "invoicedesk.devUser";

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function getDevUser(): string | null {
  try {
    return storage()?.getItem(DEV_USER_KEY) ?? process.env.NEXT_PUBLIC_DEV_USER ?? null;
  } catch {
    return process.env.NEXT_PUBLIC_DEV_USER ?? null;
  }
}

export function setDevUser(email: string): void {
  try {
    storage()?.setItem(DEV_USER_KEY, email);
  } catch {
    /* storage unavailable: identity falls back to the env default */
  }
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent("invoicedesk:identity"));
}

export function getJwt(): string | null {
  try {
    return storage()?.getItem(JWT_KEY) ?? null;
  } catch {
    return null;
  }
}

export function authHeaders(): Record<string, string> {
  const jwt = getJwt();
  if (jwt) return { Authorization: `Bearer ${jwt}` };
  const dev = getDevUser();
  return dev ? { "X-Dev-User": dev } : {};
}
