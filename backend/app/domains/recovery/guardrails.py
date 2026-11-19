import logging
import uuid
from typing import Any, Literal
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.payment import Payment
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign

logger = logging.getLogger(__name__)

GuardrailDecision = Literal["allowed", "blocked", "requires_approval"]


class CheckResult(BaseModel):
    check_name: str = Field(..., description="Name of the safety guardrail check")
    passed: bool = Field(..., description="Whether this specific check passed")
    reason_code: str = Field(..., description="Machine-readable outcome code")
    explanation: str = Field(..., description="Human-readable explanation of check result")
    details: dict[str, Any] = Field(default_factory=dict, description="Detailed metrics or values evaluated")


class GuardrailResult(BaseModel):
    decision: GuardrailDecision = Field(..., description="Overall decision: allowed, blocked, or requires_approval")
    reason_code: str = Field(..., description="Primary machine-readable reason code")
    explanation: str = Field(..., description="Human-readable summary of decision")
    audit_event_type: str = Field(..., description="Audit event category for compliance logging")
    checks: list[CheckResult] = Field(default_factory=list, description="All evaluated checks")
    evaluated_exposure: dict[str, Any] = Field(default_factory=dict, description="Financial exposure metrics evaluated")
    policy_values: dict[str, Any] = Field(default_factory=dict, description="Merchant policy thresholds applied")


class GuardrailViolationError(ValueError):
    """
    Raised when execution or action cannot proceed due to a blocking guardrail violation.
    """
    def __init__(self, message: str, result: GuardrailResult):
        super().__init__(message)
        self.result = result


