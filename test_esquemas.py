"""Esquemas opcionais: sem transferência nem conta conjunta há uma só conta; ligados, tudo como antes.
python3 test_esquemas.py"""
import json, sqlite3, tempfile
from pathlib import Path
import server as r

with tempfile.TemporaryDirectory() as tmp:
    r.FATIA_DB_PATH = Path(tmp) / "sem-fatia.db"
    p = Path(tmp) / "novo.db"
    r.init_db(p)                                   # servidor novo: tudo desligado
    db = sqlite3.connect(p); db.row_factory = sqlite3.Row
    assert r.scheme(db) == (False, False)
    db.execute("DELETE FROM month_expenses"); db.execute("DELETE FROM months")
    mid = db.execute("""INSERT INTO months(year, month, ordenado, pct_revolut, pct_poupanca)
                        VALUES (2026, 11, 2000, 0.5, 0.5)""").lastrowid
    db.executemany("INSERT INTO month_expenses(month_id, category, name, planned, account, joint_account) VALUES (?,?,?,?,?,?)",
                   [(mid, "CASA", "Renda", 600, "S", 1), (mid, "CARRO", "Gasolina", 100, "R", 0), (mid, "CASA", "Net", 40, "S", 0)])
    s = r.recompute_month(db, mid, persist=True, force=True)
    assert s["despesas_santander"] == 740 and s["despesas_revolut"] == 0, s     # tudo na mesma conta, a renda é despesa normal
    assert s["trf_revolut"] == 1260 and s["cc_livre"] == 0                      # sobra toda para o fundo da poupança
    assert round(s["poupanca_total"], 2) == 630 and round(s["livre_resto"], 2) == 630
    assert s["joint_expenses_total"] == 0

    db.execute("UPDATE settings SET value='true' WHERE key IN ('use_transfer','use_joint')")
    db.commit()
    s2 = r.recompute_month(db, mid, persist=True, force=True)
    assert s2["despesas_revolut"] == 100 and s2["joint_expenses_total"] == 600 and s2["trf_revolut"] != 1260, s2

    # uma base que já existia fica com os dois ligados (a sessão de quem já usava não muda)
    q = Path(tmp) / "antiga.db"
    c = sqlite3.connect(q); c.executescript(r.SCHEMA); c.commit(); c.close()
    r.init_db(q)
    c = sqlite3.connect(q); c.row_factory = sqlite3.Row
    assert r.scheme(c) == (True, True)
print("ok")
