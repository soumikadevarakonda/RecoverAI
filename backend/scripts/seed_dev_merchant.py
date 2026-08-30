from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.merchant import Merchant


db = SessionLocal()

try:
    merchant = db.scalar(
        select(Merchant).where(
            Merchant.name == "RecoverAI Demo Merchant"
        )
    )

    if merchant is None:
        merchant = Merchant(
            name="RecoverAI Demo Merchant",
        )
        db.add(merchant)
        db.commit()
        db.refresh(merchant)

    print(f"Merchant ID: {merchant.id}")

finally:
    db.close()