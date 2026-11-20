"""Verificação mínima: o que as % dos cofres deixam por distribuir vai para o Geral. python3 test_sobra.py"""
import sqlite3, tempfile
from pathlib import Path
import server as r

with tempfile.TemporaryDirectory() as tmp:
    r.FATIA_DB_PATH = Path(tmp) / "sem-fatia.db"
    p = Path(tmp) / "rumo.db"
    r.init_db(p)
    db = sqlite3.connect(p); db.row_factory = sqlite3.Row; db.execute("PRAGMA foreign_keys=ON")
    db.execute("DELETE FROM month_expenses"); db.execute("DELETE FROM months")
    db.execute("INSERT INTO settings(key, value) VALUES ('round_pots_enabled', 'false') "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    db.execute("UPDATE pots SET active=0"); db.execute("UPDATE investments SET active=0")
    db.execute("INSERT INTO pots(name, pct, active) VALUES ('A', 0.6, 1), ('B', 0.3, 1)")   # 90%
    db.execute("INSERT INTO investments(name, pct, active) VALUES ('C', 1.0, 1)")           # 100%
    mid = db.execute("""INSERT INTO months(year, month, ordenado, pct_revolut, pct_poupanca,
                        split_geral, split_cofres, split_investimentos)
                        VALUES (2026, 11, 1000, 1.0, 1.0, 0.2, 0.5, 0.3)""").lastrowid
    r.recompute_month(db, mid, persist=True, force=True)
    geral = db.execute("SELECT contributed FROM geral_snapshots WHERE month_id=?", (mid,)).fetchone()[0]
    # poupança 1000: Geral 200 + 10% dos 500 dos cofres = 250
    assert round(geral, 2) == 250, geral
print("ok")
