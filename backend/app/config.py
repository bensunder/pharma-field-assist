"""Application settings.

All configuration comes from environment variables (12-factor). In Azure Container Apps
these are injected from Key Vault secret references via the user-assigned managed identity,
so no secret ever lives in the image or the pipeline.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class AuthMode(StrEnum):
    DEMO = "demo"  # signed dev personas, no Entra tenant required
    ENTRA = "entra"  # validate Entra ID access tokens (MSAL SPA -> API)


class IntegrationMode(StrEnum):
    MOCK = "mock"  # local adapters over seeded data (offline demo, unit tests)
    LIVE = "live"  # real Microsoft Graph / Salesforce / Fabric endpoints


class SalesforceAuth(StrEnum):
    CLIENT_CREDENTIALS = "client_credentials"  # integration user; territory enforced by the API
    JWT_BEARER = "jwt_bearer"  # acts as each rep; Salesforce sharing rules enforced server-side


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    QA = "qa"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PFA_", env_file=".env", extra="ignore")

    environment: Environment = Environment.LOCAL
    app_version: str = "1.4.0"
    log_level: str = "INFO"

    # --- Auth -----------------------------------------------------------------
    auth_mode: AuthMode = AuthMode.DEMO
    demo_signing_key: str = Field(default="local-demo-signing-key-change-me-0123456789")
    entra_tenant_id: str | None = None
    entra_api_client_id: str | None = None  # API app registration (audience)
    entra_api_client_secret: str | None = None  # only used for OBO; Key Vault ref in ACA
    entra_audience: str | None = None  # e.g. api://pharma-field-assist

    # --- LLM (Azure OpenAI through APIM / direct) ----------------------------
    azure_openai_endpoint: str | None = None
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_api_key: str | None = None  # prefer managed identity in Azure
    llm_timeout_seconds: float = 20.0
    # Hard ceiling on tokens the model may generate per call (every call is clamped to this; APIM
    # enforces the same cap on the wire). Keep in step with MAX_OUTPUT_TOKENS in pfa-apim.sh.
    llm_max_output_tokens: int = Field(default=400, ge=16, le=4000)
    # Default ON (cost + governance): the only permitted LLM endpoint is an APIM AI gateway
    # (*.azure-api.net) and API keys are refused, so model traffic can only go
    # app -> APIM (token limits/quotas) -> Azure OpenAI with managed identities. No endpoint at all is
    # allowed (LLM off, zero model spend, extractive answers). A direct endpoint needs an explicit
    # PFA_LLM_GATEWAY_ONLY=false, e.g. local experiments.
    llm_gateway_only: bool = True
    # Set by the Azure deploy once the gateway exists: refuse to start if the LLM endpoint is missing.
    llm_required: bool = False

    # --- Integrations -------------------------------------------------------------
    graph_mode: IntegrationMode = IntegrationMode.MOCK
    crm_mode: IntegrationMode = IntegrationMode.MOCK
    fabric_mode: IntegrationMode = IntegrationMode.MOCK
    integration_timeout_seconds: float = 10.0

    # App registration used for app-only calls (Graph in demo-persona mode, Fabric SQL).
    # Tenant comes from entra_tenant_id. In ACA the secret is a Key Vault reference.
    integration_client_id: str | None = None
    integration_client_secret: str | None = None

    # Graph: with demo personas there is no user token, so calls go app-only to a mailbox.
    # graph_mailbox_map maps persona/user oid -> UPN; graph_demo_mailbox is the fallback for all.
    graph_demo_mailbox: str | None = None
    graph_mailbox_map: dict[str, str] = {}
    graph_timezone: str = "Central Standard Time"  # Windows tz id used by Outlook

    # Salesforce (Veeva CRM runs on the Salesforce platform)
    sf_login_url: str = "https://login.salesforce.com"  # or https://<mydomain>.my.salesforce.com
    sf_api_version: str = "v62.0"
    sf_auth: SalesforceAuth = SalesforceAuth.CLIENT_CREDENTIALS
    sf_client_id: str | None = None
    sf_client_secret: str | None = None  # client_credentials only
    sf_jwt_private_key_path: Path | None = None  # jwt_bearer only (PEM, matches connected-app cert)
    sf_username_map: dict[str, str] = {}  # user email -> Salesforce username (jwt_bearer)

    # Fabric Lakehouse SQL analytics endpoint
    fabric_sql_endpoint: str | None = None  # <id>.datawarehouse.fabric.microsoft.com
    fabric_database: str | None = None  # lakehouse name
    fabric_table: str = "weekly_rx_by_territory"

    # --- Knowledge / data ------------------------------------------------------
    knowledge_dir: Path = BACKEND_ROOT / "knowledge"
    fabric_dataset_path: Path = BACKEND_ROOT / "data" / "fabric_sales.json"
    crm_dataset_path: Path = BACKEND_ROOT / "data" / "crm_accounts.json"
    retrieval_top_k: int = 3
    retrieval_min_score: float = 0.8

    # --- Audit -------------------------------------------------------------------
    audit_db_path: Path = BACKEND_ROOT / "var" / "audit.db"

    # --- Resilience ----------------------------------------------------------------
    breaker_failure_threshold: int = 3
    breaker_reset_seconds: float = 30.0
    enable_fault_injection: bool = True  # forced False in prod (see validator below)

    # --- CORS ------------------------------------------------------------------------
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # --- Observability -----------------------------------------------------------------
    applicationinsights_connection_string: str | None = None

    def missing_live_config(self) -> list[str]:
        """Config required by every integration switched to live mode; empty list = OK."""
        missing: list[str] = []

        def need(name: str, value: object) -> None:
            if not value:
                missing.append(f"PFA_{name.upper()}")

        if IntegrationMode.LIVE in (self.graph_mode, self.fabric_mode):
            need("entra_tenant_id", self.entra_tenant_id)
        if self.graph_mode == IntegrationMode.LIVE:
            if self.auth_mode == AuthMode.ENTRA:
                need("entra_api_client_id", self.entra_api_client_id)
                need("entra_api_client_secret", self.entra_api_client_secret)
            else:
                need("integration_client_id", self.integration_client_id)
                need("integration_client_secret", self.integration_client_secret)
                need("graph_demo_mailbox (or graph_mailbox_map)", self.graph_demo_mailbox or self.graph_mailbox_map)
        if self.crm_mode == IntegrationMode.LIVE:
            need("sf_client_id", self.sf_client_id)
            if self.sf_auth == SalesforceAuth.CLIENT_CREDENTIALS:
                need("sf_client_secret", self.sf_client_secret)
                if "login.salesforce.com" in self.sf_login_url or "test.salesforce.com" in self.sf_login_url:
                    missing.append("PFA_SF_LOGIN_URL (client_credentials needs your My Domain URL)")
            else:
                need("sf_jwt_private_key_path", self.sf_jwt_private_key_path)
        if self.fabric_mode == IntegrationMode.LIVE:
            need("integration_client_id", self.integration_client_id)
            need("integration_client_secret", self.integration_client_secret)
            need("fabric_sql_endpoint", self.fabric_sql_endpoint)
            need("fabric_database", self.fabric_database)
        if self.llm_required and not self.azure_openai_endpoint:
            missing.append("PFA_AZURE_OPENAI_ENDPOINT is required when PFA_LLM_REQUIRED=true (the APIM gateway URL)")
        if self.llm_gateway_only and (self.azure_openai_endpoint or self.azure_openai_api_key):
            from urllib.parse import urlparse

            host = (urlparse(self.azure_openai_endpoint or "").hostname or "").lower()
            if not host.endswith(".azure-api.net"):
                missing.append(
                    "PFA_AZURE_OPENAI_ENDPOINT must be the APIM gateway (https://<apim>.azure-api.net/...) when PFA_LLM_GATEWAY_ONLY=true"
                )
            if self.azure_openai_api_key:
                missing.append("PFA_AZURE_OPENAI_API_KEY must be unset when PFA_LLM_GATEWAY_ONLY=true (managed identity only)")
        return list(dict.fromkeys(missing))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.azure_openai_endpoint)

    @property
    def fault_injection_allowed(self) -> bool:
        return self.enable_fault_injection and self.environment != Environment.PROD


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, used once at startup by ``create_app``."""
    return Settings()


def settings_dependency(request: Request) -> Settings:
    """FastAPI dependency: the settings the *running app* was built with (tests inject their own)."""
    return request.app.state.services.settings
