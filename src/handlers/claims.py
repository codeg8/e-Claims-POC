# src/handlers/claims.py
# REST handlers for claim lifecycle:
#   POST /claims          — submit a new claim + start execution
#   GET  /claims/{claimId} — get a single claim
#   GET  /claims           — list claims (by status or assignee)

import json
import logging
import os
import uuid
from decimal import Decimal

import boto3

from src.models.claim import (
    ClaimStatus,
    create_claim,
    get_claim as get_claim_from_db,
    list_claims_by_status,
    list_claims_by_assignee, update_claim_status,
)
from src.lib.helpers import (
    ok, created, bad_request, forbidden, not_found, server_error,
    extract_user, has_permission, Roles,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

sfn = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

# VALID_CLAIM_TYPES = {"AUTO", "PROPERTY", "HEALTH", "LIABILITY"}


# ─────────────────────────────────────────────
# POST /claims
# Role: CLAIMANT
# ─────────────────────────────────────────────
def submit(event: dict, _context) -> dict:
    try:
        user = extract_user(event)
        print(user)

        if not has_permission(user["role"], "submitClaim"):
            return forbidden("Only claimants can submit claims")

        body = json.loads(event.get("body") or "{}")
        policy_number = body.get("policyNumber")
        claim_type    = body.get("claimType", 'AUTO')  # default to AUTO if not provided
        amount        = body.get("amount", 0)
        description   = body.get("description", "")

        # Validation
        if not all([policy_number, claim_type, amount]):
            return bad_request("policyNumber and amount are required")

        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            return bad_request("amount must be a positive number")

        # if claim_type not in VALID_CLAIM_TYPES:
        #     return bad_request(f"claimType must be one of: {', '.join(VALID_CLAIM_TYPES)}")

        claim_id = str(uuid.uuid4())

        # Input object that travels through every state machine state
        execution_input = {
            "claimId":      claim_id,
            "policyNumber": policy_number,
            "claimType":    claim_type,
            "amount":       amount,
            "description":  description,
            "claimantId":   user["userId"],
        }

        execution_name = f"claim-{claim_id}"

        claim = create_claim({
            "claimId":       claim_id,
            "policyNumber":  policy_number,
            "claimType":     claim_type,
            "amount":        Decimal(amount),
            "description":   description,
            "claimantId":    user["userId"],
            "executionArn":  execution_name,
        })

        # Start one Step Functions execution per claim
        execution = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,          # unique name = easy to look up later
            input=json.dumps(execution_input),
        )

        update_claim_status(claim_id, ClaimStatus.SUBMITTED, {
            "executionArn": execution["executionArn"],
        })

        return created({
            "message":      "Claim submitted successfully",
            "claimId":      claim_id,
            "executionArn": execution["executionArn"],
            "status":       claim["status"],
        })

    except Exception as e:
        logger.exception("Error in submit")
        return server_error(str(e))


# ─────────────────────────────────────────────
# GET /claims/{claimId}
# Role: CLAIMANT (own only), ADJUSTER/SUPERVISOR/ADMIN (any)
# ─────────────────────────────────────────────
def get_claim(event: dict, _context) -> dict:
    try:
        user     = extract_user(event)
        claim_id = event["pathParameters"]["claimId"]

        claim = get_claim_from_db(claim_id)
        if not claim:
            return not_found(f"Claim {claim_id} not found")

        # Claimants can only see their own claims
        if user["role"] == Roles.CLAIMANT and claim.get("claimantId") != user["userId"]:
            return forbidden("You can only view your own claims")

        if not has_permission(user["role"], "viewAnyClaim") and not has_permission(user["role"], "viewOwnClaim"):
            return forbidden()

        return ok(claim)

    except Exception as e:
        logger.exception("Error in get_claim")
        return server_error(str(e))


# ─────────────────────────────────────────────
# GET /claims?status=PENDING_ADJUSTER&assignedTo=userId
# Role: ADJUSTER, SUPERVISOR, ADMIN
# ─────────────────────────────────────────────
def list_claims(event: dict, _context) -> dict:
    try:
        user = extract_user(event)

        if not has_permission(user["role"], "listAllClaims"):
            return forbidden("Insufficient permissions to list claims")

        params      = event.get("queryStringParameters") or {}
        status      = params.get("status")
        assigned_to = params.get("assignedTo")

        if assigned_to:
            claims = list_claims_by_assignee(assigned_to)

        elif status:
            if status not in ClaimStatus.ALL:
                return bad_request(f"Invalid status. Valid values: {', '.join(ClaimStatus.ALL)}")
            claims = list_claims_by_status(status)

        else:
            # Default: show the queue relevant to the caller's role
            default_status = (
                ClaimStatus.PENDING_SUPERVISOR
                if user["role"] == Roles.SUPERVISOR
                else ClaimStatus.PENDING_ADJUSTER
            )
            claims = list_claims_by_status(default_status)

        return ok({"claims": claims, "count": len(claims)})

    except Exception as e:
        logger.exception("Error in list_claims")
        return server_error(str(e))