def evaluate_campaign_guardrails(
    db: Session,
    incident: Incident,
    policy: RecoveryPolicy,
    proposed_action: str,
    per_attempt_incentive: int,
    target_payments: list[Payment],
) -> GuardrailResult:
    """
    Evaluates whether a proposed recovery campaign action and parameters comply
    with merchant safety policies and financial sanity bounds.

    Returns a structured GuardrailResult with decision 'allowed', 'blocked',
    or 'requires_approval'.
    """
    checks: list[CheckResult] = []
    policy_merchant_id = policy.merchant_id
    target_count = len(target_payments)
    total_incentive_cost = per_attempt_incentive * target_count if proposed_action == "incentive" else 0

    # 1. Query cumulative active merchant exposure
    raw_exposure = db.scalar(
        select(func.sum(RecoveryAttempt.incentive_amount)).where(
            RecoveryAttempt.merchant_id == policy_merchant_id,
            RecoveryAttempt.status.in_(["pending", "approved", "executed"]),
        )
    )
    active_exposure = int(raw_exposure) if raw_exposure is not None else 0

    evaluated_exposure = {
        "target_payment_count": target_count,
        "per_attempt_incentive": int(per_attempt_incentive),
        "total_incentive_cost": int(total_incentive_cost),
        "active_exposure": active_exposure,
        "projected_total_exposure": active_exposure + total_incentive_cost,
    }

    policy_values = {
        "allowed_actions": list(policy.allowed_actions),
        "max_incentive": policy.max_incentive,
        "max_exposure": policy.max_exposure,
        "approval_threshold": policy.approval_threshold,
    }

    # CHECK 1: Merchant Isolation
    isolation_passed = True
    isolation_explanation = "All entities belong to merchant."
    if incident.merchant_id != policy_merchant_id:
        isolation_passed = False
        isolation_explanation = f"Incident merchant {incident.merchant_id} does not match policy merchant {policy_merchant_id}."
    else:
        for p in target_payments:
            if p.merchant_id != policy_merchant_id:
                isolation_passed = False
                isolation_explanation = f"Target payment {p.id} merchant {p.merchant_id} does not match policy merchant {policy_merchant_id}."
                break

    checks.append(CheckResult(
        check_name="merchant_isolation",
        passed=isolation_passed,
        reason_code="MERCHANT_ISOLATION_VERIFIED" if isolation_passed else "MERCHANT_ISOLATION_VIOLATION",
        explanation=isolation_explanation,
    ))

    # CHECK 2: Payment Validity
    payment_validity_passed = True
    payment_validity_explanation = f"Valid failed payments ({target_count}) present."
    if target_count == 0:
        payment_validity_passed = False
        payment_validity_explanation = "No target payments provided for recovery campaign."
    else:
        for p in target_payments:
            if p.status != "failed":
                payment_validity_passed = False
                payment_validity_explanation = f"Target payment {p.id} has status '{p.status}', expected 'failed'."
                break

    checks.append(CheckResult(
        check_name="payment_validity",
        passed=payment_validity_passed,
        reason_code="PAYMENTS_VALID" if payment_validity_passed else "INVALID_PAYMENTS",
        explanation=payment_validity_explanation,
        details={"target_count": target_count},
    ))

    # CHECK 3: Action Authorization
    action_allowed = proposed_action in policy.allowed_actions
    checks.append(CheckResult(
        check_name="action_authorization",
        passed=action_allowed,
        reason_code="ACTION_ALLOWED" if action_allowed else "ACTION_DISALLOWED",
        explanation=f"Action '{proposed_action}' is permitted by policy." if action_allowed else f"Action '{proposed_action}' is not in policy allowed_actions: {policy.allowed_actions}.",
        details={"proposed_action": proposed_action, "allowed_actions": policy.allowed_actions},
    ))

    # CHECK 4: Financial Sanity & Non-Negativity
    financial_sanity_passed = True
    financial_explanation = "All financial parameters are positive and sound."
    if per_attempt_incentive < 0:
        financial_sanity_passed = False
        financial_explanation = f"Incentive amount ({per_attempt_incentive}) cannot be negative."
    else:
        for p in target_payments:
            if p.amount <= 0:
                financial_sanity_passed = False
                financial_explanation = f"Payment {p.id} has non-positive amount ({p.amount})."
                break
            if proposed_action == "incentive" and per_attempt_incentive >= p.amount:
                financial_sanity_passed = False
                financial_explanation = f"Incentive ({per_attempt_incentive}) must be strictly less than payment amount ({p.amount})."
                break

    checks.append(CheckResult(
        check_name="financial_sanity",
        passed=financial_sanity_passed,
        reason_code="FINANCIAL_SANITY_VERIFIED" if financial_sanity_passed else "FINANCIAL_SANITY_VIOLATION",
        explanation=financial_explanation,
    ))

    # CHECK 5: Per-Attempt Incentive Cap
    incentive_cap_passed = True
    incentive_explanation = "Per-attempt incentive within cap."
    if proposed_action == "incentive":
        if per_attempt_incentive > policy.max_incentive:
            incentive_cap_passed = False
            incentive_explanation = f"Incentive amount {per_attempt_incentive} exceeds policy max_incentive {policy.max_incentive}."

    checks.append(CheckResult(
        check_name="incentive_cap",
        passed=incentive_cap_passed,
        reason_code="INCENTIVE_WITHIN_CAP" if incentive_cap_passed else "INCENTIVE_EXCEEDS_CAP",
        explanation=incentive_explanation,
        details={"per_attempt_incentive": per_attempt_incentive, "max_incentive": policy.max_incentive},
    ))

    # CHECK 6: Campaign Aggregate Exposure Cap
    exposure_cap_passed = True
    exposure_explanation = "Aggregate exposure within policy limit."
    if proposed_action == "incentive":
        projected_exposure = active_exposure + total_incentive_cost
        if projected_exposure > policy.max_exposure:
            exposure_cap_passed = False
            exposure_explanation = (
                f"Projected campaign exposure ({projected_exposure}) exceeds "
                f"policy max_exposure ({policy.max_exposure})."
            )

    checks.append(CheckResult(
        check_name="campaign_exposure",
        passed=exposure_cap_passed,
        reason_code="EXPOSURE_WITHIN_CAP" if exposure_cap_passed else "CAMPAIGN_EXPOSURE_EXCEEDED",
        explanation=exposure_explanation,
        details={
            "total_incentive_cost": total_incentive_cost,
            "active_exposure": active_exposure,
            "max_exposure": policy.max_exposure,
        },
    ))

    # Determine Decision:
    # Any failed hard check blocks the campaign action
    blocking_checks = [c for c in checks if not c.passed]
    if blocking_checks:
        first_fail = blocking_checks[0]
        return GuardrailResult(
            decision="blocked",
            reason_code=first_fail.reason_code,
            explanation=f"Guardrail blocked proposed action: {first_fail.explanation}",
            audit_event_type="GUARDRAIL_BLOCKED",
            checks=checks,
            evaluated_exposure=evaluated_exposure,
            policy_values=policy_values,
        )

    # CHECK 7: Approval Threshold
    requires_approval = False
    approval_explanation = "Action requires operator approval before execution."
    approval_code = "WITHIN_POLICY"

    if proposed_action == "ops_review":
        requires_approval = True
        approval_code = "OPS_REVIEW_REQUIRES_APPROVAL"
        approval_explanation = "Action 'ops_review' requires human authorization."
    elif proposed_action == "incentive" and total_incentive_cost > policy.approval_threshold:
        requires_approval = True
        approval_code = "EXPOSURE_REQUIRES_APPROVAL"
        approval_explanation = (
            f"Campaign incentive cost ({total_incentive_cost}) exceeds "
            f"approval threshold ({policy.approval_threshold})."
        )

    checks.append(CheckResult(
        check_name="approval_threshold",
        passed=not requires_approval,
        reason_code="APPROVAL_NOT_REQUIRED" if not requires_approval else approval_code,
        explanation="Action is automatically executable." if not requires_approval else approval_explanation,
        details={
            "total_incentive_cost": total_incentive_cost,
            "approval_threshold": policy.approval_threshold,
        },
    ))

    if requires_approval:
        return GuardrailResult(
            decision="requires_approval",
            reason_code=approval_code,
            explanation=approval_explanation,
            audit_event_type="GUARDRAIL_APPROVAL_REQUIRED",
            checks=checks,
            evaluated_exposure=evaluated_exposure,
            policy_values=policy_values,
        )

    return GuardrailResult(
        decision="allowed",
        reason_code="WITHIN_POLICY",
        explanation=f"Action '{proposed_action}' complies with all safety policies and is approved for automatic execution.",
        audit_event_type="GUARDRAIL_APPROVED",
        checks=checks,
        evaluated_exposure=evaluated_exposure,
        policy_values=policy_values,
    )


