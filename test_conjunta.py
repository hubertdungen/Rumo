"""Verificação mínima da conta da casa e do mês sem poupança: python3 test_conjunta.py"""
import sqlite3, tempfile
from pathlib import Path
import server as r

with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "rumo.db"
    r.init_db(p)
    db = sqlite3.connect(p); db.row_factory = sqlite3.Row; db.execute("PRAGMA foreign_keys=ON")
    db.execute("UPDATE settings SET value='true' WHERE key IN ('use_transfer','use_joint')"); db.commit()   # estes testes são do esquema ligado
    db.execute("DELETE FROM month_expenses"); db.execute("DELETE FROM months")
    mid = db.execute("""INSERT INTO months(year, month, ordenado, pct_revolut, pct_poupanca)
                        VALUES (2026, 11, 2000, 0.5, 1.0)""").lastrowid
    db.execute("INSERT INTO month_expenses(month_id, category, name, planned, joint_account) VALUES (?, 'CASA', 'Renda', 1000, 1)", (mid,))
    db.execute("INSERT INTO month_expenses(month_id, category, name, planned, account) VALUES (?, 'X', 'Ginásio', 100, 'S')", (mid,))
    s = r.recompute_month(db, mid, persist=True, force=True)
    assert s["joint_transfer_total"] == 500 and s["joint_partner_contribution"] == 500
    assert s["despesas_santander"] == 600                                   # 100 meus + 500 da minha parte da casa
    assert s["joint_account_acumulado"] == 0                                # entrou 1000, saiu 1000
    db.execute("UPDATE months SET joint_partner_actual=400 WHERE id=?", (mid,))
    assert r.month_summary(db, mid)["joint_account_acumulado"] == -100      # ela pôs menos: a conta fica a descoberto
    # Valor adicional: cada um põe 150 a mais; a conta fica com +300.
    db.execute("UPDATE months SET joint_partner_actual=NULL, joint_extra=150 WHERE id=?", (mid,))
    s = r.recompute_month(db, mid, persist=True, force=True)
    assert s["joint_transfer_total"] == 650 and s["joint_partner_contribution"] == 650
    assert s["despesas_santander"] == 750 and s["joint_account_acumulado"] == 300
    db.execute("UPDATE months SET joint_extra=0 WHERE id=?", (mid,))
    db.execute("UPDATE months SET no_savings=1 WHERE id=?", (mid,))
    s = r.recompute_month(db, mid, persist=True, force=True)
    assert s["poupanca_total"] == 0 and s["livre_resto"] == 0 and s["cc_livre"] == 0
    assert round(s["sem_registo"], 2) == 1400                               # 2000 - 600, tudo dado por gasto
print("ok")
