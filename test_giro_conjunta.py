"""Verificação mínima dos créditos dos dois vindos do Giro: python3 test_giro_conjunta.py"""
import sqlite3, tempfile
from pathlib import Path
import server as r

with tempfile.TemporaryDirectory() as tmp:
    gp = Path(tmp) / "giro.db"
    g = sqlite3.connect(gp)
    g.executescript("""
      CREATE TABLE debts(id INTEGER PRIMARY KEY, created_on TEXT, entity TEXT, description TEXT, total_amount REAL,
        payment_amount REAL, first_payment_on TEXT, rhythm TEXT DEFAULT 'monthly', account TEXT, category TEXT,
        status TEXT DEFAULT 'active', rumo_sync INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0, holder TEXT);
      CREATE TABLE debt_payments(id INTEGER PRIMARY KEY, debt_id INTEGER, paid_on TEXT, amount REAL);
      INSERT INTO debts(created_on, entity, description, total_amount, payment_amount, first_payment_on, account, category, holder)
        VALUES ('2026-10-01', 'IASFA', 'obras', 12000, 200, '2026-11-05', 'J', 'CASA', 'Ambos'),
               ('2026-10-01', 'Santander', 'cartão', 1000, 100, '2026-11-10', 'S', 'DÍVIDAS', 'Eu');
    """)
    g.commit(); g.close()
    r.GIRO_DB_PATH = gp

    p = Path(tmp) / "rumo.db"
    r.init_db(p)
    db = sqlite3.connect(p); db.row_factory = sqlite3.Row; db.execute("PRAGMA foreign_keys=ON")
    db.execute("UPDATE settings SET value='true' WHERE key IN ('use_transfer','use_joint')"); db.commit()   # estes testes são do esquema ligado
    db.execute("DELETE FROM month_expenses"); db.execute("DELETE FROM months")
    mid = db.execute("""INSERT INTO months(year, month, ordenado, pct_revolut, pct_poupanca)
                        VALUES (2026, 11, 2000, 0.5, 1.0)""").lastrowid
    assert r.sync_giro_debts(db, mid)["ok"]
    lines = {e["name"]: e for e in db.execute("SELECT * FROM month_expenses WHERE month_id=?", (mid,))}
    assert lines["IASFA - obras"]["joint_account"] == 1 and lines["IASFA - obras"]["planned"] == 200
    assert lines["Santander - cartão"]["joint_account"] == 0
    s = r.recompute_month(db, mid, persist=True, force=True)
    assert s["joint_transfer_total"] == 100                                 # metade dos 200 da casa é minha
    assert s["despesas_santander"] == 200                                   # 100 do cartão + 100 da minha parte
print("ok")