def evaluate_execution_guardrails(
    db: Session,
    attempt: RecoveryAttempt,
    policy: RecoveryPolicy | None = None,
) -> GuardrailResult:
    """
    Defensively validates a RecoveryAttempt immediately before creating an
    external payment link with Razorpay.

    Ensures:
    1. Attempt lifecycle state is strictly 'approved'.
    2. Attempt has not already been executed, recovered, failed, or expired.
    3. Associated campaign is not 'completed'.
    4. Target payment exists, is failed, and has positive amount.
    5. Merchant isolation holds across Attempt, Payment, Incident, and Campaign.
    6. Charge amount (payment.amount - incentive) is strictly positive.
    7. Policy allows the selected action and incentive (if policy provided or found).
    """
    checks: list[CheckResult] = []
    merchant_id = attempt.merchant_id

    # 1. Target Payment Validity
    payment_exists = attempt.payment_id is not None and attempt.payment is not None
    payment_failed = payment_exists and attempt.payment.status == "failed"
    payment_positive = payment_exists and attempt.payment.amount > 0

    checks.append(CheckResult(
        check_name="payment_presence",
        passed=payment_exists,
        reason_code="PAYMENT_PRESENT" if payment_exists else "MISSING_PAYMENT",
        explanation="Target payment is present." if payment_exists else f"RecoveryAttempt {attempt.recovery_id} cannot be executed without an associated Payment.",
    ))

    checks.append(CheckResult(
        check_name="payment_status_failed",
        passed=payment_failed,
        reason_code="PAYMENT_FAILED" if payment_failed else "PAYMENT_NOT_FAILED",
        explanation="Target payment has status 'failed'." if payment_failed else f"Target payment status is '{attempt.payment.status if attempt.payment else None}', expected 'failed'.",
    ))

    # 2. Merchant Isolation
    isolation_passed = True
    isolation_explanation = "All linked entities belong to the merchant."
    if payment_exists and attempt.payment.merchant_id != merchant_id:
        isolation_passed = False
        isolation_explanation = f"Payment merchant {attempt.payment.merchant_id} does not match attempt merchant {merchant_id}."
    elif attempt.incident and attempt.incident.merchant_id != merchant_id:
        isolation_passed = False
        isolation_explanation = f"Incident merchant {attempt.incident.merchant_id} does not match attempt merchant {merchant_id}."
    elif attempt.campaign and attempt.campaign.merchant_id != merchant_id:
        isolation_passed = False
        isolation_explanation = f"Campaign merchant {attempt.campaign.merchant_id} does not match attempt merchant {merchant_id}."

    checks.append(CheckResult(
        check_name="merchant_isolation",
        passed=isolation_passed,
        reason_code="MERCHANT_MATCHED" if isolation_passed else "MERCHANT_ISOLATION_VIOLATION",
        explanation=isolation_explanation,
    ))

    # 3. Lifecycle Validity
    lifecycle_passed = attempt.status == "approved"
    lifecycle_code = "STATUS_APPROVED"
    lifecycle_explanation = "Attempt is in approved status ready for execution."

    if attempt.status == "recovered":
        lifecycle_passed = False
        lifecycle_code = "ALREADY_RECOVERED"
        lifecycle_explanation = f"Only approved attempts can be executed: Attempt {attempt.recovery_id} is already recovered."
    elif attempt.status == "executed":
        lifecycle_passed = False
        lifecycle_code = "ALREADY_EXECUTED"
        lifecycle_explanation = f"Only approved attempts can be executed: Attempt {attempt.recovery_id} is already executed (link: {attempt.payment_link_id})."
    elif attempt.status == "pending":
        lifecycle_passed = False
        lifecycle_code = "REQUIRES_APPROVAL"
        lifecycle_explanation = f"Only approved attempts can be executed: Attempt {attempt.recovery_id} is pending operator approval."
    elif attempt.status in ["failed", "expired"]:
        lifecycle_passed = False
        lifecycle_code = "TERMINAL_STATE"
        lifecycle_explanation = f"Only approved attempts can be executed: Attempt {attempt.recovery_id} is in terminal state '{attempt.status}'."
    elif attempt.status != "approved":
        lifecycle_passed = False
        lifecycle_code = "INVALID_STATUS"
        lifecycle_explanation = f"Only approved attempts can be executed: Attempt {attempt.recovery_id} has invalid status '{attempt.status}'."

    # Campaign status check
    if attempt.campaign and attempt.campaign.status == "completed":
        lifecycle_passed = False
        lifecycle_code = "CAMPAIGN_COMPLETED"
        lifecycle_explanation = f"Parent campaign {attempt.campaign.campaign_id} is completed; cannot execute further attempts."

    checks.append(CheckResult(
        check_name="lifecycle_validity",
        passed=lifecycle_passed,
        reason_code=lifecycle_code,
        explanation=lifecycle_explanation,
        details={"attempt_status": attempt.status, "campaign_status": attempt.campaign.status if attempt.campaign else None},
    ))

    # 4. Financial Sanity & Charge Amount
    original_amount = attempt.payment.amount if payment_exists else 0
    incentive = attempt.incentive_amount or 0
    charge_amount = original_amount - incentive

    charge_valid = payment_positive and incentive >= 0 and charge_amount > 0 and incentive < original_amount
    charge_explanation = f"Charge amount {charge_amount} is positive and sound (amount: {original_amount}, incentive: {incentive})."
    if not payment_positive:
        charge_explanation = f"Payment amount ({original_amount}) is not positive."
    elif incentive < 0:
        charge_explanation = f"Incentive amount ({incentive}) is negative."
    elif charge_amount <= 0:
        charge_explanation = f"Charge amount ({charge_amount}) must be greater than zero."
    elif incentive >= original_amount:
        charge_explanation = f"Incentive ({incentive}) cannot be greater than or equal to payment amount ({original_amount})."

    checks.append(CheckResult(
        check_name="financial_sanity",
        passed=charge_valid,
        reason_code="CHARGE_AMOUNT_VALID" if charge_valid else "FINANCIAL_SANITY_VIOLATION",
        explanation=charge_explanation,
        details={"original_amount": original_amount, "incentive": incentive, "charge_amount": charge_amount},
    ))

    # 5. Policy Check (if policy provided or resolved from DB)
    if policy is None and attempt.merchant_id:
        policy = db.scalar(select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == attempt.merchant_id))

    if policy:
        action_allowed = attempt.selected_action in policy.allowed_actions
        checks.append(CheckResult(
            check_name="action_authorization",
            passed=action_allowed,
            reason_code="ACTION_ALLOWED" if action_allowed else "ACTION_DISALLOWED",
            explanation=f"Action '{attempt.selected_action}' is authorized." if action_allowed else f"Action '{attempt.selected_action}' not allowed by policy.",
            details={"allowed_actions": policy.allowed_actions},
        ))
        if attempt.selected_action == "incentive":
            incentive_ok = incentive <= policy.max_incentive
            checks.append(CheckResult(
                check_name="incentive_cap",
                passed=incentive_ok,
                reason_code="INCENTIVE_WITHIN_CAP" if incentive_ok else "INCENTIVE_EXCEEDS_CAP",
                explanation=f"Incentive {incentive} is within max_incentive {policy.max_incentive}." if incentive_ok else f"Incentive {incentive} exceeds max_incentive {policy.max_incentive}.",
            ))

    # Evaluate Decision
    failed_checks = [c for c in checks if not c.passed]
    if failed_checks:
        first_fail = failed_checks[0]
        decision = "requires_approval" if first_fail.reason_code == "REQUIRES_APPROVAL" else "blocked"
        audit_event = "GUARDRAIL_APPROVAL_REQUIRED" if decision == "requires_approval" else "GUARDRAIL_BLOCKED"

        return GuardrailResult(
            decision=decision,
            reason_code=first_fail.reason_code,
            explanation=f"Execution guardrail blocked attempt: {first_fail.explanation}",
            audit_event_type=audit_event,
            checks=checks,
            evaluated_exposure={"charge_amount": charge_amount, "incentive": incentive},
            policy_values={"max_incentive": policy.max_incentive if policy else None},
        )

    return GuardrailResult(
        decision="allowed",
        reason_code="WITHIN_POLICY",
        explanation=f"Attempt {attempt.recovery_id} is verified and approved for gateway execution.",
        audit_event_type="GUARDRAIL_APPROVED",
        checks=checks,
        evaluated_exposure={"charge_amount": charge_amount, "incentive": incentive},
        policy_values={"max_incentive": policy.max_incentive if policy else None},
    )
