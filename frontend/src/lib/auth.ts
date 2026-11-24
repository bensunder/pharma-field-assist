// Token acquisition. Two modes, one interface (getToken):
//  - entra: MSAL.js (auth code + PKCE) -> access token for the API scope; silent renew, popup fallback.
//  - demo : signed persona tokens from /api/auth/demo-token (interview/local only; disabled server-side in entra mode).

import {
  InteractionRequiredAuthError,
  PublicClientApplication,
  type AccountInfo,
} from "@azure/msal-browser";
import type { Persona } from "./types";

const MODE = import.meta.env.VITE_AUTH_MODE ?? "demo";
const API_BASE = import.meta.env.VITE_API_BASE ?? "";

let msal: PublicClientApplication | null = null;
let account: AccountInfo | null = null;

async function getMsal(): Promise<PublicClientApplication> {
  if (msal) return msal;
  const clientId = import.meta.env.VITE_ENTRA_CLIENT_ID;
  const tenant = import.meta.env.VITE_ENTRA_TENANT_ID;
  if (!clientId || !tenant) throw new Error("VITE_ENTRA_CLIENT_ID / VITE_ENTRA_TENANT_ID are required in entra mode");
  msal = new PublicClientApplication({
    auth: { clientId, authority: `https://login.microsoftonline.com/${tenant}`, redirectUri: window.location.origin },
    cache: { cacheLocation: "sessionStorage" }, // tokens never persisted beyond the tab
  });
  await msal.initialize();
  const redirect = await msal.handleRedirectPromise();
  account = redirect?.account ?? msal.getAllAccounts()[0] ?? null;
  return msal;
}

const demoTokens = new Map<Persona, string>();

export const authMode = MODE;

export async function getToken(persona: Persona): Promise<string> {
  if (MODE === "entra") {
    const app = await getMsal();
    const scopes = [import.meta.env.VITE_API_SCOPE ?? "api://pharma-field-assist/access_as_user"];
    if (!account) {
      const login = await app.loginPopup({ scopes });
      account = login.account;
    }
    try {
      return (await app.acquireTokenSilent({ scopes, account: account! })).accessToken;
    } catch (e) {
      if (e instanceof InteractionRequiredAuthError) return (await app.acquireTokenPopup({ scopes })).accessToken;
      throw e;
    }
  }
  const cached = demoTokens.get(persona);
  if (cached) return cached;
  const r = await fetch(`${API_BASE}/api/auth/demo-token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona }),
  });
  if (!r.ok) throw new Error(`Demo sign-in failed (${r.status})`);
  const token = (await r.json()).access_token as string;
  demoTokens.set(persona, token);
  return token;
}
