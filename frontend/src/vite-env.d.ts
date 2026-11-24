/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_AUTH_MODE?: "demo" | "entra";
  readonly VITE_ENTRA_CLIENT_ID?: string;
  readonly VITE_ENTRA_TENANT_ID?: string;
  readonly VITE_API_SCOPE?: string;
  readonly VITE_API_BASE?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
