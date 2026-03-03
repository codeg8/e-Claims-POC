# src/lib/helpers.py
# Shared utilities: HTTP responses, user roles, and permission checks

import json
from typing import Any

# ─────────────────────────────────────────────
# USER ROLES  (carried in the JWT 'role' claim)
# ─────────────────────────────────────────────
class Roles:
    CLAIMANT   = "CLAIMANT"    # Submit + view own claims
    ADJUSTER   = "ADJUSTER"    # Review claims, approve/reject
    SUPERVISOR = "SUPERVISOR"  # Approve high-value claims
    ADMIN      = "ADMIN"       # Full access

# Which roles can perform which actions
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "submitClaim":        [Roles.CLAIMANT,   Roles.ADMIN],
    "viewAnyClaim":       [Roles.ADJUSTER,   Roles.SUPERVISOR, Roles.ADMIN],
    "viewOwnClaim":       [Roles.CLAIMANT],
    "adjusterReview":     [Roles.ADJUSTER,   Roles.ADMIN],
    "supervisorApproval": [Roles.SUPERVISOR, Roles.ADMIN],
    "listAllClaims":      [Roles.ADJUSTER,   Roles.SUPERVISOR, Roles.ADMIN],
}

def has_permission(role: str, action: str) -> bool:
    return role in ROLE_PERMISSIONS.get(action, [])


# ─────────────────────────────────────────────
# HTTP RESPONSE HELPERS
# ─────────────────────────────────────────────
CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}

def _response(status_code: int, body: Any) -> dict:
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, default=str),  # default=str handles Decimals, datetimes
    }

def ok(body: Any)                  -> dict: return _response(200, body)
def created(body: Any)             -> dict: return _response(201, body)
def bad_request(message: str)      -> dict: return _response(400, {"error": message})
def forbidden(message: str = "Forbidden")       -> dict: return _response(403, {"error": message})
def not_found(message: str = "Not found")       -> dict: return _response(404, {"error": message})
def conflict(message: str)         -> dict: return _response(409, {"error": message})
def server_error(message: str = "Internal server error") -> dict: return _response(500, {"error": message})


# ─────────────────────────────────────────────
# EXTRACT USER FROM API GATEWAY AUTHORIZER CONTEXT
# ─────────────────────────────────────────────
def extract_user(event: dict) -> dict:
    """
    API Gateway passes the authorizer's returned context under
    event['requestContext']['authorizer'].
    Returns a dict with userId, role, email.
    """
    ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    return {
        "userId": ctx.get("userId"),
        "role":   ctx.get("role"),
        "email":  ctx.get("email", ""),
    }
