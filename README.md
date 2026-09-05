# RecoverAI

**Autonomous Merchant Revenue Recovery Control Plane**  
*AI Recommends. Guardrails Authorize. Gateway Verifies. RecoverAI Learns.*

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14+-336791.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0+-red.svg)](https://www.sqlalchemy.org)
[![Razorpay](https://img.shields.io/badge/Razorpay-Payment%20Links%20API-0C2340.svg?logo=razorpay&logoColor=528FF0)](https://razorpay.com)
[![Tests](https://img.shields.io/badge/Tests-223%20Passed-22c55e.svg)](backend/tests)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org)

---

## 1. Executive Summary

In high-volume e-commerce and SaaS, payment failures represent massive recoverable top-line revenue. However, conventional recovery systems rely on blind, naive automatic retries. Retrying every failed transaction causes customer friction, triggers bank anti-fraud rate limits, wastes gateway fees, and fails when the failure is systemic (e.g., bank downtime or network timeouts).

**RecoverAI** is an intelligent revenue recovery control plane that closes the loop between telemetry, decisioning, execution, and verification:
1. **Detects** acute failure spikes at the cohort level (Bank × Method × Error Code × Pipeline Step).
2. **Diagnoses** the deterministic root cause behind the degradation.
3. **Evaluates** the economic yield of candidate recovery strategies.
4. **Advises** optimal actions using LLM-assisted strategy selection and Bayesian operational memory.
5. **Enforces** strict deterministic governance guardrails before taking any financial action.
6. **Executes** targeted, idempotent recovery via real Razorpay Payment Links.
7. **Verifies** recovered revenue strictly through cryptographically signed webhook receipts.
8. **Learns** from recovery outcomes to calibrate future decision thresholds.

> **Core Philosophy**:  
> *"AI recommends. Guardrails authorize. Gateway verifies. RecoverAI learns."*

---

## 2. The Problem: The Hidden Cost of Blind Retries

Payment failures are not homogeneous. When merchants treat all failures identically:
- **Systemic Outages Go Unaddressed**: An authorization timeout at HDFC UPI is fundamentally different from a customer entering an invalid CVV or having insufficient funds. Retrying a bad card payload repeatedly annoys customers and wastes resources.
- **Margin Degradation**: Blindly issuing discount coupons to recover payments destroys unit economics on healthy transactions.
- **Lack of Verification**: Systems often report "recovered" status as soon as an action is fired, rather than when cash actually clears the gateway.
- **Zero Memory**: Conventional systems do not track which recovery strategies work for specific cohorts over time.

Merchants need an **auditable control plane** that answers:
1. *Where am I losing money right now?* (Cohort Anomaly Detection)
2. *Why is this cohort failing?* (Causal Diagnosis)
3. *What is the safest, highest-yield action?* (Economic Evaluation & AI Recommendation)
4. *Does this action comply with safety limits?* (Deterministic Governance)
5. *Did the customer actually pay?* (Webhook Verification)

---

## 3. The RecoverAI Closed-Loop Lifecycle

```
Payment Signals (Gateway Telemetry)
       │
       ▼
Cohort Detection (Rolling Window vs. Baseline Multiplier)
       │
       ▼
Failure Diagnosis (Deterministic Causal Slicing)
       │
       ▼
Recovery Economics (Expected Yield, Cost, Margin Impact)
       │
       ▼
AI Recommendation (LLM-Assisted Strategy Synthesis)
       │
       ▼
Deterministic Guardrails (Hard Merchant Policy Enforcement)
       │
       ▼
Razorpay Execution (Smart Payment Links with Idempotency)
       │
       ▼
Webhook Verification (Signature-Verified Settlement)
       │
       ▼
Outcome Learning (Bayesian Operational Memory Calibration)
```

---

## 4. Key Differentiators

| Capability | Conventional Retry Logic | RecoverAI Control Plane |
| :--- | :--- | :--- |
| **Detection Granularity** | Isolated payment failure triggers | Multi-dimensional cohort aggregation (`Bank` × `Method` × `ErrorCode` × `ErrorStep`) |
| **Causal Intelligence** | None (Treats all errors identically) | Deterministic diagnostic engine identifying root cause concentration |
| **Action Selection** | Hardcoded static retry schedule | Dynamic economic ROI ranking with Bayesian prior calibration |
| **AI Governance** | Unconstrained LLM or no AI | **AI has zero execution authority**; deterministic guardrails enforce policy bounds |
| **Execution Safety** | Blind execution | Hard exposure caps, per-attempt incentive budgets, and action whitelisting |
| **Settlement Truth** | Assumed success upon dispatch | Cryptographic Razorpay webhook verification (`payment_link.paid`) |
| **System Memory** | Stateless execution | Hierarchical fallback operational memory with learning signals |

---

## 5. System Architecture

![RecoverAI Platform Architecture](assets/architecture.jpg)

```mermaid
flowchart TB
    subgraph Ingestion["1. Telemetry and Ingestion"]
        P[Payment Events] --> PE[Payment Normalizer]
        PE --> DB_P[(PostgreSQL: Payments)]
    end

    subgraph DetectionDiagnosis["2. Detection and Diagnosis Engine"]
        DB_P --> CD[Cohort Detector]
        CD -->|Spike Detection| INC[Incident Record]
        INC --> DIAG[Deterministic Diagnostic Engine]
        DIAG --> DIAG_REC[Diagnosis Record]
    end

    subgraph Decisioning["3. Economic Decisioning and AI Strategist"]
        DIAG_REC --> ECON[Economics Engine]
        MEM[(Operational Memory)] --> ECON
        ECON --> AI[AI Recovery Strategist / Fallback]
        AI --> PLAN[Proposed Recovery Action]
    end

    subgraph Governance["4. Deterministic Governance Guardrails"]
        PLAN --> GR{Guardrails Engine}
        POL[(Merchant Policy)] --> GR
        GR -->|Veto / Policy Breach| AUDIT[(Audit Trail)]
        GR -->|Authorized| APP[Approved Recovery Campaign]
    end

    subgraph ExecutionVerification["5. Execution and Gateway Verification"]
        APP --> RZP_EXE[Razorpay Payment Links Client]
        RZP_EXE -->|Generate Link| RZP_API[Razorpay API]
        RZP_API -->|Customer Pays| WH[Razorpay Webhook]
        WH --> SIG_VER[Signature Verification]
        SIG_VER --> SETTLE[State Transition: EXECUTED -> RECOVERED]
    end

    subgraph Learning["6. Outcome Learning Loop"]
        SETTLE --> LEARN[Outcome Evaluator]
        LEARN -->|Update Priors and Learning Signals| MEM
    end

    subgraph UI["7. Merchant Control Plane"]
        INC -.-> FE[Command Center]
        DIAG_REC -.-> FE[Incident Detail]
        PLAN -.-> FE[Decision Room]
        APP -.-> FE[Campaign Execution]
        AUDIT -.-> FE[Outcome and Trace]
    end
```

---

## 6. How It Works: Technical Deep Dive

### A. Cohort-Level Anomaly Detection
The detection engine groups transactions into rolling 30-minute observation windows against 1-hour historical baselines. An **Incident** is triggered when:
- Cohort sample volume $\ge 20$ transactions (`MIN_TRANSACTION_COUNT`)
- Absolute failure rate increases by $\ge 5\%$ points (`MIN_ABSOLUTE_INCREASE`)
- Current failure rate is $\ge 2.0	imes$ the baseline rate (`MIN_RELATIVE_MULTIPLIER`)

### B. Deterministic Causal Diagnosis
Rather than guessing, the diagnostic engine calculates concentration deltas across telemetry dimensions:
- **Bank-Specific Degradation**: Failure rate for bank $B$ on method $M$ is $\ge 10\%$ higher than all other banks on method $M$ (minimum 5 failures).
- **Payment-Method Degradation**: Method failure rate exceeds overall baseline by $\ge 15\%$.
- **Error-Code / Step Concentration**: Acute concentration in gateway timeouts (`GATEWAY_ERROR`) during the `payment_authorization` step.

### C. Recovery Economics Engine
For each candidate action (`retry`, `grace_period`, `incentive`, `ops_review`), the system evaluates:
$$	ext{Expected Net Recovery Value} = (	ext{Revenue at Risk} 	imes 	ext{Observed Recovery Rate}) - 	ext{Action Cost}$$
- **Action Cost**: ₹0 for smart retry / grace period; exact discount amount for incentives.
- **Incentive Budgeting**: Ensures proposed discounts do not exceed merchant policy limits.

### D. Adaptive Decision Engine & AI Strategist
- Synthesizes diagnostic evidence, economic ROI, and historical cohort priors.
- Evaluates Gemini / LLM recommendations against deterministic rules.
- If the AI model is unavailable, slow, or suggests an invalid action, the system falls back seamlessly to the highest expected net-recovery deterministic action.

### E. Deterministic Governance Guardrails
The guardrail engine acts as an immovable safety boundary:
- **Action Whitelist Check**: Rejects actions not permitted in merchant settings.
- **Per-Attempt Incentive Cap**: Enforces `max_incentive` (e.g., max ₹1,000 / attempt).
- **Active Exposure Cap**: Sums active pending/executed recovery incentives across the merchant account and blocks execution if `max_exposure` (e.g., ₹20,000) would be breached.
- **Manual Review Gate**: Flags high-risk actions as `requires_approval`.

### F. Execution via Razorpay Smart Payment Links
- Generates customer payment links with autonomous idempotency.
- Injects critical tracking metadata (`recovery_id`, `incident_id`, `merchant_id`).
- Creates an immutable `RecoveryAuditEvent` logging actor, rationale, and state transitions.

### G. Webhook-Confirmed Settlement
- Ingests `payment_link.paid` webhook events.
- Validates cryptographic SHA256 HMAC signature using `X-Razorpay-Signature`.
- Matches `reference_id` to the target `RecoveryAttempt`.
- Transitions state from `executed` to `recovered` and records `recovered_amount`.

### H. Operational Memory & Learning
Tracks 7 distinct learning signals across the decision lifecycle:
1. `SUCCESSFUL_DECISION`: Action resulted in verified recovery within expected yield.
2. `OVER_INTERVENTION`: Incentive was offered when a zero-cost retry would have succeeded.
3. `UNDER_INTERVENTION`: Passive action failed where an active link was required.
4. `RECOVERY_OUTPERFORMANCE`: Recovery rate exceeded prior estimates.
5. `RECOVERY_UNDERPERFORMANCE`: Recovery rate fell below expected ROI.
6. `GUARDRAIL_VETO`: AI recommendation breached policy and was safely overridden.
7. `AI_FALLBACK`: Graceful degradation executed when LLM was unreachable.

---

## 7. AI Safety: The Advisory Boundary

In financial systems, unconstrained AI agents pose severe risks (hallucinated payouts, policy violations, unauthorized execution). RecoverAI strictly implements an **advisory-only AI layer**:

```
                 ┌─────────────────────────────┐
                 │   LLM Strategy Advisor      │
                 │   (Advisory Recommendation) │
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │  Deterministic Policy Gate  │
                 │  (Allowed Actions Whitelist)│
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │ Financial Exposure Guardrail│
                 │ (Incentive & Exposure Caps) │
                 └──────────────┬──────────────┘
                                │
                        [ Passes All Checks ]
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │  Approved Recovery Attempt  │
                 │  (Execution Authorized)     │
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │  Razorpay Gateway Execution │
                 └─────────────────────────────┘
```

---

## 8. Progressive Operational Memory

When estimating recovery rates, RecoverAI utilizes a hierarchical fallback structure to avoid sample starvation while guaranteeing merchant data isolation:

```
Level 1: Exact Cohort Match      (Method + Bank + ErrorCode + ErrorStep)
   │ (Insufficient samples < 5)
   ▼
Level 2: Method + Bank + Error
   │ (Insufficient samples < 5)
   ▼
Level 3: Method + ErrorCode
   │ (Insufficient samples < 5)
   ▼
Level 4: Payment Method Only
   │ (Insufficient samples < 5)
   ▼
Level 5: Merchant Global Baseline
   │ (Zero history)
   ▼
Level 6: Cold-Start Configured Fallback (30% Retry, 20% Grace Period, 25% Incentive, 5% Ops Review)
```

---

## 9. Repository Structure

```
recoverai/
├── backend/
│   ├── alembic/                      # Database migrations (13 revisions)
│   │   └── versions/
│   ├── app/
│   │   ├── api/                      # FastAPI endpoint routers
│   │   │   └── routes/
│   │   │       ├── merchant.py       # Control plane API (incidents, recoveries, telemetry)
│   │   │       └── webhooks.py       # Cryptographic Razorpay webhook handler
│   │   ├── core/                     # Application configuration & settings
│   │   ├── db/                       # SQLAlchemy session & dependency injection
│   │   ├── domains/                  # Modular domain-driven business logic
│   │   │   ├── cohorts/              # Cohort aggregation & anomaly detection
│   │   │   ├── diagnosis/            # Deterministic root-cause analyzer
│   │   │   ├── payments/             # Ingestion & event normalization
│   │   │   └── recovery/             # Economics, guardrails, memory, execution, strategist
│   │   ├── integrations/             # Gateway & external provider adapters
│   │   │   ├── llm/                  # Gemini / LLM provider interfaces
│   │   │   └── razorpay/             # Razorpay API client & webhook signature verifier
│   │   ├── models/                   # SQLAlchemy database entity models
│   │   └── schemas/                  # Pydantic request/response validation schemas
│   ├── scripts/
│   │   └── seed_trial_data.py        # Deterministic golden scenario trial seeder
│   └── tests/                        # Pytest automated test suite (24 test modules)
├── frontend/
│   ├── assets/                       # Brand identity assets (transparent logo)
│   ├── js/
│   │   └── api.js                    # Live REST client consuming merchant APIs
│   ├── views/                        # Composable SPA view templates
│   │   ├── layout_top.html           # Brand header & navigation
│   │   ├── view_command.html         # View 1: Command Center
│   │   ├── view_incidents.html       # View 2: Incident Detail & Telemetry
│   │   ├── view_decision.html        # View 3: Decision Room & Strategy
│   │   ├── view_campaign.html        # View 4: Live Recovery Campaign & Dispatch
│   │   ├── view_trace.html           # View 5: Audit Trail & State Transitions
│   │   └── layout_bottom.html        # State engine, demo simulation & footer
│   ├── index.html                    # Compiled Single Page Application
│   └── serve.py                      # Dedicated frontend HTTP server (Port 5173)
└── README.md
```

---

## 10. The Golden Demo Scenario

The repository includes a deterministic trial scenario configured via `backend/scripts/seed_trial_data.py`:

- **Merchant**: Trial Merchant (`b614b90f-49fd-4d6a-8689-52d4d2878b03`)
- **Degraded Cohort**: `HDFC` + `UPI` + `GATEWAY_ERROR` + `payment_authorization`
- **Window Traffic**: 28 total transactions (20 captured, 8 failed)
- **Failure Elevation**: 28.6% failure rate vs. 3.3% baseline ($8.6	imes$ elevation)
- **Revenue at Risk**: **₹14,000** (derived from actual failed checkout values: ₹1,250, ₹2,400, ₹850, ₹3,200, ₹1,750, ₹2,100, ₹950, ₹1,500)
- **Diagnosis**: Deterministic payment authorization degradation due to upstream gateway timeouts.
- **Historical Evidence**: 5 prior comparable retry attempts with a 100% observed recovery rate.
- **Expected Net Recovery**: **₹14,000** (100% yield, ₹0 discount cost).
- **Verified Recovered**: **₹0** initially $
ightarrow$ Increases dynamically as real/simulated Razorpay payment links settle.

---

## 11. Automated Verification & Testing

The backend is backed by an automated test suite verifying financial invariants, guardrail safety, webhook settlement, and idempotency:

```bash
$env:PYTHONPATH="backend"; $env:DATABASE_URL="postgresql+psycopg://postgres:admin@localhost:5432/recoverai"; python -m pytest backend/tests/ -q
```

**Results**:
```
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 96%]
.......                                                                  [100%]
============================== 223 passed in 13.99s ===============================
```

### Key Test Suites:
- `test_recovery_guardrails.py`: Verifies exposure caps, action whitelists, and veto logic.
- `test_recovery_economics.py`: Validates financial arithmetic and net ROI calculations.
- `test_operational_memory.py`: Tests 6-level hierarchical fallback and learning signals.
- `test_razorpay_webhook.py`: Verifies cryptographic signature verification and idempotent settlement.
- `test_recovery_invariants.py`: Ensures money conservation invariants are never violated.

---

## 12. Local Setup & Quickstart

### Prerequisites
- **Python**: 3.11 or higher
- **PostgreSQL**: 14 or higher running on `localhost:5432`
- **Modern Web Browser**

### 1. Clone & Configure Environment
Create a `.env` file in the `backend/` directory:

```env
DATABASE_URL=postgresql+psycopg://postgres:admin@localhost:5432/recoverai
RAZORPAY_KEY_ID=rzp_test_placeholder
RAZORPAY_KEY_SECRET=razorpay_secret_placeholder
RAZORPAY_WEBHOOK_SECRET=webhook_secret_placeholder
DEV_MERCHANT_ID=b614b90f-49fd-4d6a-8689-52d4d2878b03
ENVIRONMENT=development
AUTH_MODE=development
```

### 2. Install Dependencies
```bash
pip install -r backend/requirements.txt
```

### 3. Run Database Migrations
```bash
cd backend
alembic upgrade head
```

### 4. Seed the Golden Trial Scenario
```bash
python scripts/seed_trial_data.py
```

### 5. Start the FastAPI Backend
```bash
# From project root
$env:PYTHONPATH="backend"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
API Documentation will be live at `http://127.0.0.1:8000/docs`.

### 6. Start the Frontend Application
In a separate terminal:
```bash
python frontend/serve.py
```
Open your browser at **`http://127.0.0.1:5173`**.

---

## 13. Guided Demo Walkthrough

1. **Command Center (`/`)**:
   - Inspect the top-level **₹14,000 Revenue at Risk** visual anchor.
   - Observe the active degradation badge in **HDFC UPI** authorization traffic.
2. **Incident Detail (`Incident Detail` tab)**:
   - Review baseline failure rate (3.3%) vs. acute failure rate (28.6%).
   - Inspect the causal evidence cluster confirming upstream gateway timeout concentrations.
3. **Decision Room (`Decision Room` tab)**:
   - Review the AI recommendation (**RETRY** with 100% expected yield).
   - Inspect the 4 deterministic governance gates (Action Whitelist, Incentive Budget, Exposure Cap, Authorization Gate).
4. **Recovery Campaign (`Recovery Campaign` tab)**:
   - Click **Dispatch Payment Links** to approve and execute recovery.
   - Observe real Razorpay test-mode payment link generation.
   - Click **Pay via Razorpay Test Mode** to simulate customer settlement.
5. **Outcome & Trace (`Outcome & Trace` tab)**:
   - Inspect the immutable audit log displaying state transitions from `PENDING` -> `APPROVED` -> `EXECUTED` -> `RECOVERED`.

---

## 14. Engineering Principles & Invariants

- **Financial Conservatism**: Never claim revenue as recovered until confirmed by a signed gateway webhook.
- **Strict Merchant Isolation**: Operational memory and policy thresholds never bleed across merchant boundaries.
- **Idempotent Operations**: Repeated webhook deliveries or duplicate execution clicks never double-recover or double-charge.
- **Fail-Safe Fallbacks**: If the AI strategist experiences latency or error, the system falls back immediately to deterministic economic models.
- **Immutable Compliance**: Every approval, override, and execution writes a permanent audit record with actor attribution.

---

## 15. Limitations & Future Roadmap

- **Distributed Workers**: Current execution and webhook sync run synchronously within FastAPI; production deployment will transition background jobs to Celery/Temporal with Redis brokers.
- **Public Webhook Tunneling**: Local webhook testing requires ngrok or reverse tunneling; production integrates direct HTTPS webhook endpoints with edge rate-limiting.
- **Multi-Gateway Connectors**: Razorpay is implemented as the primary integration; future iterations will support Stripe, Adyen, and PayU adapters.
- **Contextual Multi-Armed Bandits**: Expanding operational memory into Thompson sampling for optimal exploration-exploitation trade-offs across recovery channels.

---

<p align="center">
  <b>RecoverAI</b> · <i>Merchant Revenue Recovery Control Plane</i><br/>
  Detection • Diagnosis • Decisioning • Governance • Recovery • Verification • Learning
</p>
