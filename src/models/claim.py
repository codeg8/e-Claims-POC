# src/models/claim.py
# DynamoDB access layer for Claims and Task Tokens tables.
#
# ── Claims table schema ────────────────────────────────────────────────
# PK: claimId (str, UUID)
#
# {
#   claimId:            "uuid-v4",
#   policyNumber:       "POL-12345",
#   claimType:          "AUTO" | "PROPERTY" | "HEALTH" | "LIABILITY",
#   amount:             Decimal("12500.00"),
#   description:        "...",
#   claimantId:         "user-id",
#   assignedTo:         "adjuster-user-id",      # drives assignedTo-index GSI
#   status:             ClaimStatus.*,            # drives status-createdAt-index GSI
#   executionArn:       "arn:aws:states:...",
#   triage:             { fraudScore, priority, triageNotes },
#   adjusterReview:     { decision, notes, reviewedBy, reviewedAt },
#   supervisorApproval: { decision, notes, approvedBy, approvedAt },
#   settlement:         { settlementAmount, paymentRef, documentsGenerated },
#   createdAt:          "ISO-8601",
#   updatedAt:          "ISO-8601",
# }
#
# ── Tokens table schema ────────────────────────────────────────────────
# PK: claimId (str)  |  SK: step (str)
#
# {
#   claimId:   "uuid-v4",
#   step:      "ADJUSTER_REVIEW" | "SUPERVISOR_APPROVAL",
#   taskToken: "<sfn-task-token>",
#   savedAt:   "ISO-8601",
#   ttl:       1234567890,   # Unix epoch — DynamoDB auto-deletes expired tokens
#   consumed:  False,
#   consumedAt: "ISO-8601"   # set when consumed
# }

import os
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")

CLAIMS_TABLE = os.environ["CLAIMS_TABLE"]
TOKENS_TABLE = os.environ["TOKENS_TABLE"]

_claims_table = dynamodb.Table(CLAIMS_TABLE)
_tokens_table = dynamodb.Table(TOKENS_TABLE)


# ─────────────────────────────────────────────
# CLAIM STATUS ENUM
# Maps 1:1 with Step Functions state names so
# the DB always reflects where the execution is.
# ─────────────────────────────────────────────
class ClaimStatus:
    SUBMITTED          = "SUBMITTED"
    IN_TRIAGE          = "IN_TRIAGE"
    PENDING_ADJUSTER   = "PENDING_ADJUSTER"
    PENDING_SUPERVISOR = "PENDING_SUPERVISOR"
    IN_SETTLEMENT      = "IN_SETTLEMENT"
    CLOSED_APPROVED    = "CLOSED_APPROVED"
    CLOSED_REJECTED    = "CLOSED_REJECTED"
    ESCALATED          = "ESCALATED"
    FAILED             = "FAILED"

    ALL = [
        SUBMITTED, IN_TRIAGE, PENDING_ADJUSTER, PENDING_SUPERVISOR,
        IN_SETTLEMENT, CLOSED_APPROVED, CLOSED_REJECTED, ESCALATED, FAILED,
    ]


# Token TTL in seconds from now
TOKEN_TTL: dict[str, int] = {
    "ADJUSTER_REVIEW":    7 * 24 * 60 * 60,   # 7 days
    "SUPERVISOR_APPROVAL": 3 * 24 * 60 * 60,  # 3 days
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# CLAIMS
# ─────────────────────────────────────────────

def create_claim(claim: dict) -> dict:
    """
    Write a new claim record. Raises ConditionalCheckFailedException
    if a claim with the same claimId already exists.
    """
    now = _now_iso()
    item = {
        **claim,
        "status":    ClaimStatus.SUBMITTED,
        "createdAt": now,
        "updatedAt": now,
    }
    _claims_table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(claimId)",
    )
    return item


def get_claim(claim_id: str) -> dict | None:
    result = _claims_table.get_item(Key={"claimId": claim_id})
    return result.get("Item")


def update_claim_status(claim_id: str, status: str, extra_fields: dict | None = None) -> None:
    """
    Update the status (and any extra_fields) on a claim.
    extra_fields keys map directly to DynamoDB attribute names.
    """
    now = _now_iso()
    extra_fields = extra_fields or {}

    # Build a dynamic UpdateExpression
    set_parts = ["#status = :status", "updatedAt = :updatedAt"]
    expr_names = {"#status": "status"}  # 'status' is a DDB reserved word
    expr_values = {":status": status, ":updatedAt": now}

    for key, value in extra_fields.items():
        set_parts.append(f"{key} = :{key}")
        expr_values[f":{key}"] = value

    _claims_table.update_item(
        Key={"claimId": claim_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ConditionExpression="attribute_exists(claimId)",
    )


def list_claims_by_status(status: str) -> list[dict]:
    result = _claims_table.query(
        IndexName="status-createdAt-index",
        KeyConditionExpression=Key("status").eq(status),
        ScanIndexForward=False,  # newest first
    )
    return result.get("Items", [])


def list_claims_by_assignee(assigned_to: str) -> list[dict]:
    result = _claims_table.query(
        IndexName="assignedTo-index",
        KeyConditionExpression=Key("assignedTo").eq(assigned_to),
        ScanIndexForward=False,
    )
    return result.get("Items", [])


# ─────────────────────────────────────────────
# TASK TOKENS
# ─────────────────────────────────────────────

def save_task_token(claim_id: str, step: str, task_token: str) -> None:
    """
    Store the WaitForTaskToken task token so the API handler can
    retrieve it later and call SendTaskSuccess to resume the execution.
    """
    ttl_seconds = TOKEN_TTL.get(step, 86400)
    ttl = int(time.time()) + ttl_seconds

    _tokens_table.put_item(Item={
        "claimId":   claim_id,
        "step":      step,
        "taskToken": task_token,
        "savedAt":   _now_iso(),
        "ttl":       ttl,
        "consumed":  False,
    })


def get_task_token(claim_id: str, step: str) -> dict | None:
    result = _tokens_table.get_item(Key={"claimId": claim_id, "step": step})
    return result.get("Item")


def mark_token_consumed(claim_id: str, step: str) -> None:
    _tokens_table.update_item(
        Key={"claimId": claim_id, "step": step},
        UpdateExpression="SET #c = :true, consumedAt = :now",
        ExpressionAttributeNames={"#c": "consumed"},
        ExpressionAttributeValues={":true": True, ":now": _now_iso()},
    )
