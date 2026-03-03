# src/handlers/actions.py
# Handlers that RESUME paused Step Functions executions
# by calling send_task_success / send_task_failure with the stored task token.
#
# Flow:
#   1. User calls POST /claims/{id}/adjuster-review  (or supervisor-approval)
#   2. We validate their role + claim state
#   3. We retrieve the task token from DynamoDB
#   4. We call sfn.send_task_success(taskToken, output)
#   5. The paused execution resumes from where it left off

import json
import logging

import boto3
from botocore.exceptions import ClientError

from src.models.claim import (
    ClaimStatus,
    get_claim,
    update_claim_status,
    get_task_token,
    mark_token_consumed,
)
from src.lib.helpers import (
    ok, bad_request, forbidden, not_found, conflict, server_error,
    extract_user, has_permission,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

sfn = boto3.client("stepfunctions")


def _resume_execution(task_token: str, result: dict) -> None:
    """Send the result back to Step Functions, resuming the execution."""
    sfn.send_task_success(
        taskToken=task_token,
        output=json.dumps(result, default=str),
    )


def _reject_execution(task_token: str, reason: str, rejected_by: str) -> None:
    """Fail the wait state with a ClaimRejected error → triggers the Catch in the state machine."""
    sfn.send_task_failure(
        taskToken=task_token,
        error="ClaimRejected",
        cause=json.dumps({"reason": reason, "rejectedBy": rejected_by}),
    )


# ─────────────────────────────────────────────
# POST /claims/{claimId}/adjuster-review
# Role: ADJUSTER, ADMIN
#
# Body: {
#   "decision": "APPROVED" | "REJECTED",
#   "notes": "...",
#   "settlementRecommendation": 9500.00   (optional)
# }
# ─────────────────────────────────────────────
def adjuster_review(event: dict, _context) -> dict:
    try:
        user = extract_user(event)

        if not has_permission(user["role"], "adjusterReview"):
            return forbidden("Only adjusters can perform this action")

        claim_id = event["pathParameters"]["claimId"]
        body     = json.loads(event.get("body") or "{}")
        decision = body.get("decision")
        notes    = body.get("notes")
        settlement_recommendation = body.get("settlementRecommendation")

        if decision not in ("APPROVED", "REJECTED"):
            return bad_request("decision must be APPROVED or REJECTED")
        if not notes:
            return bad_request("notes are required")

        # Validate claim exists and is in the right state
        claim = get_claim(claim_id)
        if not claim:
            return not_found(f"Claim {claim_id} not found")

        if claim["status"] != ClaimStatus.PENDING_ADJUSTER:
            return conflict(
                f"Claim is in status '{claim['status']}', expected PENDING_ADJUSTER. "
                "Cannot perform adjuster review."
            )

        # Retrieve the stored task token
        token_record = get_task_token(claim_id, "ADJUSTER_REVIEW")
        if not token_record:
            return not_found("Task token not found. It may have expired.")
        if token_record.get("consumed"):
            return conflict("This review step has already been acted on.")

        from datetime import datetime, timezone
        review_result = {
            "decision":                 decision,
            "notes":                    notes,
            "settlementRecommendation": settlement_recommendation or claim["amount"],
            "reviewedBy":               user["userId"],
            "reviewedAt":               datetime.now(timezone.utc).isoformat(),
        }

        if decision == "APPROVED":
            # Resume the execution → moves to SupervisorApprovalRequired choice state
            _resume_execution(token_record["taskToken"], review_result)

            # Status will settle at PENDING_SUPERVISOR or IN_SETTLEMENT depending
            # on the Choice state outcome, but we optimistically set PENDING_SUPERVISOR here.
            update_claim_status(claim_id, ClaimStatus.PENDING_SUPERVISOR, {
                "adjusterReview": review_result,
                "assignedTo": user["userId"],
            })
            mark_token_consumed(claim_id, "ADJUSTER_REVIEW")

            return ok({
                "message":  "Claim approved by adjuster, routing to supervisor if required",
                "claimId":  claim_id,
                "decision": decision,
            })

        else:  # REJECTED
            # send_task_failure triggers the Catch(ClaimRejected) in the state machine
            _reject_execution(token_record["taskToken"], notes, user["userId"])

            update_claim_status(claim_id, ClaimStatus.CLOSED_REJECTED, {
                "adjusterReview": review_result,
            })
            mark_token_consumed(claim_id, "ADJUSTER_REVIEW")

            return ok({
                "message":  "Claim rejected",
                "claimId":  claim_id,
                "decision": decision,
            })

    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "TaskTimedOut":
            return conflict("The task token has expired. This step can no longer be acted on.")
        if code == "InvalidToken":
            return bad_request("Invalid task token.")
        logger.exception("AWS error in adjuster_review")
        return server_error(str(e))
    except Exception as e:
        logger.exception("Error in adjuster_review")
        return server_error(str(e))


# ─────────────────────────────────────────────
# POST /claims/{claimId}/supervisor-approval
# Role: SUPERVISOR, ADMIN
#
# Body: {
#   "decision": "APPROVED" | "REJECTED",
#   "notes": "...",
#   "finalAmount": 13000.00   (optional override)
# }
# ─────────────────────────────────────────────
def supervisor_approval(event: dict, _context) -> dict:
    try:
        user = extract_user(event)

        if not has_permission(user["role"], "supervisorApproval"):
            return forbidden("Only supervisors can perform this action")

        claim_id     = event["pathParameters"]["claimId"]
        body         = json.loads(event.get("body") or "{}")
        decision     = body.get("decision")
        notes        = body.get("notes")
        final_amount = body.get("finalAmount")

        if decision not in ("APPROVED", "REJECTED"):
            return bad_request("decision must be APPROVED or REJECTED")
        if not notes:
            return bad_request("notes are required")

        claim = get_claim(claim_id)
        if not claim:
            return not_found(f"Claim {claim_id} not found")

        if claim["status"] != ClaimStatus.PENDING_SUPERVISOR:
            return conflict(
                f"Claim is in status '{claim['status']}', expected PENDING_SUPERVISOR."
            )

        token_record = get_task_token(claim_id, "SUPERVISOR_APPROVAL")
        if not token_record:
            return not_found("Task token not found. It may have expired.")
        if token_record.get("consumed"):
            return conflict("This approval step has already been acted on.")

        from datetime import datetime, timezone
        approval_result = {
            "decision":    decision,
            "notes":       notes,
            "finalAmount": final_amount or claim["amount"],
            "approvedBy":  user["userId"],
            "approvedAt":  datetime.now(timezone.utc).isoformat(),
        }

        if decision == "APPROVED":
            _resume_execution(token_record["taskToken"], approval_result)

            update_claim_status(claim_id, ClaimStatus.IN_SETTLEMENT, {
                "supervisorApproval": approval_result,
            })
            mark_token_consumed(claim_id, "SUPERVISOR_APPROVAL")

            return ok({
                "message":  "Claim approved by supervisor, proceeding to settlement",
                "claimId":  claim_id,
                "decision": decision,
            })

        else:  # REJECTED
            _reject_execution(token_record["taskToken"], notes, user["userId"])

            update_claim_status(claim_id, ClaimStatus.CLOSED_REJECTED, {
                "supervisorApproval": approval_result,
            })
            mark_token_consumed(claim_id, "SUPERVISOR_APPROVAL")

            return ok({
                "message":  "Claim rejected by supervisor",
                "claimId":  claim_id,
                "decision": decision,
            })

    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "TaskTimedOut":
            return conflict("The task token has expired.")
        if code == "InvalidToken":
            return bad_request("Invalid task token.")
        logger.exception("AWS error in supervisor_approval")
        return server_error(str(e))
    except Exception as e:
        logger.exception("Error in supervisor_approval")
        return server_error(str(e))
