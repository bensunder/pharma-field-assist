"""Authentication & principal resolution.

Two modes behind one interface:

* ``entra`` — the React SPA signs in with MSAL (PKCE) and calls the API with an Entra ID access
  token. We validate signature (tenant JWKS), issuer, audience, expiry and pull ``oid``, ``roles``
  (app roles) and the custom ``territory`` claim. The raw assertion is kept on the principal so
  downstream integrations can run the On-Behalf-Of exchange (see ``obo.py``).
* ``demo`` — interview/local mode. Short-lived HS256 tokens for three fixed personas so the full
  RBAC + audit flow can be exercised without a tenant.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

import jwt
from fastapi import Depends, Header

from app.config import AuthMode, Settings, settings_dependency
from app.core.errors import AuthenticationError, AuthorizationError


class Role(StrEnum):
    FIELD_REP = "FieldRep"
    DISTRICT_MANAGER = "DistrictManager"
    COMPLIANCE_OFFICER = "ComplianceOfficer"


@dataclass(frozen=True)
class Principal:
    oid: str
    name: str
    email: str
    roles: frozenset[Role]
    territory: str
    raw_token: str = field(repr=False, default="")

    def has_role(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))


DEMO_PERSONAS: dict[str, dict] = {
    "rep": {
        "oid": "00000000-0000-0000-0000-00000000a001",
        "name": "Jordan Reyes",
        "email": "jordan.reyes@contoso-pharma.com",
        "roles": [Role.FIELD_REP],
        "territory": "TX-HOU-03",
    },
    "manager": {
        "oid": "00000000-0000-0000-0000-00000000b002",
        "name": "Priya Natarajan",
        "email": "priya.natarajan@contoso-pharma.com",
        "roles": [Role.FIELD_REP, Role.DISTRICT_MANAGER],
        "territory": "TX-HOU",
    },
    "compliance": {
        "oid": "00000000-0000-0000-0000-00000000c003",
        "name": "Marcus Lee",
        "email": "marcus.lee@contoso-pharma.com",
        "roles": [Role.COMPLIANCE_OFFICER],
        "territory": "US",
    },
}

DEMO_ISSUER = "pharma-field-assist-demo"
DEMO_AUDIENCE = "pharma-field-assist-api"
DEMO_TTL_SECONDS = 8 * 3600


def issue_demo_token(persona: str, settings: Settings) -> str:
    if settings.auth_mode != AuthMode.DEMO:
        raise AuthorizationError("Demo tokens are disabled when auth_mode=entra")
    p = DEMO_PERSONAS.get(persona)
    if p is None:
        raise AuthenticationError(f"Unknown demo persona '{persona}'")
    now = int(time.time())
    claims = {
        "iss": DEMO_ISSUER,
        "aud": DEMO_AUDIENCE,
        "iat": now,
        "exp": now + DEMO_TTL_SECONDS,
        "oid": p["oid"],
        "name": p["name"],
        "preferred_username": p["email"],
        "roles": [str(r) for r in p["roles"]],
        "territory": p["territory"],
    }
    return jwt.encode(claims, settings.demo_signing_key, algorithm="HS256")


@lru_cache(maxsize=4)
def _jwks_client(tenant_id: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
        cache_keys=True,
        lifespan=3600,
    )


def _decode(token: str, settings: Settings) -> dict:
    try:
        if settings.auth_mode == AuthMode.DEMO:
            return jwt.decode(
                token,
                settings.demo_signing_key,
                algorithms=["HS256"],
                audience=DEMO_AUDIENCE,
                issuer=DEMO_ISSUER,
            )
        if not (settings.entra_tenant_id and settings.entra_audience):
            raise AuthenticationError("Entra auth is enabled but tenant/audience are not configured")
        signing_key = _jwks_client(settings.entra_tenant_id).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.entra_audience,
            issuer=f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0",
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid token") from exc


def principal_from_token(token: str, settings: Settings) -> Principal:
    claims = _decode(token, settings)
    roles: set[Role] = set()
    for r in claims.get("roles", []):
        try:
            roles.add(Role(r))
        except ValueError:
            continue  # unknown app roles are ignored, never elevated
    if not roles:
        raise AuthorizationError("Caller has no application role assigned")
    return Principal(
        oid=claims["oid"],
        name=claims.get("name", "unknown"),
        email=claims.get("preferred_username", ""),
        roles=frozenset(roles),
        territory=claims.get("territory", "UNASSIGNED"),
        raw_token=token,
    )


async def get_principal(
    authorization: str | None = Header(default=None),
    x_pfa_token: str | None = Header(default=None),
    settings: Settings = Depends(settings_dependency),
) -> Principal:
    # The SPA sends its token in X-PFA-Token so the Authorization header stays free for an
    # edge gate (nginx basic auth on demo deployments); nginx maps it back to Bearer upstream.
    if authorization and authorization.lower().startswith("bearer "):
        return principal_from_token(authorization.split(" ", 1)[1].strip(), settings)
    if x_pfa_token and x_pfa_token.strip():
        return principal_from_token(x_pfa_token.strip(), settings)
    raise AuthenticationError("Missing bearer token")


def require_roles(*roles: Role):
    async def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has_role(*roles):
            raise AuthorizationError(f"Requires one of roles: {', '.join(roles)}")
        return principal

    return _dep
