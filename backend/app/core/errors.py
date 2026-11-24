"""Domain error hierarchy mapped to HTTP responses by the global handler in main.py."""

from __future__ import annotations


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"


class AuthorizationError(AppError):
    status_code = 403
    code = "forbidden"


class ValidationError(AppError):
    status_code = 422
    code = "invalid_request"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class DependencyUnavailableError(AppError):
    """Raised when a downstream integration is down or its circuit is open."""

    status_code = 503
    code = "dependency_unavailable"
