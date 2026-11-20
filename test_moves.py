"""As marcas «feito» dos Movimentos ficam no mês, na DB. python3 test_moves.py"""
import sqlite3, tempfile
from pathlib import Path
import server as r

with tempfile.TemporaryDirectory() as tmp:
    r.FATIA_DB_PATH = Path(tmp) / "sem-fatia.db"
    p = Path(tmp) / "rumo.db"
    r.init_db(p)
    db = sqlite3.connect(p); db.row_factory = sqlite3.Row
    mid = db.execute("INSERT INTO months(year, month, ordenado) VALUES (2030, 1, 1000)").lastrowid
    first = r.set_move_done(db, mid, "pot-3", True)["pot-3"]
    assert r.set_move_done(db, mid, "pot-3", True)["pot-3"] == first     # marcar outra vez guarda a hora da 1.ª
    r.set_move_done(db, mid, "in-S", True)
    assert set(r.month_summary(db, mid)["moves_done"]) == {"pot-3", "in-S"}
    assert set(r.set_move_done(db, mid, "pot-3", False)) == {"in-S"}
    for bad in ("", "x'; --", "a" * 41):
        try:
            r.set_move_done(db, mid, bad, True)
            raise SystemExit("aceitou " + bad)
        except ValueError:
            pass
    try:
        r.set_move_done(db, 999, "in-S", True)
        raise SystemExit("aceitou mês inexistente")
    except LookupError:
        pass
print("ok")
