# src/handlers/authorizer.py
# API Gateway Lambda Authorizer
# Validates the Bearer JWT and injects userId + role into the
# request context so downstream handlers can do RBAC.

import os
import logging

import jwt  # PyJWT

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod-at-least-256-bits-long")
JWT_ALGORITHM = "HS256"


def handler(event: dict, _context) -> dict:
    token = _extract_token(event)

    if not token:
        raise Exception("Unauthorized")  # API GW returns 401

    try:
        print("JWT_SECRET:", JWT_SECRET)
        print("token:", token)
        decoded = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        logger.info("JWT verified for user %s with role %s", decoded.get("sub"), decoded.get("role"))

        return _build_policy(
            principal_id=decoded["sub"],
            effect="Allow",
            resource=event["methodArn"],
            context={
                "userId": decoded["sub"],
                "role":   decoded.get("role", ""),
                "email":  decoded.get("email", ""),
            },
        )

    except jwt.ExpiredSignatureError:
        logger.warning("JWT expired")
        raise Exception("Unauthorized")
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid JWT: %s", e)
        raise Exception("Unauthorized")


def _extract_token(event: dict) -> str | None:
    auth_header = event.get("authorizationToken", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def _build_policy(principal_id: str, effect: str, resource: str, context: dict) -> dict:
    # Wildcard the resource path so the cached policy covers all endpoints
    base_resource = "/".join(resource.split("/")[:2]) + "/*"

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{
                "Action":   "execute-api:Invoke",
                "Effect":   effect,
                "Resource": base_resource,
            }],
        },
        "context": context,
    }
