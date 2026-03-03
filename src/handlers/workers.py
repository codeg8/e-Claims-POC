# src/handlers/workers.py
# Lambdas invoked directly by Step Functions (not via API Gateway).
# These do automated work AND store task tokens for the human-wait steps.
#
# NOTE: auto_triage is reused as the token-saving Lambda for both
# WaitForTaskToken steps in the state machine (POC simplicity).
# The 'step' field in the event differentiates the two cases.
# In production, split into dedicated save_token_adjuster / save_token_supervisor Lambdas.

import logging
import random
import time
from decimal import Decimal

from src.models.claim import (
    ClaimStatus,
    update_claim_status,
    save_task_token,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ─────────────────────────────────────────────
# AUTO TRIAGE
# Called by the AutoTriage state (standard Lambda invoke)
# Also handles token-saving when called by WaitForTaskToken states
# ─────────────────────────────────────────────
def auto_triage(event: dict, _context) -> dict:
    # If a taskToken is present this is a WaitForTaskToken invocation → save & return
    if "taskToken" in event and "step" in event:
        return _handle_wait_step(event)

    claim_id   = event["claimId"]
    claim_type = event["claimType"]
    amount     = float(event["amount"])

    logger.info("[AutoTriage] Processing claim %s", claim_id)
    update_claim_status(claim_id, ClaimStatus.IN_TRIAGE)

    fraud_score = _compute_fraud_score(claim_type=claim_type, amount=amount)

    if amount > 50_000 or fraud_score > 0.7:
        priority = "HIGH"
    elif amount > 10_000:
        priority = "MEDIUM"
    else:
        priority = "STANDARD"

    notes_parts = [
        f"Fraud score: {fraud_score:.2f}",
        f"Claim type: {claim_type}",
        f"Priority assigned: {priority}",
    ]
    if fraud_score > 0.6:
        notes_parts.append("⚠️  Elevated fraud risk — manual review recommended")

    triage_notes = ". ".join(notes_parts)

    update_claim_status(claim_id, ClaimStatus.PENDING_ADJUSTER)

    return {
        "fraudScore":   fraud_score,
        "priority":     priority,
        "triageNotes":  triage_notes,
    }


def _handle_wait_step(event: dict) -> dict:
    """
    Called when Step Functions invokes this Lambda with a task token
    via the waitForTaskToken integration.

    We store the token in DynamoDB and return — the execution stays
    paused until the API handler calls SendTaskSuccess.
    """
    step       = event["step"]
    claim_id   = event["claimId"]
    task_token = event["taskToken"]

    logger.info("[WaitStep] Saving token for claim %s at step %s", claim_id, step)

    status_map = {
        "ADJUSTER_REVIEW":    ClaimStatus.PENDING_ADJUSTER,
        "SUPERVISOR_APPROVAL": ClaimStatus.PENDING_SUPERVISOR,
    }

    save_task_token(claim_id, step, task_token)
    update_claim_status(claim_id, status_map.get(step, ClaimStatus.PENDING_ADJUSTER))

    # IMPORTANT: returning here does NOT resume the execution.
    # The execution only resumes when SendTaskSuccess is called with the token.
    return {"saved": True, "step": step, "claimId": claim_id}


# ─────────────────────────────────────────────
# PREPARE SETTLEMENT
# Called by the PrepareSettlement state
# ─────────────────────────────────────────────
def prepare_settlement(event: dict, _context) -> dict:
    claim_id        = event["claimId"]
    amount          = float(event["amount"])
    adjuster_review = event.get("adjusterReview", {})

    logger.info("[PrepareSettlement] Processing claim %s", claim_id)
    update_claim_status(claim_id, ClaimStatus.IN_SETTLEMENT)

    # Use adjuster's recommendation if provided, otherwise original amount
    settlement_amount = float(adjuster_review.get("settlementRecommendation") or amount)

    # Stub: in production → call payment service, generate PDFs via S3+Lambda
    payment_ref = f"PAY-{int(time.time())}-{claim_id[:8].upper()}"

    return {
        "settlementAmount":    settlement_amount,
        "paymentRef":          payment_ref,
        "documentsGenerated": [
            f"settlement-letter-{claim_id}.pdf",
            f"payment-confirmation-{payment_ref}.pdf",
        ],
    }


# ─────────────────────────────────────────────
# CLOSE CLAIM
# Called by CloseClaim, ClaimRejected, and ClaimEscalated states
# ─────────────────────────────────────────────
def close_claim(event: dict, _context) -> dict:
    claim_id   = event["claimId"]
    outcome    = event.get("outcome", "FAILED")
    settlement = event.get("settlement")
    reason     = event.get("reason")
    error      = event.get("error")

    logger.info("[CloseClaim] Closing claim %s with outcome %s", claim_id, outcome)

    outcome_to_status = {
        "CLOSED_APPROVED": ClaimStatus.CLOSED_APPROVED,
        "CLOSED_REJECTED": ClaimStatus.CLOSED_REJECTED,
        "ESCALATED":       ClaimStatus.ESCALATED,
    }
    final_status = outcome_to_status.get(outcome, ClaimStatus.FAILED)

    from datetime import datetime, timezone
    extra = {"closedAt": datetime.now(timezone.utc).isoformat()}
    if settlement:
        extra["settlement"] = Decimal(settlement)
    if reason:
        extra["rejectionReason"] = reason
    if error:
        extra["errorDetails"] = error

    update_claim_status(claim_id, final_status, extra)

    # Stub: send SNS/SES notification to claimant
    logger.info("[CloseClaim] Notification sent for claim %s: %s", claim_id, outcome)

    return {"closed": True, "outcome": outcome}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _compute_fraud_score(claim_type: str, amount: float) -> float:
    """
    Stub fraud scoring — replace with SageMaker endpoint or rules engine.
    Returns a float between 0.0 (clean) and 1.0 (high risk).
    """
    score = 0.1
    if amount > 100_000:
        score += 0.3
    elif amount > 50_000:
        score += 0.15
    if claim_type == "LIABILITY":
        score += 0.1
    score += random.uniform(0, 0.1)  # jitter to simulate a real model
    return min(score, 1.0)
