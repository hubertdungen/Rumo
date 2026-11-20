"""Juros dos cofres (TANB / valor) e registo do valor real contra o esperado.
python3 test_juros.py"""
import sqlite3, tempfile
from pathlib import Path
import server as r

p = {"rate_mode": "tanb", "rate_value": 3.0, "rate_tax": 28}
assert round(r.pot_interest(p, 1500), 2) == 2.70                      # 1500 * 3% / 12 * 0,72
assert r.pot_interest({"rate_mode": "eur", "rate_value": 4.5}, 99) == 4.5
assert r.pot_interest({"rate_mode": "none", "rate_value": 9}, 99) == 0
assert r.pot_projection(p, 1500, 50, 2) == [1552.7, 1605.49]
g = r.goal_progress({"goal_amount": 2000, "accumulated": 1500, **p}, {"per_month": 50})
assert g["months_needed"] == 10                                        # sem juros seriam 10 também; com juros nunca mais de 10
g0 = r.goal_progress({"goal_amount": 1600, "accumulated": 1500, "rate_mode": "tanb", "rate_value": 12, "rate_tax": 0}, {"per_month": 0})
assert g0["months_needed"] and g0["months_needed"] < 12                # só os juros também lá chegam

with tempfile.TemporaryDirectory() as tmp:
    r.FATIA_DB_PATH = Path(tmp) / "sem-fatia.db"
    path = Path(tmp) / "t.db"
    r.init_db(path)
    db = sqlite3.connect(path); db.row_factory = sqlite3.Row
    db.execute("DELETE FROM month_expenses"); db.execute("DELETE FROM months")
    mid = db.execute("INSERT INTO months(year, month, ordenado, pct_revolut, pct_poupanca) VALUES (2026, 11, 2000, 0.5, 0.5)").lastrowid
    pid = db.execute("INSERT INTO pots(name, pct, opening_balance) VALUES ('Fundo', 1, 100)").lastrowid
    db.commit()
    exp = r.expected_balance(db, "pots", "pot_snapshots", "pot_id", pid)
    assert exp == 100, exp                                              # só o saldo de abertura: o mês aberto não conta
    r.log_check(db, "pot", pid, exp, 103.5)
    r.log_check(db, "pot", pid, 999, 104.0)                            # corrigir o real não muda o esperado
    row = db.execute("SELECT expected, real FROM balance_checks").fetchall()
    assert len(row) == 1 and (row[0]["expected"], row[0]["real"]) == (100, 104.0), dict(row[0])
    assert r.month_summary(db, mid)["checks"][f"pot:{pid}"] == {"expected": 100, "real": 104.0}
print("ok")
