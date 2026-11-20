"""Verificação mínima da ponte Fatia → Rumo: python3 test_fatia.py"""
import datetime, sqlite3, tempfile
from pathlib import Path
import server as r


def ts(y, m, d, h=12):
    return int(datetime.datetime(y, m, d, h).timestamp())


with tempfile.TemporaryDirectory() as tmp:
    r.FATIA_DB_PATH = Path(tmp) / "nao-existe.db"
    assert r.fatia_jobs_live(2026, 10) == []                                # sem Fatia: nada, sem erro

    fp = Path(tmp) / "fatia.db"
    f = sqlite3.connect(fp)
    f.execute("""CREATE TABLE registos(id INTEGER PRIMARY KEY, cliente_nome TEXT, projeto TEXT,
                 estado TEXT, total REAL, criado INTEGER)""")
    f.executemany("INSERT INTO registos(cliente_nome, projeto, estado, total, criado) VALUES (?,?,?,?,?)", [
        ("Ana", "Estore", "impresso", 12, ts(2026, 10, 13)),
        ("Ana", "Duche", "impresso", 14, ts(2026, 10, 31, 23)),
        ("Rui", "Vaso", "mandado", 50, ts(2026, 10, 14)),                   # ainda não impresso: não conta
        ("Rui", "Caneca", "impresso", 9, ts(2026, 11, 1, 0)),               # mês seguinte
    ])
    f.commit()
    r.FATIA_DB_PATH = fp

    p = Path(tmp) / "rumo.db"
    r.init_db(p)
    db = sqlite3.connect(p); db.row_factory = sqlite3.Row; db.execute("PRAGMA foreign_keys=ON")
    db.execute("DELETE FROM month_expenses"); db.execute("DELETE FROM months")
    mid = db.execute("""INSERT INTO months(year, month, ordenado, extras, pct_revolut, pct_poupanca)
                        VALUES (2026, 10, 1000, 10, 0.5, 1.0)""").lastrowid
    db.execute("INSERT INTO month_expenses(month_id, category, name, planned, account) VALUES (?, 'X', 'Renda', 400, 'S')", (mid,))
    s = r.recompute_month(db, mid, persist=True)
    assert s["fatia_income"] == 26 and len(s["fatia_jobs"]) == 2            # só os impressos de outubro
    assert round(s["cc_livre"] + s["trf_revolut"], 2) == 636                # 1000 + 10 + 26 - 400

    r.close_month(db, mid)
    f.execute("INSERT INTO registos(cliente_nome, projeto, estado, total, criado) VALUES ('Zé','Peça','impresso',100,?)",
              (ts(2026, 10, 20),))
    f.commit()
    r.refresh_fatia(db, mid)
    s = r.recompute_month(db, mid, persist=True, force=True)
    assert s["fatia_income"] == 26 and s["closed"]                          # fechado: fica o que havia ao fechar

    db.execute("UPDATE months SET closed=0 WHERE id=?", (mid,)); db.commit()
    r.refresh_fatia(db, mid)                                                # reaberto: volta a ler o Fatia
    s = r.month_summary(db, mid)
    assert s["fatia_income"] == 126 and round(s["cc_livre"] + s["trf_revolut"], 2) == 736
print("ok")
