from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.diagnosis import Diagnosis
from app.models.incident import Incident
from app.models.payment import Payment


def diagnose_incident(db: Session, incident: Incident) -> Diagnosis:
    # 1. Fetch payments in the incident's active time window for the same merchant
    payments = db.scalars(
        select(Payment).where(
            Payment.merchant_id == incident.merchant_id,
            Payment.created_at >= incident.window_start,
            Payment.created_at < incident.window_end,
        )
    ).all()

    # 2. Separate failed and captured payments
    failed_payments = [p for p in payments if p.status == "failed"]
    total_count = len(payments)
    failed_count = len(failed_payments)

    evidence = {
        "total_failures_in_window": failed_count,
        "total_transactions_in_window": total_count,
    }

    diagnoses_scores = []

    # --- Rule 1: Bank-Specific Degradation ---
    if incident.bank and incident.bank != "UNKNOWN":
        bank_payments = [p for p in payments if p.bank == incident.bank]
        bank_failed = [p for p in bank_payments if p.status == "failed"]
        bank_total_cnt = len(bank_payments)
        bank_failed_cnt = len(bank_failed)
        bank_failure_rate = bank_failed_cnt / bank_total_cnt if bank_total_cnt > 0 else 0.0

        # Compare with other banks on the same method
        other_banks_payments = [
            p for p in payments if p.bank != incident.bank and p.method == incident.method
        ]
        other_banks_failed = [p for p in other_banks_payments if p.status == "failed"]
        other_banks_total_cnt = len(other_banks_payments)
        other_banks_failed_cnt = len(other_banks_failed)
        other_banks_failure_rate = (
            other_banks_failed_cnt / other_banks_total_cnt if other_banks_total_cnt > 0 else 0.0
        )

        delta = bank_failure_rate - other_banks_failure_rate
        if delta >= 0.1 and bank_failed_cnt >= 5:
            conf = min(1.0, delta * 2.0)
            diagnoses_scores.append(
                (
                    "bank-specific degradation",
                    conf,
                    f"Degradation is localized to bank '{incident.bank}' for method '{incident.method}' with a failure rate of {bank_failure_rate:.1%} compared to {other_banks_failure_rate:.1%} for other banks.",
                    {
                        "bank": incident.bank,
                        "method": incident.method,
                        "bank_failure_rate": round(bank_failure_rate, 4),
                        "bank_total_count": bank_total_cnt,
                        "bank_failed_count": bank_failed_cnt,
                        "other_banks_failure_rate": round(other_banks_failure_rate, 4),
                        "other_banks_total_count": other_banks_total_cnt,
                        "other_banks_failed_count": other_banks_failed_cnt,
                    },
                )
            )

    # --- Rule 2: Payment-Method Degradation ---
    if incident.method and incident.method != "UNKNOWN":
        method_payments = [p for p in payments if p.method == incident.method]
        method_failed = [p for p in method_payments if p.status == "failed"]
        method_total_cnt = len(method_payments)
        method_failed_cnt = len(method_failed)
        method_failure_rate = method_failed_cnt / method_total_cnt if method_total_cnt > 0 else 0.0

        # Compare with other payment methods
        other_methods_payments = [p for p in payments if p.method != incident.method]
        other_methods_failed = [p for p in other_methods_payments if p.status == "failed"]
        other_methods_total_cnt = len(other_methods_payments)
        other_methods_failed_cnt = len(other_methods_failed)
        other_methods_failure_rate = (
            other_methods_failed_cnt / other_methods_total_cnt if other_methods_total_cnt > 0 else 0.0
        )

        delta = method_failure_rate - other_methods_failure_rate
        if delta >= 0.1 and method_failed_cnt >= 5:
            conf = min(1.0, delta * 1.5)
            diagnoses_scores.append(
                (
                    "payment-method degradation",
                    conf,
                    f"Broad degradation detected across method '{incident.method}' with a failure rate of {method_failure_rate:.1%} compared to {other_methods_failure_rate:.1%} for other payment methods.",
                    {
                        "method": incident.method,
                        "method_failure_rate": round(method_failure_rate, 4),
                        "method_total_count": method_total_cnt,
                        "method_failed_count": method_failed_cnt,
                        "other_methods_failure_rate": round(other_methods_failure_rate, 4),
                        "other_methods_total_count": other_methods_total_cnt,
                        "other_methods_failed_count": other_methods_failed_cnt,
                    },
                )
            )

    # --- Rule 3: Error-Code Spike ---
    if incident.error_code and incident.error_code != "NONE":
        error_failed = [p for p in failed_payments if p.error_code == incident.error_code]
        error_failed_cnt = len(error_failed)
        error_concentration = error_failed_cnt / failed_count if failed_count > 0 else 0.0

        if error_concentration >= 0.5 and error_failed_cnt >= 5:
            diagnoses_scores.append(
                (
                    "error-code spike",
                    error_concentration,
                    f"High concentration of failures ({error_concentration:.1%}) sharing error code '{incident.error_code}'.",
                    {
                        "error_code": incident.error_code,
                        "error_failed_count": error_failed_cnt,
                        "error_concentration": round(error_concentration, 4),
                    },
                )
            )

    # --- Rule 4: Authorization-Step Degradation ---
    if incident.error_step and incident.error_step != "NONE":
        step_failed = [p for p in failed_payments if p.error_step == incident.error_step]
        step_failed_cnt = len(step_failed)
        step_concentration = step_failed_cnt / failed_count if failed_count > 0 else 0.0

        if step_concentration >= 0.5 and step_failed_cnt >= 5:
            diagnoses_scores.append(
                (
                    "authorization-step degradation",
                    step_concentration,
                    f"High concentration of failures ({step_concentration:.1%}) occurring during step '{incident.error_step}'.",
                    {
                        "error_step": incident.error_step,
                        "step_failed_count": step_failed_cnt,
                        "step_concentration": round(step_concentration, 4),
                    },
                )
            )

    # 3. Determine the highest confidence diagnosis
    best_type = "insufficient evidence / unknown"
    best_conf = 0.0
    best_explanation = "Insufficient transaction or failure data to confidently isolate a specific cause."
    best_evidence = evidence

    if diagnoses_scores:
        diagnoses_scores.sort(key=lambda x: x[1], reverse=True)
        top_type, top_conf, top_explanation, top_evidence = diagnoses_scores[0]
        if top_conf >= 0.5:
            best_type = top_type
            best_conf = top_conf
            best_explanation = top_explanation
            best_evidence = {**evidence, **top_evidence}

    # 4. Check if a diagnosis already exists for this incident (one-to-one)
    existing_diagnosis = db.scalar(
        select(Diagnosis).where(Diagnosis.incident_id == incident.id)
    )

    if existing_diagnosis:
        diagnosis = existing_diagnosis
        diagnosis.diagnosis_type = best_type
        diagnosis.explanation = best_explanation
        diagnosis.supporting_evidence = best_evidence
        diagnosis.confidence = best_conf
    else:
        diagnosis = Diagnosis(
            incident_id=incident.id,
            diagnosis_type=best_type,
            explanation=best_explanation,
            supporting_evidence=best_evidence,
            confidence=best_conf,
        )
        db.add(diagnosis)

    db.commit()
    db.refresh(diagnosis)
    return diagnosis
