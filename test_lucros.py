"""Verificação mínima: as linhas de LUCROS entram, não saem. python3 test_lucros.py"""
import sqlite3, tempfile
from pathlib import Path
import server as r

with tempfile.TemporaryDirectory() as tmp:
    r.FATIA_DB_PATH = Path(tmp) / "sem-fatia.db"
    p = Path(tmp) / "rumo.db"
    r.init_db(p)
    db = sqlite3.connect(p); db.row_factory = sqlite3.Row; db.execute("PRAGMA foreign_keys=ON")
    db.execute("DELETE FROM month_expenses"); db.execute("DELETE FROM months")
    mid = db.execute("""INSERT INTO months(year, month, ordenado, pct_revolut, pct_poupanca, no_savings)
                        VALUES (2026, 11, 2000, 0.5, 1.0, 1)""").lastrowid
    rows = [("LUCROS", "OLX", 50, "S", 0), ("LUCROS", "Vendas Online", 30, "R", 0),
            ("Lucros", "Bónus casa", 20, "S", 1),                      # rendimento marcado como da casa: não entra na casa
            ("CARRO", "Combustível", 100, "S", 0)]
    db.executemany("INSERT INTO month_expenses(month_id, category, name, planned, account, joint_account) VALUES (?,?,?,?,?,?)",
                   [(mid, *row) for row in rows])
    s = r.recompute_month(db, mid, persist=True, force=True)
    assert s["despesas_santander"] == 100 and s["despesas_revolut"] == 0, s["despesas_santander"]
    assert round(s["sem_registo"], 2) == 2000 + 50 + 20 + 30 - 100            # tudo o que entrou menos o que saiu
    assert s["joint_transfer_total"] == 0                                    # nada da casa
print("ok")
