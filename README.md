# COrE — Claim Orchestration Engine (Python)

Insurance claim automation POC using **AWS Step Functions** as the orchestration backbone, **Serverless Framework** for deployment, and **Python 3.11** for all Lambda handlers.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          API Gateway                                 │
│  POST /claims          GET /claims       GET /claims/{id}            │
│  POST /claims/{id}/adjuster-review                                   │
│  POST /claims/{id}/supervisor-approval                               │
└────────────────────────────┬────────────────────────────────────────┘
                             │  Lambda (Python 3.11)
                             │
        ┌────────────────────▼──────────────────────┐
        │           DynamoDB                         │
        │  claims-table    ←→   tokens-table         │
        │  (claim state)        (task tokens w/ TTL) │
        └────────────────────┬──────────────────────┘
                             │
        ┌────────────────────▼──────────────────────┐
        │       Step Functions (Standard Workflow)   │
        │                                            │
        │  ┌──────────┐                              │
        │  │AutoTriage│ ← Lambda (automated)         │
        │  └────┬─────┘                              │
        │       │                                    │
        │  ┌────▼──────────────┐                     │
        │  │ AdjusterReviewWait│ ← PAUSED ⏸          │
        │  │ (WaitForTaskToken)│   until adjuster     │
        │  └────┬──────────────┘   calls API         │
        │       │                                    │
        │  ┌────▼───────────────────┐                │
        │  │ SupervisorApprovalWait │ ← PAUSED ⏸     │
        │  │ (if amount > $10,000)  │   if needed     │
        │  └────┬───────────────────┘                │
        │       │                                    │
        │  ┌────▼───────────┐                        │
        │  │PrepareSettlement│ ← Lambda (automated)  │
        │  └────┬────────────┘                       │
        │       │                                    │
        │  ┌────▼──────┐                             │
        │  │ CloseClaim │ ← Lambda (terminal)        │
        │  └───────────┘                             │
        └───────────────────────────────────────────┘
```

---

## Claim Workflow (5 Steps)

| Step | Type | Description | Timeout |
|------|------|-------------|---------|
| **AutoTriage** | Automated | Fraud scoring, priority assignment | ~2s |
| **AdjusterReviewWait** | 🧑 Human | Adjuster approves/rejects | 7 days |
| **SupervisorApprovalWait** | 🧑 Human | Required for claims > $10,000 | 3 days |
| **PrepareSettlement** | Automated | Calculates payout, generates docs | ~5s |
| **CloseClaim** | Automated | Notifies claimant, audit record | ~2s |

### Claim Status Flow

```
SUBMITTED → IN_TRIAGE → PENDING_ADJUSTER → PENDING_SUPERVISOR → IN_SETTLEMENT → CLOSED_APPROVED
                                        ↘                     ↘
                                          CLOSED_REJECTED       ESCALATED (timeout)
```

---

## The WaitForTaskToken Pattern

```
1. Step Functions reaches AdjusterReviewWait
2. Invokes auto_triage Lambda WITH task token in payload ($$.Task.Token)
3. Lambda stores token in DynamoDB (tokens-table with TTL) and returns
4. Execution is PAUSED — stays paused for days/weeks
5. Adjuster calls POST /claims/{id}/adjuster-review
6. actions.py retrieves token from DynamoDB
7. Calls sfn.send_task_success(token, result) → execution RESUMES
```

---

## Project Structure

```
insurance-core/
├── serverless.yml                   # Infra: API GW, Lambdas, DynamoDB, Step Functions
├── requirements.txt                 # boto3, PyJWT
├── statemachine/
│   └── claim-workflow.json          # Step Functions ASL (language-agnostic)
└── src/
    ├── handlers/
    │   ├── authorizer.py            # JWT Lambda authorizer → injects userId + role
    │   ├── claims.py                # submit / get_claim / list_claims
    │   ├── actions.py               # adjuster_review / supervisor_approval (token resume)
    │   └── workers.py               # auto_triage / prepare_settlement / close_claim
    ├── models/
    │   └── claim.py                 # DynamoDB access layer + schema docs
    └── lib/
        └── helpers.py               # Roles, permissions, HTTP response helpers
```

---

## User Roles & Permissions

| Role | Can Do |
|------|--------|
| `CLAIMANT` | Submit claims, view own claims |
| `ADJUSTER` | View all claims, perform adjuster review |
| `SUPERVISOR` | View all claims, perform supervisor approval |
| `ADMIN` | All of the above |

JWT payload: `{ "sub": "userId", "role": "ADJUSTER", "email": "..." }`

---

## API Reference

### Submit a Claim
```
POST /claims
Authorization: Bearer <claimant-jwt>
{ "policyNumber": "POL-12345", "claimType": "AUTO", "amount": 15000, "description": "..." }
→ 201 { claimId, executionArn, status: "SUBMITTED" }
```

### Get a Claim
```
GET /claims/{claimId}
Authorization: Bearer <any-role-jwt>
→ 200 { claimId, status, triage, adjusterReview, ... }
```

### List Claims
```
GET /claims?status=PENDING_ADJUSTER
GET /claims?assignedTo=user-123
Authorization: Bearer <adjuster-jwt>
→ 200 { claims: [...], count: N }
```

### Adjuster Review
```
POST /claims/{claimId}/adjuster-review
Authorization: Bearer <adjuster-jwt>
{ "decision": "APPROVED", "notes": "Damage consistent with report.", "settlementRecommendation": 13500 }
→ 200 { message, claimId, decision }
```

### Supervisor Approval
```
POST /claims/{claimId}/supervisor-approval
Authorization: Bearer <supervisor-jwt>
{ "decision": "APPROVED", "notes": "Within policy limits.", "finalAmount": 13500 }
→ 200 { message, claimId, decision }
```

---

## Setup & Deployment

### Prerequisites
- Python 3.11
- Node.js (for Serverless Framework)
- AWS CLI configured

### Install
```bash
npm install -g serverless
npm install   # installs serverless-offline and serverless-python-requirements

pip install -r requirements.txt   # for local dev/testing
```

### Deploy
```bash
serverless deploy --stage dev
```

### Local Dev
```bash
serverless offline
# API at http://localhost:3000
```

### Generate Test JWTs (dev only)
```python
import jwt

SECRET = "dev-secret-change-in-prod"

# Claimant
jwt.encode({"sub": "user-1", "role": "CLAIMANT",   "email": "alice@example.com"}, SECRET, algorithm="HS256")

# Adjuster
jwt.encode({"sub": "user-2", "role": "ADJUSTER",   "email": "bob@example.com"},   SECRET, algorithm="HS256")

# Supervisor
jwt.encode({"sub": "user-3", "role": "SUPERVISOR", "email": "carol@example.com"}, SECRET, algorithm="HS256")
```

---

## Production Hardening Checklist

- [ ] Replace `JWT_SECRET` with AWS Secrets Manager / Parameter Store reference
- [ ] Add Cognito or an IdP instead of raw JWT signing
- [ ] Split `auto_triage` into dedicated `save_token` Lambda per wait step
- [ ] Add SQS DLQ on all Lambda functions
- [ ] Add CloudWatch alarms on escalated claims and execution failures
- [ ] Define a state machine versioning strategy before first prod deploy
- [ ] Add end-to-end integration tests that drive a full claim through the workflow
- [ ] Replace stub fraud scoring with SageMaker endpoint
- [ ] Add SES/SNS notifications for claimant status updates
