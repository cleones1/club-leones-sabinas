import json

from .database import SessionLocal
from .models import (
    ClubSetting,
    DebtPaymentAllocation,
    FamilyMember,
    ManualDebt,
    Member,
    MemberBillingProfile,
    MemberPhoto,
    Membership,
    MonthlyCharge,
    Payment,
    PoolPass,
    User,
)

CLEANUP_KEY = "test_member_cleanup_2026_09_17_v1"


def run_test_cleanup_once():
    """Elimina una sola vez los socios/usuarios y movimientos financieros de prueba.

    Conserva administradores, configuraciones/precios y rentas al público.
    """
    db = SessionLocal()
    try:
        if db.query(ClubSetting).filter(ClubSetting.key == CLEANUP_KEY).first():
            return

        before = {
            "socios": db.query(Member).count(),
            "usuarios_socios": db.query(User).filter(User.role == "member").count(),
            "pagos": db.query(Payment).count(),
            "mensualidades": db.query(MonthlyCharge).count(),
            "adeudos_manuales": db.query(ManualDebt).count(),
            "asignaciones": db.query(DebtPaymentAllocation).count(),
            "pases_alberca": db.query(PoolPass).count(),
            "familiares": db.query(FamilyMember).count(),
            "membresias": db.query(Membership).count(),
            "perfiles_cobro": db.query(MemberBillingProfile).count(),
            "fotos": db.query(MemberPhoto).count(),
        }

        # El orden evita referencias entre pagos, pases y asignaciones.
        db.query(DebtPaymentAllocation).delete(synchronize_session=False)
        db.query(PoolPass).delete(synchronize_session=False)
        db.query(Payment).delete(synchronize_session=False)
        db.query(MonthlyCharge).delete(synchronize_session=False)
        db.query(ManualDebt).delete(synchronize_session=False)
        db.query(FamilyMember).delete(synchronize_session=False)
        db.query(MemberPhoto).delete(synchronize_session=False)
        db.query(Membership).delete(synchronize_session=False)
        db.query(MemberBillingProfile).delete(synchronize_session=False)
        db.query(User).filter(User.role == "member").delete(synchronize_session=False)
        db.query(Member).delete(synchronize_session=False)

        db.add(ClubSetting(
            key=CLEANUP_KEY,
            value=json.dumps(before, ensure_ascii=False, separators=(",", ":"))[:255],
        ))
        db.commit()
        print("TEST_DATA_CLEANUP_OK", json.dumps(before, ensure_ascii=False))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
