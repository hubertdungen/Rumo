#!/usr/bin/env python3
"""Rumo: gestão financeira pessoal — PWA estática + API SQLite."""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import datetime
import socket
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SESSION_COOKIE = "rumo_session"
SESSION_DAYS = int(os.environ.get("RUMO_SESSION_DAYS", "30"))
MAX_BODY = int(os.environ.get("RUMO_MAX_BODY", "2097152"))
STATIC_EXTS = {".html", ".js", ".svg", ".webmanifest", ".png", ".ico", ".css", ".woff2"}
PBKDF2_ROUNDS = int(os.environ.get("RUMO_PBKDF2_ROUNDS", "600000"))
ALLOW_SIGNUP = os.environ.get("RUMO_ALLOW_SIGNUP", "").lower() in {"1", "true", "yes", "on"}
ALLOW_PUBLIC_BOOTSTRAP = os.environ.get("RUMO_ALLOW_PUBLIC_BOOTSTRAP", "").lower() in {
    "1", "true", "yes", "on",
}
COOKIE_SECURE = os.environ.get("RUMO_COOKIE_SECURE", "").lower() in {"1", "true", "yes", "on"}
GIRO_DB_PATH = Path(
    os.environ.get(
        "GIRO_DB_PATH",
        str(Path(os.environ.get("GIRO_DATA_DIR", str(Path.home() / ".local/share/giro"))) / "giro.db"),
    )
).expanduser()
# O Fatia (orçamentos 3D) guarda os trabalhos impressos na base dele; o Rumo só a lê.
FATIA_DB_PATH = Path(
    os.environ.get("FATIA_DB_PATH", str(Path.home() / ".local/share/fatia/fatia.db"))
).expanduser()

# Expired sessions used to be swept on every authenticated request, which turned each
# API call into a write (and an fsync, and WAL growth) just to delete nothing. The
# session lookup already filters on expires_at, so the sweep is pure housekeeping --
# once an hour is plenty.
SESSION_SWEEP_EVERY = 3600
_session_sweep_lock = threading.Lock()
_session_sweep_at = 0.0

# The app is one 130 KB HTML file and a handful of JSON payloads -- all of it text, all
# of it compressing to roughly a quarter of its size. Nothing was being compressed: the
# browser asked for gzip on every request and got the raw bytes back. Below the
# threshold the header overhead is not worth the CPU.
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 6
COMPRESSIBLE_EXT = {".html", ".css", ".js", ".json", ".svg", ".webmanifest", ".txt", ".map"}

MONTH_NAMES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
GIRO_RHYTHMS = {
    "once": "único",
    "weekly": "semanal",
    "biweekly": "quinzenal",
    "monthly": "mensal",
    "quarterly": "trimestral",
    "semiannual": "semestral",
    "annual": "anual",
    "custom": "personalizado",
}
# Os mesmos degraus do Giro. Uma dívida de 2 em 2 meses (ou de 5 em 5 semanas)
# tem de cair nos mesmos meses dos dois lados, senão o Rumo lançava-a no mês errado.
GIRO_STEPS = {
    "once": ("once", 0),
    "weekly": ("week", 1),
    "biweekly": ("week", 2),
    "monthly": ("month", 1),
    "quarterly": ("month", 3),
    "semiannual": ("month", 6),
    "annual": ("month", 12),
}
GIRO_UNIT_PLURALS = {"day": "dias", "week": "semanas", "month": "meses"}


def giro_field(row: Any, key: str, default: Any = None) -> Any:
    """Uma coluna do Giro que pode ainda não existir: a ponte lê a base dele
    tal como ela está, e um Giro por actualizar não pode partir o mês."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def giro_step(debt: sqlite3.Row) -> tuple[str, int]:
    unit = str(giro_field(debt, "interval_unit", "") or "").strip().lower()
    count = int(giro_field(debt, "interval_count", 0) or 0)
    if unit in {"day", "week", "month", "year"} and count > 0:
        return ("month", count * 12) if unit == "year" else (unit, count)
    return GIRO_STEPS.get(str(giro_field(debt, "rhythm", "monthly") or "monthly"), ("month", 1))


def giro_step_label(debt: sqlite3.Row) -> str:
    named = str(giro_field(debt, "rhythm", "monthly") or "monthly")
    if not str(giro_field(debt, "interval_unit", "") or "").strip() and named in GIRO_RHYTHMS and named != "custom":
        return GIRO_RHYTHMS[named]
    unit, count = giro_step(debt)
    if unit == "once":
        return "único"
    if count == 1:
        return {"day": "diário", "week": "semanal", "month": "mensal"}.get(unit, "mensal")
    if unit == "month" and count % 12 == 0:
        anos = count // 12
        return "anual" if anos == 1 else f"de {anos} em {anos} anos"
    return f"de {count} em {count} {GIRO_UNIT_PLURALS.get(unit, 'meses')}"


def giro_installment(debt: sqlite3.Row) -> float:
    """A prestação, venha ela em euros ou em percentagem do total da dívida."""
    total = float(giro_field(debt, "total_amount", 0) or 0)
    if str(giro_field(debt, "payment_mode", "amount") or "amount") == "percent":
        percent = float(giro_field(debt, "payment_percent", 0) or 0)
        return round(total * percent / 100.0, 2) if percent > 0 else 0.0
    return float(giro_field(debt, "payment_amount", 0) or 0)


def now() -> int:
    return int(time.time())


def parse_iso_date(value: Any) -> datetime.date | None:
    try:
        if not value:
            return None
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def last_day_of_month(year: int, month: int) -> int:
    nxt = datetime.date(year + (month // 12), (month % 12) + 1, 1)
    return (nxt - datetime.timedelta(days=1)).day


def add_calendar_months(day: datetime.date, months: int) -> datetime.date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return datetime.date(year, month, min(day.day, last_day_of_month(year, month)))


def giro_due_dates_for_month(debt: sqlite3.Row, year: int, month: int) -> list[datetime.date]:
    first = parse_iso_date(giro_field(debt, "first_payment_on")) or parse_iso_date(giro_field(debt, "created_on")) or datetime.date(year, month, 1)
    unit, count = giro_step(debt)
    start = datetime.date(year, month, 1)
    end = datetime.date(year, month, last_day_of_month(year, month))

    if unit == "once":
        return [first] if start <= first <= end else []

    if unit in {"day", "week"}:
        step = count * (7 if unit == "week" else 1)
        due = first
        if due < start:
            skips = max(0, (start - due).days // step)
            due = due + datetime.timedelta(days=skips * step)
            while due < start:
                due += datetime.timedelta(days=step)
        out: list[datetime.date] = []
        while due <= end:
            out.append(due)
            due += datetime.timedelta(days=step)
        return out

    diff = (year - first.year) * 12 + (month - first.month)
    if diff < 0 or diff % count:
        return []
    due = add_calendar_months(first, diff)
    return [due] if due.year == year and due.month == month else []


GIRO_PERSONAL_HOLDERS = {"", "eu", "mim", "pessoal", "me", "proprio", "próprio"}


def giro_holder_mine(debt: sqlite3.Row) -> bool:
    return str(giro_field(debt, "holder", "") or "").strip().lower() in GIRO_PERSONAL_HOLDERS


def giro_creditor_planned(creditor: sqlite3.Row, row: sqlite3.Row | None, ym: str) -> float:
    """A mesma regra que `creditor_planned` do Giro: uma excepção do mês manda;
    sem ela vale a mensalidade, a partir do mês em que começou."""
    if row is not None:
        return 0.0 if row["status"] == "skipped" else float(row["amount"] or 0)
    if not int(creditor["active"]) or ym < str(creditor["start_month"] or ""):
        return 0.0
    return float(creditor["monthly_amount"] or 0)


def giro_creditor_lines(giro: sqlite3.Connection, creditors: list[sqlite3.Row], debts: list[sqlite3.Row],
                        month: sqlite3.Row) -> list[tuple]:
    ym = f"{int(month['year']):04d}-{int(month['month']):02d}"
    out = []
    for index, c in enumerate(creditors):
        if not c["rumo_sync"]:
            continue
        key = str(c["name"]).strip().lower()
        own = [d for d in debts if str(d["entity"] or "").strip().lower() == key and giro_holder_mine(d)
               and d["status"] == "active"]
        balance = 0.0
        for d in own:
            paid = giro.execute("SELECT COALESCE(SUM(amount), 0) FROM debt_payments WHERE debt_id=?", (d["id"],)).fetchone()[0]
            balance += max(0.0, float(d["total_amount"] or 0) - float(paid or 0))
        row = giro.execute("SELECT * FROM creditor_months WHERE creditor_id=? AND ym=?", (c["id"], ym)).fetchone()
        allocated = float(giro.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM debt_payments WHERE creditor_month_id=?", (row["id"],)
        ).fetchone()[0] or 0) if row else 0.0
        # O que esta mensalidade já pagou conta como saldo que havia à entrada do mês.
        due = round(min(giro_creditor_planned(c, row, ym), balance + allocated), 2)
        paid = round(allocated, 2) if row and row["status"] == "paid" else 0.0
        if due <= 0 and paid <= 0:
            continue
        day = min(max(1, int(c["pay_day"] or 1)), last_day_of_month(int(month["year"]), int(month["month"])))
        due_on = datetime.date(int(month["year"]), int(month["month"]), day)
        bits = [f"Giro · mensalidade · dia {day:02d}", f"{len(own)} dívida(s) · saldo {balance:.2f} €"]
        if row and row["status"] == "paid":
            bits.append("paga")
        elif row:
            bits.append("excepção deste mês")
        out.append((
            month["id"], str(c["category"] or "DÍVIDAS").strip().upper(), f"{c['name']} — mensalidade",
            " · ".join(bits), due, paid or None, "R" if c["account"] == "R" else "S", 0,
            1 if paid >= due > 0 else 0, 8900 + index, f"credor:{c['id']}", due_on.isoformat(),
        ))
    return out


def giro_payment_totals(giro: sqlite3.Connection, debt_id: int, year: int, month: int) -> tuple[float, float]:
    month_start = datetime.date(year, month, 1).isoformat()
    month_end = datetime.date(year, month, last_day_of_month(year, month)).isoformat()
    before = giro.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM debt_payments WHERE debt_id=? AND paid_on < ?",
        (debt_id, month_start),
    ).fetchone()["total"]
    in_month = giro.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total
           FROM debt_payments WHERE debt_id=? AND paid_on >= ? AND paid_on <= ?""",
        (debt_id, month_start, month_end),
    ).fetchone()["total"]
    return float(before or 0), float(in_month or 0)


def giro_debt_due_amount(debt: sqlite3.Row, year: int, month: int, paid_before_month: float) -> tuple[float, list[datetime.date], float]:
    dates = giro_due_dates_for_month(debt, year, month)
    balance_before_month = max(0.0, float(giro_field(debt, "total_amount", 0) or 0) - paid_before_month)
    if not dates or balance_before_month <= 0:
        return 0.0, dates, balance_before_month
    installment = giro_installment(debt)
    if installment <= 0:
        installment = balance_before_month
    return round(min(balance_before_month, installment * len(dates)), 2), dates, balance_before_month


def fatia_jobs_live(year: int, month: int) -> list[dict]:
    """Os trabalhos que o Fatia deu por impressos neste mês (hora local). O total
    é o que o cliente paga e entra na Santander. Sem a base do Fatia, não há nada."""
    start = datetime.datetime(year, month, 1).timestamp()
    end = datetime.datetime(year + month // 12, month % 12 + 1, 1).timestamp()
    try:
        fatia = sqlite3.connect(f"file:{FATIA_DB_PATH}?mode=ro", uri=True, timeout=2)
        try:
            rows = fatia.execute(
                """SELECT id, cliente_nome, projeto, total, criado FROM registos
                   WHERE estado='impresso' AND criado >= ? AND criado < ? ORDER BY criado, id""",
                (start, end),
            ).fetchall()
        finally:
            fatia.close()
    except sqlite3.Error:
        return []
    return [{"id": r[0], "cliente": r[1] or "", "projeto": r[2] or "", "total": round(float(r[3] or 0), 2),
             "criado": r[4]} for r in rows]


def month_fatia_jobs(month: Any) -> list[dict]:
    """Os trabalhos do Fatia gravados no mês (é o que as contas usaram)."""
    try:
        return json.loads(month["fatia_jobs"] or "[]")
    except (ValueError, TypeError, IndexError, KeyError):
        return []


def month_income(month: Any) -> float:
    """O que me entra no mês: ordenado + extras + o que o Fatia faturou."""
    fatia = sum(float(j.get("total") or 0) for j in month_fatia_jobs(month))
    return float(month["ordenado"] or 0) + float(month["extras"] or 0) + fatia


def refresh_fatia(db: sqlite3.Connection, month_id: int) -> None:
    """Num mês aberto, um trabalho novo (ou apagado) no Fatia refaz as contas.
    Um mês fechado fica com os trabalhos que tinha ao fechar."""
    m = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
    if m is None or m["closed"] or m["historical_import"]:
        return
    if fatia_jobs_live(m["year"], m["month"]) != month_fatia_jobs(m):
        recompute_month(db, month_id, persist=True)
        carry_forward(db, month_id, m["computed_cc_livre"], m["computed_livre_resto"])


def giro_status(db: sqlite3.Connection, month_id: int | None = None) -> dict:
    if month_id is None:
        month = db.execute(
            "SELECT id, year, month, closed FROM months WHERE closed=0 ORDER BY year DESC, month DESC LIMIT 1"
        ).fetchone()
    else:
        month = db.execute("SELECT id, year, month, closed FROM months WHERE id=?", (month_id,)).fetchone()
    if not month:
        return {"ok": True, "state": "no_open_month", "month": None, "synced": 0, "amount": 0}
    row = db.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(planned), 0) AS total
           FROM month_expenses WHERE month_id=? AND external_source='giro'""",
        (month["id"],),
    ).fetchone()
    return {
        "ok": True,
        "state": "ready",
        "month": dict(month),
        "synced": row["n"],
        "amount": round(float(row["total"] or 0), 2),
    }


def sync_giro_debts(db: sqlite3.Connection, month_id: int | None = None) -> dict:
    if month_id is None:
        month = db.execute(
            "SELECT * FROM months WHERE closed=0 ORDER BY year DESC, month DESC LIMIT 1"
        ).fetchone()
    else:
        month = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
    if not month:
        return {"ok": True, "state": "no_open_month", "month": None, "synced": 0, "amount": 0}
    if month["closed"]:
        return giro_status(db, month["id"]) | {"state": "closed"}
    if not GIRO_DB_PATH.exists():
        return {"ok": False, "state": "missing", "month": dict(month), "synced": 0, "amount": 0}

    try:
        giro = sqlite3.connect(GIRO_DB_PATH)
        giro.row_factory = sqlite3.Row
        giro.execute("PRAGMA foreign_keys=ON")
        debts = giro.execute(
            """SELECT * FROM debts
               WHERE rumo_sync=1 AND status IN ('active', 'paid')
               ORDER BY sort_order, id"""
        ).fetchall()
        has_creditors = giro.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creditors'"
        ).fetchone()
        creditors = giro.execute(
            "SELECT * FROM creditors WHERE active=1 ORDER BY name"
        ).fetchall() if has_creditors else []
        all_debts = giro.execute("SELECT * FROM debts ORDER BY id").fetchall() if creditors else []
    except sqlite3.Error as exc:
        return {"ok": False, "state": "error", "month": dict(month), "error": str(exc), "synced": 0, "amount": 0}

    try:
        to_insert = []
        # As dívidas de quem tem mensalidade entram pela mensalidade, numa linha só.
        under_plan = {str(c["name"]).strip().lower() for c in creditors}
        for index, debt in enumerate(debts):
            if giro_holder_mine(debt) and str(debt["entity"] or "").strip().lower() in under_plan:
                continue
            paid_before, paid_in_month = giro_payment_totals(giro, int(debt["id"]), int(month["year"]), int(month["month"]))
            if debt["status"] == "paid" and paid_in_month <= 0:
                continue
            due, dates, balance_before_month = giro_debt_due_amount(debt, int(month["year"]), int(month["month"]), paid_before)
            if due <= 0:
                continue
            entity = str(debt["entity"] or "Dívida").strip()
            desc = str(debt["description"] or "").strip()
            name = f"{entity} - {desc}" if desc else entity
            due_label = ", ".join(d.strftime("%d/%m") for d in dates)
            installment = giro_installment(debt)
            # Quantas prestações faltam contando com esta -- é a pergunta que se
            # faz a olhar para a linha no mês, e evita ir ao Giro por causa dela.
            left = math.ceil(round(balance_before_month / installment, 6)) if installment > 0 else 1
            holder = str(giro_field(debt, "holder", "") or "").strip()
            bits = [f"Giro · vence {due_label}", giro_step_label(debt)]
            # Conta «J» no Giro = crédito da casa: entra pela conjunta, pelo valor inteiro.
            joint = 1 if debt["account"] == "J" else 0
            if joint:
                bits.append("crédito da casa")
            elif holder and holder.lower() not in {"eu", "mim", "pessoal", "me", "proprio", "próprio"}:
                bits.append(f"de {holder}")
            bits.append("falta 1 prestação" if left == 1 else f"faltam {left} prestações")
            bits.append(f"saldo {balance_before_month:.2f} €")
            obs = " · ".join(bits)
            actual = min(paid_in_month, due) if paid_in_month > 0 else None
            to_insert.append(
                (
                    month["id"],
                    str(debt["category"] or "DÍVIDAS").strip().upper(),
                    name,
                    obs,
                    due,
                    actual,
                    "R" if debt["account"] == "R" else "S",
                    joint,
                    1 if paid_in_month >= due else 0,
                    9000 + index,
                    str(debt["id"]),
                    ",".join(d.isoformat() for d in dates),
                )
            )
        to_insert += giro_creditor_lines(giro, creditors, all_debts, month)
    except sqlite3.Error as exc:
        return {"ok": False, "state": "error", "month": dict(month), "error": str(exc), "synced": 0, "amount": 0}
    finally:
        giro.close()

    db.execute("SAVEPOINT giro_sync")
    try:
        db.execute("DELETE FROM month_expenses WHERE month_id=? AND external_source='giro'", (month["id"],))
        db.executemany(
            """INSERT INTO month_expenses
               (month_id, template_id, category, name, obs, planned, actual, mutable,
                account, joint_account, closed, sort_order, external_source, external_id, external_due_on)
               VALUES (?, NULL, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'giro', ?, ?)""",
            to_insert,
        )
        db.execute("RELEASE giro_sync")
    except sqlite3.Error as exc:
        db.execute("ROLLBACK TO giro_sync")
        db.execute("RELEASE giro_sync")
        return {"ok": False, "state": "error", "month": dict(month), "error": str(exc), "synced": 0, "amount": 0}
    db.commit()

    return {
        "ok": True,
        "state": "ready",
        "month": {"id": month["id"], "year": month["year"], "month": month["month"], "closed": bool(month["closed"])},
        "synced": len(to_insert),
        "amount": round(sum(row[4] for row in to_insert), 2),
    }


# ------------------------------------------------------------- market data --

PRICE_TTL = int(os.environ.get("RUMO_PRICE_TTL", "900"))
# Failures get their own, much shorter memory. Caching them for PRICE_TTL turned a
# two-second DNS hiccup into a quarter of an hour of "não encontro este símbolo",
# long after the network was fine again.
FAIL_TTL = int(os.environ.get("RUMO_PRICE_FAIL_TTL", "45"))
_price_cache: dict[str, tuple[float, dict]] = {}
_fx_cache: dict[str, tuple[float, float]] = {}
_search_cache: dict[str, tuple[float, list]] = {}
_YAHOO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Symbols people actually type (Revolut's own names, mostly) mapped to the symbol
# Yahoo prices. XAU is the motivating case: Yahoo *resolves* "XAU", but to a mutual
# fund delisted in 2019 that carries no price at all, so the lookup died with a
# baffling "sem preço" instead of "esse não é o símbolo do ouro".
SYMBOL_ALIASES = {
    "XAU": "GC=F", "XAUUSD": "GC=F", "XAUEUR": "GC=F", "GOLD": "GC=F", "OURO": "GC=F",
    "XAG": "SI=F", "XAGUSD": "SI=F", "SILVER": "SI=F", "PRATA": "SI=F",
    "XPT": "PL=F", "PLATINUM": "PL=F", "PLATINA": "PL=F",
    "XPD": "PA=F", "PALLADIUM": "PA=F", "PALADIO": "PA=F",
    "OIL": "CL=F", "PETROLEO": "CL=F", "BRENT": "BZ=F", "NATGAS": "NG=F",
}

# Bare crypto codes -> the EUR pair, so typing "BTC" doesn't land on some equity.
CRYPTO_CODES = [
    "BTC", "ETH", "USDT", "XRP", "BNB", "SOL", "USDC", "DOGE", "ADA", "TRX",
    "LINK", "AVAX", "XLM", "SHIB", "DOT", "LTC", "BCH", "UNI", "PEPE", "NEAR",
    "APT", "ICP", "ETC", "POL", "ALGO", "VET", "FIL", "ATOM", "ARB", "OP",
    "AAVE", "INJ", "SAND", "MANA", "GRT", "EOS", "XTZ", "CHZ", "CRV", "SUI",
]
for _c in CRYPTO_CODES:
    SYMBOL_ALIASES.setdefault(_c, f"{_c}-EUR")


def resolve_symbol(ticker: str) -> str:
    """User-facing ticker -> the symbol Yahoo actually prices."""
    t = (ticker or "").strip().upper()
    return SYMBOL_ALIASES.get(t, t)


def _yahoo_result(symbol: str, rng: str = "5d") -> dict:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol)}?interval=1d&range={rng}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _YAHOO_UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = (data.get("chart") or {}).get("result")
    if not result:
        err = (data.get("chart") or {}).get("error") or {}
        raise ValueError(err.get("description") or "símbolo desconhecido no Yahoo")
    return result[0]


def _yahoo_chart(symbol: str) -> dict:
    return _yahoo_result(symbol)["meta"]


def _closes(result: dict) -> list[tuple[str, float]]:
    """[(YYYY-MM-DD, close)] oldest first, skipping the gaps Yahoo leaves as null."""
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    closes = quote[0].get("close") or []
    out = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()
        out.append((day, float(close)))
    return out


def explain_net_error(exc: Exception) -> str:
    """Turn a stdlib network exception into something worth showing a person.

    The raw text ("URLError: <urlopen error [Errno -3] Temporary failure in name
    resolution>") says nothing to someone who just wants to type in how much silver
    they own."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "símbolo não encontrado no Yahoo"
        if exc.code == 429:
            return "o Yahoo está a limitar os pedidos; tenta daqui a um bocado"
        return f"o Yahoo respondeu {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        text = str(reason).lower()
        if isinstance(reason, socket.gaierror) or "name resolution" in text or "name or service" in text:
            return "sem ligação à internet neste momento (o endereço do Yahoo não resolveu) -- tenta outra vez"
        if isinstance(reason, TimeoutError) or "timed out" in text:
            return "o Yahoo demorou demasiado a responder -- tenta outra vez"
        return f"falha de rede: {reason}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "o Yahoo demorou demasiado a responder -- tenta outra vez"
    return str(exc)


def yahoo_result_retrying(symbol: str, rng: str = "5d") -> dict:
    """Network blips here are common enough (and cheap enough to retry) that one
    second attempt saves the user a confusing error."""
    try:
        return _yahoo_result(symbol, rng)
    except urllib.error.HTTPError:
        raise  # a real answer from Yahoo -- retrying won't change it
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        time.sleep(1.0)
        return _yahoo_result(symbol, rng)


def yahoo_chart_retrying(symbol: str) -> dict:
    return yahoo_result_retrying(symbol)["meta"]


# Moeda de base do orçamento (definição «currency»): tudo o que vem noutra moeda converte-se para esta.
BASE_CURRENCY = "EUR"


def set_base_currency(code: Any) -> None:
    global BASE_CURRENCY
    code = str(code or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", code) and code != BASE_CURRENCY:
        BASE_CURRENCY = code
        _fx_cache.clear()


def fx_pair_for(currency: str) -> tuple[str | None, float]:
    """(Yahoo FX symbol, sub-unit divisor) for a quote currency; symbol is None for
    euros. Yahoo mixes sub-unit quotes in without warning: London listings come in GBp
    (pence) and some futures in USd (cents), 1/100 of the currency the pair quotes."""
    cur = (currency or BASE_CURRENCY).strip()
    divisor = 1.0
    if cur in ("GBp", "GBX"):
        cur, divisor = "GBP", 100.0
    elif cur == "USd":
        cur, divisor = "USD", 100.0
    elif cur == "ZAc":
        cur, divisor = "ZAR", 100.0
    cur = cur.upper()
    return (None if cur == BASE_CURRENCY else cur), divisor


def fx_to_eur(currency: str) -> float | None:
    """Multiplier that turns one unit of `currency` into euros, or None if unknown."""
    cur, divisor = fx_pair_for(currency)
    if cur is None:
        return 1.0 / divisor
    cached = _fx_cache.get(cur)
    if cached and time.time() - cached[0] < PRICE_TTL:
        rate = cached[1]
    else:
        try:
            # EURUSD=X é «USD por 1 EUR» (a moeda de base à frente), por isso o caminho inverso é uma divisão.
            meta = yahoo_chart_retrying(f"{BASE_CURRENCY}{cur}=X")
            rate = meta.get("regularMarketPrice")
            if not rate:
                return None
            rate = float(rate)
            _fx_cache[cur] = (time.time(), rate)
        except Exception:
            return None
    return 1.0 / (rate * divisor)


def fetch_price(ticker: str) -> dict:
    """Current price for a ticker via Yahoo Finance's chart endpoint (unofficial, no
    key required). Covers stocks, ETFs, crypto pairs (BTC-EUR) and commodity futures
    (GC=F) through the same code path, and always reports a euro price alongside the
    native one -- without that, a USD-quoted holding was being summed straight into a
    euro total. Cached a few minutes since it's an undocumented endpoint that could
    rate-limit or change without notice."""
    raw = (ticker or "").strip().upper()
    symbol = resolve_symbol(raw)
    cached = _price_cache.get(symbol)
    if cached and time.time() - cached[0] < (PRICE_TTL if cached[1].get("ok") else FAIL_TTL):
        return dict(cached[1], input=raw)
    try:
        result = yahoo_result_retrying(symbol)
        meta = result["meta"]
        price = meta.get("regularMarketPrice")
        if price in (None, ""):
            raise ValueError(
                f"o Yahoo conhece '{symbol}' mas não publica cotação para ele "
                "(costuma ser um símbolo extinto com o mesmo nome)"
            )
        currency = meta.get("currency") or BASE_CURRENCY
        mult = fx_to_eur(currency)
        if mult is None:
            # Better no number than a wrong one: without the rate, a USD quote passed
            # through as euros would understate gold by about a sixth.
            raise ValueError(f"não consegui o câmbio {currency}→{BASE_CURRENCY} para converter este preço")
        payload = {
            "ok": True,
            "ticker": symbol,
            "input": raw,
            "name": meta.get("longName") or meta.get("shortName") or symbol,
            "price": price,
            "currency": currency,
            "price_eur": price * mult,
            "fx_rate": mult,
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "week52_high": meta.get("fiftyTwoWeekHigh"),
            "week52_low": meta.get("fiftyTwoWeekLow"),
            "fetched_at": now(),
        }
        # The day's move, taken from the series rather than meta: `previousClose` is
        # often absent on futures and crypto, and `chartPreviousClose` refers to the
        # close before the whole window, not yesterday.
        series = _closes(result)
        if len(series) >= 2:
            prev = series[-2][1]
            payload["prev_close"] = prev
            payload["day_change_pct"] = ((price - prev) / prev * 100) if prev else None
    except Exception as exc:  # rede, timeout, ticker inválido -- o cliente só precisa de saber que falhou
        hint = f" (tentei '{symbol}' por '{raw}')" if raw != symbol else ""
        payload = {"ok": False, "ticker": symbol, "input": raw,
                   "error": f"{explain_net_error(exc)}{hint}"}
    _price_cache[symbol] = (time.time(), payload)
    return payload


def amount_to_eur(amount, currency: str) -> float:
    """Money as the user typed it -> euros. Raises when the rate can't be had, so a
    dollar figure is never stored as though it were euros."""
    value = float(amount)
    cur = (currency or BASE_CURRENCY).strip().upper()
    if cur == BASE_CURRENCY:
        return value
    mult = fx_to_eur(cur)
    if mult is None:
        raise ValueError(f"não consegui o câmbio {cur}→{BASE_CURRENCY} para converter este valor")
    return value * mult


def price_in_eur(info: dict) -> float | None:
    """Euro price out of a fetch_price payload, falling back to the native quote."""
    if not info.get("ok"):
        return None
    val = info.get("price_eur")
    return val if val is not None else info.get("price")


HISTORY_TTL = int(os.environ.get("RUMO_HISTORY_TTL", "3600"))
_history_cache: dict[str, tuple[float, list]] = {}
# What the UI may ask for, mapped to Yahoo's own range words. Anything else is
# rejected rather than passed through to the URL.
HISTORY_RANGES = {"1mo": "1mo", "6mo": "6mo", "1y": "1y", "5y": "5y"}


def yahoo_history(symbol: str, rng: str) -> list[tuple[str, float]]:
    """Daily closes for a symbol. Cached by the hour -- daily bars don't move faster."""
    key = f"{symbol}|{rng}"
    cached = _history_cache.get(key)
    if cached and time.time() - cached[0] < HISTORY_TTL:
        return cached[1]
    try:
        series = _closes(yahoo_result_retrying(symbol, rng))
    except Exception:
        series = []
    _history_cache[key] = (time.time(), series)
    return series


def holdings_history(rows: list, rng: str) -> dict:
    """What today's positions would have been worth each day over the range.

    Deliberately not "how the portfolio actually did": only the current quantities are
    known, not when they were bought, so this reprices today's holdings backwards. The
    UI has to say as much."""
    per_holding = []
    for row in rows:
        ticker = (row["ticker"] or "").strip()
        qty = float(row["quantity"] or 0)
        if not ticker or qty <= 0:
            continue
        series = yahoo_history(ticker, rng)
        if not series:
            continue
        info = fetch_price(ticker)
        cur, divisor = fx_pair_for(info.get("currency") if info.get("ok") else BASE_CURRENCY)
        rates = {}
        if cur is not None:
            rates = dict(yahoo_history(f"{BASE_CURRENCY}{cur}=X", rng))
            if not rates:
                continue  # no rate history means no honest euro figure for this one
        by_day = {}
        last_rate = None
        for day, close in series:
            if cur is None:
                by_day[day] = close * qty / divisor
                continue
            # FX and equity calendars differ; carry the last known rate forward.
            last_rate = rates.get(day, last_rate)
            if last_rate is None:
                continue
            by_day[day] = close * qty / (last_rate * divisor)
        if by_day:
            per_holding.append(by_day)

    if not per_holding:
        return {"labels": [], "values": []}

    # Start where every position already has data, so nothing steps in mid-chart and
    # looks like a gain that never happened.
    start = max(min(d) for d in per_holding)
    days = sorted({d for holding in per_holding for d in holding if d >= start})
    values = []
    carried = [None] * len(per_holding)
    for day in days:
        total = 0.0
        for idx, holding in enumerate(per_holding):
            if day in holding:
                carried[idx] = holding[day]
            if carried[idx] is None:
                total = None
                break
            total += carried[idx]
        if total is not None:
            values.append(round(total, 2))
        else:
            values.append(None)
    pairs = [(d, v) for d, v in zip(days, values) if v is not None]
    return {"labels": [d for d, _ in pairs], "values": [v for _, v in pairs]}


# Curated pickers per investment kind: everything Revolut offers under Commodities,
# the crypto it lists (as EUR pairs, so no conversion is needed), and a starter set of
# the ETFs/stocks most likely to show up here. Anything missing is reachable through
# the live Yahoo search behind the same dropdown.
MARKET_CATALOG = {
    "commodity": [
        {"symbol": "GC=F", "label": "Ouro — XAU", "hint": "por onça troy, futuros COMEX"},
        {"symbol": "SI=F", "label": "Prata — XAG", "hint": "por onça troy, futuros COMEX"},
        {"symbol": "PL=F", "label": "Platina — XPT", "hint": "por onça troy, futuros NYMEX"},
        {"symbol": "PA=F", "label": "Paládio — XPD", "hint": "por onça troy, futuros NYMEX"},
        {"symbol": "4GLD.DE", "label": "Ouro físico em euros — Xetra-Gold", "hint": "por grama, cotado em EUR"},
        {"symbol": "CL=F", "label": "Petróleo WTI", "hint": "por barril, futuros NYMEX"},
        {"symbol": "BZ=F", "label": "Petróleo Brent", "hint": "por barril, futuros ICE"},
        {"symbol": "NG=F", "label": "Gás natural", "hint": "futuros NYMEX"},
        {"symbol": "HG=F", "label": "Cobre", "hint": "por libra, futuros COMEX"},
    ],
    "crypto": [
        {"symbol": f"{code}-EUR", "label": name, "hint": "cotado em EUR"}
        for code, name in [
            ("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("XRP", "XRP"), ("SOL", "Solana"),
            ("BNB", "BNB"), ("ADA", "Cardano"), ("DOGE", "Dogecoin"), ("TRX", "TRON"),
            ("LINK", "Chainlink"), ("AVAX", "Avalanche"), ("XLM", "Stellar"),
            ("DOT", "Polkadot"), ("LTC", "Litecoin"), ("BCH", "Bitcoin Cash"),
            ("UNI", "Uniswap"), ("NEAR", "NEAR Protocol"), ("APT", "Aptos"),
            ("ICP", "Internet Computer"), ("ETC", "Ethereum Classic"), ("POL", "Polygon"),
            ("ALGO", "Algorand"), ("VET", "VeChain"), ("FIL", "Filecoin"),
            ("ATOM", "Cosmos"), ("AAVE", "Aave"), ("SHIB", "Shiba Inu"),
            ("PEPE", "Pepe"), ("SUI", "Sui"), ("XTZ", "Tezos"), ("SAND", "The Sandbox"),
        ]
    ],
    "equity": [
        {"symbol": "VWCE.DE", "label": "Vanguard FTSE All-World (acum.)", "hint": "ETF mundo, EUR"},
        {"symbol": "IWDA.AS", "label": "iShares Core MSCI World", "hint": "ETF mundo, EUR"},
        {"symbol": "EUNL.DE", "label": "iShares Core MSCI World (Xetra)", "hint": "ETF mundo, EUR"},
        {"symbol": "CSPX.AS", "label": "iShares Core S&P 500 (acum.)", "hint": "ETF EUA, EUR"},
        {"symbol": "SXR8.DE", "label": "iShares Core S&P 500 (Xetra)", "hint": "ETF EUA, EUR"},
        {"symbol": "EIMI.AS", "label": "iShares Core MSCI Emerging Markets", "hint": "ETF emergentes, EUR"},
        {"symbol": "VUAA.DE", "label": "Vanguard S&P 500 (acum.)", "hint": "ETF EUA, EUR"},
        {"symbol": "EQQQ.DE", "label": "Invesco EQQQ Nasdaq-100", "hint": "ETF tecnologia, EUR"},
        {"symbol": "AAPL", "label": "Apple", "hint": "ação EUA, USD"},
        {"symbol": "MSFT", "label": "Microsoft", "hint": "ação EUA, USD"},
        {"symbol": "NVDA", "label": "NVIDIA", "hint": "ação EUA, USD"},
        {"symbol": "GOOGL", "label": "Alphabet", "hint": "ação EUA, USD"},
        {"symbol": "AMZN", "label": "Amazon", "hint": "ação EUA, USD"},
        {"symbol": "TSLA", "label": "Tesla", "hint": "ação EUA, USD"},
        {"symbol": "META", "label": "Meta Platforms", "hint": "ação EUA, USD"},
        {"symbol": "EDP.LS", "label": "EDP", "hint": "ação Euronext Lisboa, EUR"},
        {"symbol": "GALP.LS", "label": "Galp Energia", "hint": "ação Euronext Lisboa, EUR"},
        {"symbol": "JMT.LS", "label": "Jerónimo Martins", "hint": "ação Euronext Lisboa, EUR"},
        {"symbol": "BCP.LS", "label": "Banco Comercial Português", "hint": "ação Euronext Lisboa, EUR"},
        {"symbol": "NOS.LS", "label": "NOS SGPS", "hint": "ação Euronext Lisboa, EUR"},
    ],
}


def guess_kind(category_name: str) -> str:
    """Which catalog fits an investment category, judged by its name -- categories are
    free-text ('Bens', 'Crypto', 'Acções', 'Bot') and the user renames them."""
    n = unicodedata.normalize("NFKD", (category_name or "").strip().lower())
    n = "".join(c for c in n if not unicodedata.combining(c))
    if any(w in n for w in ("bot", "robo", "algor")):
        return "none"
    if any(w in n for w in ("crypt", "cript", "coin", "moeda digital")):
        return "crypto"
    if any(w in n for w in ("bem", "bens", "commod", "metal", "metai", "ouro", "materia")):
        return "commodity"
    return "equity"


def search_symbols(query: str) -> list[dict]:
    """Live Yahoo symbol search, so the dropdown isn't limited to the curated list."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    key = q.lower()
    cached = _search_cache.get(key)
    if cached and time.time() - cached[0] < PRICE_TTL:
        return cached[1]
    try:
        url = (
            "https://query1.finance.yahoo.com/v1/finance/search?q="
            f"{urllib.parse.quote(q)}&quotesCount=12&newsCount=0"
        )
        req = urllib.request.Request(url, headers={"User-Agent": _YAHOO_UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for item in data.get("quotes", []):
            sym = item.get("symbol")
            if not sym:
                continue
            out.append({
                "symbol": sym,
                "label": item.get("longname") or item.get("shortname") or sym,
                "hint": " - ".join(x for x in (item.get("typeDisp"), item.get("exchDisp")) if x),
            })
    except Exception:
        out = []
    _search_cache[key] = (time.time(), out)
    return out

NEWS_TTL = int(os.environ.get("RUMO_NEWS_TTL", "900"))
_news_cache: dict[str, tuple[float, list[dict]]] = {}


def fetch_news(query: str, count: int = 6) -> list[dict]:
    """Headlines for one symbol or topic. Never raises: news is a nice-to-have."""
    q = (query or "").strip()
    if not q:
        return []
    key = f"{q.lower()}|{count}"
    cached = _news_cache.get(key)
    if cached and time.time() - cached[0] < NEWS_TTL:
        return cached[1]
    out = []
    try:
        url = (
            "https://query1.finance.yahoo.com/v1/finance/search?q="
            f"{urllib.parse.quote(q)}&quotesCount=0&newsCount={int(count)}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": _YAHOO_UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("news", []):
            title = (item.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "title": title,
                "publisher": item.get("publisher") or "",
                "link": item.get("link") or "",
                "published": item.get("providerPublishTime"),
                "tickers": [t for t in (item.get("relatedTickers") or []) if t][:4],
            })
    except Exception:
        out = []
    _news_cache[key] = (time.time(), out)
    return out


def catalog_label(symbol: str) -> str:
    for items in MARKET_CATALOG.values():
        for it in items:
            if it.get("symbol") == symbol:
                return it.get("label") or ""
    return ""


# Palavras de nome de empresa que não dizem qual é a empresa.
NAME_FILLER = {"and", "the", "inc", "inc.", "corp", "corp.", "corporation", "company", "co", "co.", "group",
               "holdings", "holding", "plc", "ltd", "limited", "sa", "ag", "nv", "se", "spa", "class",
               "ucits", "etf", "acc", "dist", "usd", "eur", "index", "fund", "trust", "shares"}


def name_words(name: str) -> list[str]:
    """As palavras do nome que identificam o ativo («Eli Lilly and Company» → Eli, Lilly)."""
    # Letras com acento contam como letras: «Paládio» é uma palavra, não «Pal» + «dio».
    words = re.findall(r"[^\W\d_][\w&'-]{2,}", re.sub(r"\s*\([^)]*\)", "", name or ""))
    return [w for w in words if w.lower() not in NAME_FILLER][:3]


def headline_names_asset(title: str, symbol: str, name: str) -> bool:
    root = re.split(r"[.\-=]", symbol)[0]
    keys = ([root] if len(root) >= 2 else []) + name_words(name)
    return any(re.search(rf"\b{re.escape(k)}\b", title, re.I) for k in keys)


def relevant_news(symbol: str, name: str = "", count: int = 6, strict: bool = False) -> list[dict]:
    """Headlines that are actually about this asset.

    Asked about a symbol it has no coverage for -- most crypto pairs, European ETF
    listings -- Yahoo answers with the same handful of general-finance stories rather
    than with nothing. Two tests tell coverage from filler: the symbol is tagged in
    relatedTickers, or the asset's own name appears in the headline.
    """
    # Marcada pelo Yahoo não basta: as listas («3 ações para ver») marcam muitas de uma
    # vez. Entra se fala de no máximo duas, ou se o título nomeia mesmo o ativo.
    # Nos futuros (ouro, prata…) a marca do Yahoo erra muito («PAL» como paládio): só o título conta.
    tagged = [] if strict and symbol.endswith("=F") else [
        it for it in fetch_news(symbol, count) if symbol in (it.get("tickers") or [])
        and (not strict or len(it.get("tickers") or []) <= 2 or headline_names_asset(it["title"], symbol, name))]
    if tagged:
        return tagged
    # The curated label beats Yahoo's own name as a search term: "Bitcoin" finds
    # headlines, "Bitcoin EUR" finds none because it reads as the pair, not the asset.
    # As manchetes do Yahoo são em inglês: o nome do Yahoo («Gold Dec 26» → Gold) pesquisa
    # melhor do que o rótulo do catálogo, que está em português («Ouro — XAU»).
    topic = (name or catalog_label(symbol) or "").strip()
    topic = re.sub(r"\s*\([^)]*\)", "", topic).strip()
    if not topic:
        return []
    # A primeira palavra própria do nome: «Ethereum EUR» pesquisa-se como «Ethereum».
    words = name_words(topic)
    if not words:
        return []
    word = words[0]
    # Whole word only: "Cartesi" must not match "Cartesian Therapeutics".
    pat = re.compile(rf"\b{re.escape(word)}\b", re.I)
    return [it for it in fetch_news(word, count) if pat.search(it["title"])]


def portfolio_news(assets: list[tuple[str, str]], per_symbol: int = 5,
                   limit: int = 14, strict: bool = False) -> list[dict]:
    """Headlines across the holdings, deduplicated.

    Taken round-robin rather than newest-first: a single asset with a busy news day
    would otherwise fill the whole list and the rest of the portfolio would look as
    though nothing had happened to it.
    """
    per_asset, seen = [], set()
    for sym, name in assets:
        picked = []
        for item in relevant_news(sym, name, per_symbol, strict):
            if item["title"] in seen:
                continue
            seen.add(item["title"])
            picked.append(dict(item, query=sym))
        picked.sort(key=lambda x: x.get("published") or 0, reverse=True)
        if picked:
            per_asset.append(picked)

    out = []
    for depth in range(per_symbol):
        for bucket in per_asset:
            if depth < len(bucket):
                out.append(bucket[depth])
        if len(out) >= limit:
            break
    return out[:limit]


ANTHROPIC_MODEL = os.environ.get("RUMO_ANTHROPIC_MODEL", "claude-sonnet-5")


def call_anthropic(api_key: str, prompt: str) -> tuple[bool, str]:
    """One-shot call to the Anthropic Messages API. Returns (ok, text_or_error)."""
    try:
        body = json.dumps({
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        if not text:
            return False, "A API respondeu sem texto"
        return True, text
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except Exception:
            pass
        return False, f"Erro {exc.code} da API Anthropic: {detail}"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


OPENAI_MODEL = os.environ.get("RUMO_OPENAI_MODEL", "gpt-4o")
GEMINI_MODEL = os.environ.get("RUMO_GEMINI_MODEL", "gemini-2.0-flash")
DEEPSEEK_MODEL = os.environ.get("RUMO_DEEPSEEK_MODEL", "deepseek-chat")


def _http_json(url: str, body: dict, headers: dict, provider_label: str) -> tuple[bool, Any]:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={**headers, "content-type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = (parsed.get("error") or {}).get("message") or parsed.get("message") or detail
        except Exception:
            pass
        return False, f"Erro {exc.code} da API {provider_label}: {detail}"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def call_openai(api_key: str, prompt: str) -> tuple[bool, str]:
    ok, data = _http_json(
        "https://api.openai.com/v1/chat/completions",
        {"model": OPENAI_MODEL, "messages": [{"role": "user", "content": prompt}]},
        {"Authorization": f"Bearer {api_key}"},
        "OpenAI",
    )
    if not ok:
        return False, data
    try:
        return True, data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return False, "A API OpenAI respondeu sem texto"


def call_gemini(api_key: str, prompt: str) -> tuple[bool, str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    ok, data = _http_json(url, {"contents": [{"parts": [{"text": prompt}]}]}, {"x-goog-api-key": api_key}, "Gemini")
    if not ok:
        return False, data
    try:
        return True, data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return False, "A API Gemini respondeu sem texto"


def call_deepseek(api_key: str, prompt: str) -> tuple[bool, str]:
    ok, data = _http_json(
        "https://api.deepseek.com/chat/completions",
        {"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}]},
        {"Authorization": f"Bearer {api_key}"},
        "DeepSeek",
    )
    if not ok:
        return False, data
    try:
        return True, data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return False, "A API DeepSeek respondeu sem texto"


AI_PROVIDERS = {
    "anthropic": ("anthropic_api_key", call_anthropic),
    "openai": ("openai_api_key", call_openai),
    "gemini": ("gemini_api_key", call_gemini),
    "deepseek": ("deepseek_api_key", call_deepseek),
}


def call_ai(db: sqlite3.Connection, prompt: str) -> tuple[bool, str]:
    provider_row = db.execute("SELECT value FROM settings WHERE key='ai_provider'").fetchone()
    provider = json.loads(provider_row["value"]) if provider_row else "anthropic"
    if provider not in AI_PROVIDERS:
        provider = "anthropic"
    key_setting, fn = AI_PROVIDERS[provider]
    key_row = db.execute("SELECT value FROM settings WHERE key=?", (key_setting,)).fetchone()
    api_key = json.loads(key_row["value"]) if key_row else ""
    if not api_key:
        return False, f"Adiciona a tua chave da API ({provider}) em Definições para usar isto."
    lang_row = db.execute("SELECT value FROM settings WHERE key='language'").fetchone()
    if not lang_row or json.loads(lang_row["value"]) != "pt-PT":
        prompt += ("\n\nIMPORTANT: write the whole answer in English, whatever language the instructions above are in "
                   "(ignore any request to write in Portuguese). Keep any JSON keys and section markers exactly as requested.")
    return fn(api_key, prompt)


# A handful of instruments that place everything else in context. Fetched live like
# any other quote, because a model's own idea of "the market right now" is frozen at
# its training cutoff and will happily quote a year-old level as today's.
MARKET_BENCHMARKS = [
    ("IWDA.AS", "Ações mundiais (iShares Core MSCI World)"),
    ("^GSPC", "S&P 500"),
    ("^STOXX50E", "Euro Stoxx 50"),
    ("GC=F", "Ouro"),
    ("BTC-EUR", "Bitcoin"),
    ("EURUSD=X", "EUR/USD"),
]


def perf_summary(symbol: str) -> dict:
    """Percentage moves over 1 month, 6 months and a year, from one cached series."""
    series = yahoo_history(symbol, "1y")
    if len(series) < 2:
        return {}
    last_day, last = series[-1]
    today = datetime.date.fromisoformat(last_day)
    out = {"last": last}
    span = (today - datetime.date.fromisoformat(series[0][0])).days
    for label, days in (("1m", 30), ("6m", 182), ("1a", 365)):
        # Yahoo's "1y" is ~250 trading days and can start a day or two short of the
        # cutoff; near enough still answers "how did this do over a year".
        if span < days * 0.8:
            continue
        cutoff = (today - datetime.timedelta(days=days)).isoformat()
        earlier = next((c for d, c in reversed(series) if d <= cutoff), series[0][1])
        if earlier:
            out[label] = (last / earlier - 1) * 100
    return out


def fmt_perf(perf: dict) -> str:
    parts = [f"{lbl} {perf[key]:+.1f}%"
             for key, lbl in (("1m", "1 mês"), ("6m", "6 meses"), ("1a", "1 ano"))
             if perf.get(key) is not None]
    return ", ".join(parts) if parts else "sem histórico suficiente"


def market_briefing() -> list[str]:
    lines = ["Contexto de mercado (cotações obtidas agora, do Yahoo Finance):"]
    for symbol, label in MARKET_BENCHMARKS:
        info = fetch_price(symbol)
        if not info.get("ok"):
            continue
        perf = perf_summary(symbol)
        day = info.get("day_change_pct")
        day_txt = f", hoje {day:+.2f}%" if day is not None else ""
        lines.append(
            f"- {label} [{symbol}]: {info['price']:.2f} {info['currency']}{day_txt}"
            f" | variação: {fmt_perf(perf)}"
        )
    return lines


def market_overview() -> list[dict]:
    """The benchmark row, in the shape the UI draws: price, day move, 1m/6m/1a."""
    out = []
    for symbol, label in MARKET_BENCHMARKS:
        try:
            info = fetch_price(symbol)
        except Exception:
            continue
        if not info.get("ok"):
            continue
        row = {
            "symbol": symbol,
            "label": label,
            "price": info.get("price"),
            "currency": info.get("currency"),
            "day_change_pct": info.get("day_change_pct"),
        }
        try:
            row.update({k: v for k, v in perf_summary(symbol).items() if k != "last"})
        except Exception:
            pass
        out.append(row)
    return out


def holding_assets(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Distinct held assets as (symbol, name), largest position in euros first."""
    rows = db.execute(
        """SELECT ticker, quantity, name FROM holdings
           WHERE ticker IS NOT NULL AND ticker != '' AND manual_value IS NULL"""
    ).fetchall()
    acc: dict[str, dict] = {}
    for r in rows:
        sym = resolve_symbol(r["ticker"])
        entry = acc.setdefault(sym, {"value": 0.0, "name": ""})
        try:
            info = fetch_price(sym)
            eur = price_in_eur(info) if info.get("ok") else None
            entry["value"] += (eur or 0.0) * float(r["quantity"] or 0)
            entry["name"] = entry["name"] or (info.get("name") or "")
        except Exception:
            pass
        entry["name"] = entry["name"] or (r["name"] or "")
    return [(s, d["name"]) for s, d in sorted(acc.items(), key=lambda kv: -kv[1]["value"])]


def savings_history(db: sqlite3.Connection, limit: int = 18) -> list[dict]:
    """One row per month, oldest first: what came in, what went out, what stayed."""
    rows = db.execute(
        "SELECT id, year, month, ordenado, extras, fatia_jobs, joint_partner_contribution, "
        "       computed_despesas_santander, computed_despesas_revolut, "
        "       computed_poupanca_total, computed_livre_resto, "
        "       split_geral, split_cofres, split_investimentos, closed, historical_import "
        "FROM months ORDER BY year DESC, month DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for m in reversed(rows):
        entrada = month_income(m)
        despesas = (m["computed_despesas_santander"] or 0) + (m["computed_despesas_revolut"] or 0)
        poupou = m["computed_poupanca_total"] or 0
        out.append({
            "month_id": m["id"],
            "label": f"{m['year']}-{m['month']:02d}",
            "ordenado": m["ordenado"] or 0,
            "extras": entrada - (m["ordenado"] or 0),  # extras + Fatia: o que entra além do ordenado
            "entrada": entrada,
            "despesas": despesas,
            "poupou": poupou,
            "taxa": (poupou / entrada * 100) if entrada else None,
            "sobrou": m["computed_livre_resto"] or 0,
            "split": [m["split_geral"], m["split_cofres"], m["split_investimentos"]],
            "closed": bool(m["closed"]),
            "historical": bool(m["historical_import"]),
        })
    return out


def yearly_history(db: sqlite3.Connection, top: int = 6) -> list[dict]:
    """Um resumo por ano, com todos os meses (os importados do Excel incluídos), para a
    IA comparar anos: o histórico mês a mês só leva os últimos 18. As categorias contam
    só os meses com linhas detalhadas (Jan-Jun 2026 têm apenas o total importado)."""
    hist = savings_history(db, limit=10 ** 6)
    cats = db.execute(
        f"""SELECT mo.year, e.category, SUM(e.planned) AS total, COUNT(DISTINCT mo.id) AS n
            FROM month_expenses e JOIN months mo ON mo.id = e.month_id
            WHERE mo.closed = 1 AND mo.id NOT IN (SELECT month_id FROM month_expenses WHERE category = 'Histórico')
              AND {NOT_INCOME_SQL.replace('category', 'e.category')}
            GROUP BY mo.year, e.category"""
    ).fetchall()
    detailed = {r["year"]: r["n"] for r in db.execute(
        """SELECT mo.year, COUNT(DISTINCT mo.id) AS n FROM month_expenses e
           JOIN months mo ON mo.id = e.month_id
           WHERE mo.closed = 1 AND mo.id NOT IN (SELECT month_id FROM month_expenses WHERE category = 'Histórico')
           GROUP BY mo.year""")}
    out = []
    for year in sorted({int(h["label"][:4]) for h in hist}):
        rows = [h for h in hist if int(h["label"][:4]) == year and h["closed"]]
        if not rows:
            continue
        n = len(rows)
        entrada = sum(h["entrada"] for h in rows)
        poupou = sum(h["poupou"] for h in rows)
        detail = [c for c in cats if c["year"] == year]
        out.append({
            "year": year,
            "months": [int(h["label"][5:]) for h in rows],
            "entrada_media": entrada / n,
            "despesas_media": sum(h["despesas"] for h in rows) / n,
            "poupou_total": poupou,
            "poupou_media": poupou / n,
            "taxa": poupou / entrada * 100 if entrada else None,
            "top_categorias": [(c["category"], c["total"] / detailed[year])
                               for c in sorted(detail, key=lambda c: -c["total"])[:top] if c["total"] > 0],
        })
    return out


def pot_pace(db: sqlite3.Connection, months: int = 6) -> dict[int, dict]:
    """Recent monthly rate per pot, and how long its goal would take at that rate.

    The arithmetic is done here rather than left to the model: "at this rate you get
    there in eleven months" is a division, and a wrong one reads just as confident.
    """
    rows = db.execute(
        """SELECT s.pot_id, s.contributed, mo.year, mo.month
           FROM pot_snapshots s JOIN months mo ON mo.id = s.month_id
           ORDER BY mo.year DESC, mo.month DESC"""
    ).fetchall()
    recent_months = sorted({(r["year"], r["month"]) for r in rows}, reverse=True)[:months]
    keep = set(recent_months)
    acc: dict[int, list[float]] = {}
    for r in rows:
        if (r["year"], r["month"]) in keep:
            acc.setdefault(r["pot_id"], []).append(r["contributed"] or 0)
    return {pid: {"per_month": sum(v) / len(v), "months_counted": len(v)}
            for pid, v in acc.items() if v}


RATE_MODES = ("none", "tanb", "eur")


def pot_interest(pot: dict, balance: float) -> float:
    """Juro líquido previsto num mês sobre `balance`: TANB (% ao ano, antes de imposto) ou um valor fixo em moeda."""
    mode = pot.get("rate_mode") or "none"
    value = float(pot.get("rate_value") or 0)
    if mode == "tanb":
        return max(0.0, balance) * value / 100 / 12 * (1 - float(pot.get("rate_tax") or 0) / 100)
    if mode == "eur":
        return value
    return 0.0


def pot_projection(pot: dict, balance: float, per_month: float, months: int = 12) -> list[float]:
    """Saldo mês a mês (sem o ponto de partida): entrada habitual + juro previsto sobre o saldo anterior."""
    out, bal = [], balance
    for _ in range(months):
        bal += per_month + pot_interest(pot, bal)
        out.append(round(bal, 2))
    return out


def goal_progress(pot: dict, pace: dict | None) -> dict | None:
    """Where a pot stands against its goal, in euros and in months."""
    goal = pot.get("goal_amount")
    if not goal or goal <= 0:
        return None
    have = pot.get("accumulated") or 0
    missing = max(0.0, goal - have)
    rate = (pace or {}).get("per_month") or 0
    out = {
        "goal": goal,
        "have": have,
        "missing": missing,
        "pct": min(100.0, have / goal * 100) if goal else 0,
        "per_month": rate,
        "months_needed": None,
        "date_needed": None,
        "on_track": None,
    }
    if missing <= 0:
        out["months_needed"] = 0
    elif rate > 0 or pot_interest(pot, have) > 0:
        # Com juros o saldo cresce mais depressa do que a entrada: simula-se mês a mês.
        if (pot.get("rate_mode") or "none") == "none":
            out["months_needed"] = math.ceil(missing / rate)
        else:
            bal, n = have, 0
            while bal < goal and n < 1200:
                bal += rate + pot_interest(pot, bal)
                n += 1
            out["months_needed"] = n if bal >= goal else None
    if out["months_needed"] is not None:
        today = datetime.date.today()
        n = out["months_needed"]
        y, mth = today.year + (today.month - 1 + n) // 12, (today.month - 1 + n) % 12 + 1
        out["date_needed"] = f"{y}-{mth:02d}"
        if pot.get("goal_date"):
            out["on_track"] = out["date_needed"] <= str(pot["goal_date"])[:7]
    return out


def _pot_payload(pot: dict, contributed: float, accumulated: float, pace: dict | None) -> dict:
    out = pot | {
        "active": bool(pot["active"]),
        "contributed_this_month": contributed,
        "accumulated": accumulated,
        "per_month": (pace or {}).get("per_month"),
    }
    out["goal"] = goal_progress(out, pace)
    if (pot.get("rate_mode") or "none") != "none":
        per = (pace or {}).get("per_month") or 0
        proj = pot_projection(pot, accumulated, per)
        start = accumulated - contributed
        out["interest"] = {
            "this_month": round(pot_interest(pot, start), 2),
            "per_month_next": round(pot_interest(pot, accumulated), 2),
            "next12": round(sum(pot_interest(pot, b) for b in [accumulated] + proj[:-1]), 2),
            "projection": proj,
        }
    return out


# O tipo de cada cofre diz à IA para que serve o dinheiro (definido pelo utilizador).
POT_LEVELS = {
    1: "fixo -- valor guardado que não se mexe: não se acrescenta (ou muito raramente) nem se tira",
    2: "variável -- dinheiro posto para se ir gastando (consumíveis, peças, manutenção): o reforço acompanha o que se gasta",
    3: "objetivo -- juntar até um valor; só se usa numa urgência (linha amarela): serve de atrito às tentações",
}
POT_LEVELS_TEXT = (
    "Tipos de cofre: 1 = " + POT_LEVELS[1] + "; 2 = " + POT_LEVELS[2] + "; 3 = " + POT_LEVELS[3]
    + ". A Poupança Geral (fora dos cofres) é o nível 4: acumula e nunca se tira. "
    "Nos cofres o que der dá, e é um extra; se for preciso, usa-se."
)


def pot_kind_text(pot: dict) -> str:
    level = int(pot.get("level") or 0)
    bits = [f"tipo {level} ({POT_LEVELS[level].split(' -- ')[0]})"] if level in POT_LEVELS else []
    if str(pot.get("purpose") or "").strip():
        bits.append(f"para que serve: {str(pot['purpose']).strip()[:240]}")
    return (" | " + "; ".join(bits)) if bits else ""


def build_savings_prompt(db: sqlite3.Connection, summary: dict) -> str:
    hist = savings_history(db)
    pace = pot_pace(db)
    lines = []
    lines.append("Preciso de uma análise honesta das minhas poupanças e do que devo mudar.")
    lines.append("")
    lines.append(f"Hoje é {datetime.date.today().isoformat()}.")
    lines.append(
        "IMPORTANTE: todos os números de que precisas estão neste prompt. Não inventes "
        "valores nem contas: se um número que te faz falta não estiver aqui, diz qual é "
        "em vez de o estimares. As contas de ritmo e de prazo já vêm feitas -- usa-as "
        "como estão em vez de as refazeres."
    )
    lines.append("")

    priorities_row = db.execute("SELECT value FROM settings WHERE key='savings_priorities'").fetchone()
    priorities = json.loads(priorities_row["value"]) if priorities_row else ""
    if priorities.strip():
        lines.append(f"O que é importante para mim agora, nas minhas palavras: {priorities.strip()}")
    else:
        lines.append(
            "Não escrevi as minhas prioridades. Uma das tuas tarefas é perguntar-mas: no fim, "
            "faz-me duas ou três perguntas concretas cuja resposta mudaria a tua análise."
        )
    lines.append("")

    lines.append("Histórico mensal (entrada = ordenado + extras, com o que o Fatia faturou; poupou = o que foi para poupança):")
    for h in hist:
        taxa = f"{h['taxa']:.0f}%" if h["taxa"] is not None else "-"
        flag = " [mês em curso]" if not h["closed"] else (" [importado]" if h["historical"] else "")
        lines.append(
            f"- {h['label']}: ordenado {h['ordenado']:.2f}, extras {h['extras']:+.2f}, "
            f"despesas {h['despesas']:.2f}, poupou {h['poupou']:.2f} ({taxa} da entrada), "
            f"sobrou na conta {h['sobrou']:.2f}{flag}"
        )
    closed = [h for h in hist if h["closed"]]
    if closed:
        med = sorted(h["poupou"] for h in closed)[len(closed) // 2]
        avg = sum(h["poupou"] for h in closed) / len(closed)
        lines.append(
            f"(em {len(closed)} meses fechados: média {avg:.2f} {BASE_CURRENCY}/mês, mediana {med:.2f} {BASE_CURRENCY}/mês, "
            f"melhor {max(h['poupou'] for h in closed):.2f}, pior {min(h['poupou'] for h in closed):.2f})"
        )
    lines.append("")

    years = yearly_history(db)
    if len(years) > 1:
        lines.append(
            "Resumo por ano, com todos os meses fechados que tenho registados (médias por mês "
            "registado; os meses que faltam num ano são meses sem registo, não meses a zero). "
            "Até março de 2024 a folha punha de parte a poupança antes de pagar as despesas; "
            "desde setembro de 2024 as despesas saem primeiro, por isso a taxa de poupança dos "
            "anos antigos não se compara à letra com a de agora. Nas categorias, as despesas da "
            "casa pagas pela conta conjunta contam pelo valor inteiro (a minha parte é só uma "
            "fração), o que as faz subir nos meses em que a conjunta já existia:"
        )
        for y in years:
            taxa = f"{y['taxa']:.0f}%" if y["taxa"] is not None else "-"
            meses = ",".join(f"{m:02d}" for m in y["months"])
            cats = "; ".join(f"{c} {v:.0f}" for c, v in y["top_categorias"])
            lines.append(
                f"- {y['year']} ({len(y['months'])} meses: {meses}): entrada média {y['entrada_media']:.2f}, "
                f"despesas médias {y['despesas_media']:.2f}, poupou {y['poupou_total']:.2f} no total "
                f"({y['poupou_media']:.2f}/mês, {taxa} da entrada)"
                + (f"; maiores despesas por mês: {cats}" if cats else "")
            )
        lines.append("")

    lines.append(
        f"A poupança de cada mês é dividida em três: Geral {summary['split_geral']*100:.0f}%, "
        f"Cofres {summary['split_cofres']*100:.0f}%, Investimentos {summary['split_investimentos']*100:.0f}%."
    )
    lines.append(
        f"Este mês isso dá: Geral {summary['geral_this_month']:.2f} {BASE_CURRENCY}, "
        f"e o acumulado do Geral está em {summary['geral_acumulado']:.2f} {BASE_CURRENCY}."
    )
    lines.append(f"Poupança acumulada total (Geral + cofres + investimentos): {summary['poupanca_acumulada']:.2f} {BASE_CURRENCY}.")
    lines.append("")

    lines.append(
        "Os meus cofres. A percentagem é a fatia da parte 'Cofres' que cada um recebe; "
        "'ritmo' é a média mensal real dos últimos meses. " + POT_LEVELS_TEXT
    )
    for pot in summary["pots"]:
        pp = pace.get(pot["id"], {})
        rate = pp.get("per_month")
        rate_txt = f"{rate:.2f} EUR/mês" if rate is not None else "sem histórico"
        line = (f"- {pot['name']}: {pot['pct']*100:.1f}% da fatia dos cofres, "
                f"acumulado {pot['accumulated']:.2f} {BASE_CURRENCY}, este mês {pot['contributed_this_month']:.2f} {BASE_CURRENCY}, "
                f"ritmo {rate_txt}") + pot_kind_text(pot)
        gp = goal_progress(pot, pp) if int(pot.get("level") or 0) != 1 else None  # fixo: não recebe, de propósito
        if gp:
            line += f" | objetivo {gp['goal']:.2f} EUR ({gp['pct']:.0f}% feito, faltam {gp['missing']:.2f})"
            if gp["months_needed"] == 0:
                line += ", JÁ ATINGIDO"
            elif gp["months_needed"] is not None:
                line += f", a este ritmo chega lá em {gp['months_needed']} meses ({gp['date_needed']})"
            else:
                line += ", e a este ritmo NUNCA chega lá porque não recebe nada"
            if pot.get("goal_date"):
                line += f", prazo que eu quero: {pot['goal_date']}"
                if gp["on_track"] is False:
                    line += " -- NÃO chega a tempo"
                elif gp["on_track"] is True:
                    line += " -- chega a tempo"
        lines.append(line)
    total_pct = sum(p["pct"] for p in summary["pots"]) * 100
    lines.append(f"(as percentagens dos cofres somam {total_pct:.1f}%)")
    lines.append("")

    cats = db.execute(
        """SELECT e.category, SUM(e.planned) AS planned,
                  SUM(COALESCE(e.actual, e.planned)) AS actual, COUNT(*) AS n
           FROM month_expenses e WHERE e.month_id = ?
           GROUP BY e.category ORDER BY planned DESC""",
        (summary["id"],),
    ).fetchall()
    if cats:
        lines.append("Despesas do mês em curso, por categoria (previsto vs real):")
        for c in cats:
            if not (c["planned"] or c["actual"]):
                continue
            lines.append(f"- {c['category']}: previsto {c['planned']:.2f}, real {c['actual']:.2f} ({c['n']} linhas)")
        lines.append("")

    lines.append(
        "Com base nisto, escreve em português de Portugal e organiza a resposta exatamente "
        "por esta ordem, em markdown:"
    )
    lines.append("")
    lines.append(
        "1. **O que os números dizem** -- dois ou três pontos sobre o padrão real das minhas "
        "poupanças: quanto poupo num mês normal, o que distingue os meses bons dos maus, e se "
        "estou a melhorar ou a piorar. Usa a mediana e não só a média quando um mês extremo "
        "distorcer a leitura, e diz quando o fizeres.\n"
        "2. **Cofres que precisam de atenção** -- uma tabela markdown | Cofre | Situação | O que "
        "eu faria |, só com os que têm mesmo um problema: dinheiro parado sem receber nada, "
        "objetivo que não chega a tempo, percentagem que não corresponde à importância, ou "
        "acumulado grande sem objetivo definido. Não listes os que estão bem.\n"
        "3. **Percentagens** -- se mudarias a divisão Geral/Cofres/Investimentos ou as "
        "percentagens dos cofres, diz exatamente para que valores e o que isso muda em euros por "
        "mês. Se não mudarias nada, di-lo claramente em vez de inventar ajustes.\n"
        "4. **O ponto fraco** -- a única coisa que, mudada, teria mais efeito. Uma só.\n"
        "5. **Perguntas para mim** -- duas ou três perguntas cuja resposta mudaria a tua análise. "
        "Perguntas concretas sobre a minha vida e as minhas intenções, não sobre números que já "
        "tens aqui."
    )
    lines.append("")
    lines.append(
        "Regras: nomeia os números em que te baseias. Não me devolvas listas de dados que já "
        "tenho -- o valor está na leitura. Sê direto, incluindo quando a leitura for "
        "desagradável. Termina com um aviso curto de que isto não é aconselhamento financeiro "
        "profissional."
    )
    return "\n".join(lines)


# Ajuste da IA às % de investimentos ou de cofres. Usa os dados das análises (até ao
# marcador onde começam as instruções do relatório) e pede só as % em JSON. Os itens com
# cadeado (ai_locked) ficam como estão; a IA reparte o resto pelos livres.
INV_MARKER = "Com base nos dados acima, sugere concretamente onde e quanto devo investir este mês. "
POT_MARKER = "Com base nisto, escreve em português de Portugal e organiza a resposta exatamente "
REBALANCE_MAX_STEP = 15.0  # pontos percentuais que cada item pode mexer de uma vez
REBALANCE_KINDS = {"inv": ("investments", "das minhas categorias de investimento"), "pot": ("pots", "dos meus cofres")}


def rebalance_prompt(db: sqlite3.Connection, summary: dict, kind: str, items: list) -> str:
    if kind == "inv":
        context = build_investment_prompt(db, summary).split(INV_MARKER, 1)[0].rstrip()
    else:
        context = build_savings_prompt(db, summary).split(POT_MARKER, 1)[0].rstrip()
        budget = summary["poupanca_total"]
        context += (f"\n\nEste mês: poupança total {budget:.2f} {BASE_CURRENCY} -- Geral {budget * summary['split_geral']:.2f}, "
                    f"cofres {budget * summary['split_cofres']:.2f}, investimentos {budget * summary['split_investimentos']:.2f}.")
        last = setting(db, "last_savings_advice", None)
        if isinstance(last, dict) and last.get("text"):
            context += "\n\nA tua última análise das minhas poupanças (para coerência):\n" + str(last["text"])[:3500]
    free = [i for i in items if not i["ai_locked"]]
    locked = [i for i in items if i["ai_locked"]]
    room = 100 - sum(float(i["pct"]) * 100 for i in locked)
    lines = "\n".join(f"- id {i['id']}: {i['name']} (agora {float(i['pct']) * 100:.1f}%)"
                      + (pot_kind_text(dict(i)) if kind == "pot" else "") for i in free)
    fixed = "\n".join(f"- {i['name']}: {float(i['pct']) * 100:.1f}% (bloqueado, não mexer)"
                      + (pot_kind_text(dict(i)) if kind == "pot" else "") for i in locked)
    name = REBALANCE_KINDS[kind][1]
    return (
        f"{context}\n\n"
        f"TAREFA: ajusta as percentagens {name} com base nos dados acima. "
        f"Só podes mexer nestes (usa os ids):\n{lines}\n"
        + (f"Estes ficam como estão:\n{fixed}\n" if fixed else "")
        + f"\nRegras: as percentagens dos que podes mexer somam exatamente {room:.1f}; nenhuma é negativa; "
        f"cada uma muda no máximo {REBALANCE_MAX_STEP:.0f} pontos face ao valor atual; se nada nos dados "
        "justifica mudar uma, deixa-a igual. Cada razão cita o número, o objetivo ou a manchete concreta "
        "que a justifica. Não inventes dados que não estão acima.\n"
        "Responde APENAS com JSON válido, sem texto antes ou depois, neste formato:\n"
        '{"resumo": "duas ou três frases em português de Portugal com a lógica geral", '
        '"itens": [{"id": 1, "pct": 25.0, "porque": "razão curta"}]}'
    )


def parse_rebalance(text: str, items: list) -> dict:
    """Lê a resposta da IA e devolve {id: pct (0..1)} e as razões. Não confia nela:
    só ids livres que existem, sem negativos, no máximo REBALANCE_MAX_STEP de
    mudança; os bloqueados ficam iguais e o resto volta a somar o espaço que sobra."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("A IA não devolveu JSON.")
    data = json.loads(text[start:end + 1])
    current = {int(i["id"]): float(i["pct"]) * 100 for i in items}
    locked = {int(i["id"]) for i in items if i["ai_locked"]}
    asked, reasons = {}, {}
    for c in data.get("itens") or data.get("categorias") or []:
        try:
            cid = int(c.get("id"))
        except (TypeError, ValueError):
            continue
        if cid not in current or cid in locked:
            continue
        pct = max(0.0, float(c.get("pct") or 0))
        asked[cid] = min(max(pct, current[cid] - REBALANCE_MAX_STEP, 0.0), current[cid] + REBALANCE_MAX_STEP)
        reasons[cid] = str(c.get("porque") or "").strip()[:300]
    if not asked:
        raise ValueError("A IA não devolveu percentagens para os itens livres.")
    free = [cid for cid in current if cid not in locked]
    room = max(0.0, 100 - sum(current[cid] for cid in locked))
    merged = {cid: asked.get(cid, current[cid]) for cid in free}
    total = sum(merged.values())
    if total <= 0:
        raise ValueError("As percentagens da IA somam zero.")
    after = {cid: round(current[cid] / 100, 4) for cid in locked}
    after.update({cid: round(v / total * room / 100, 4) for cid, v in merged.items()})
    return {"after": after, "reasons": reasons, "resumo": str(data.get("resumo") or "").strip()[:800]}


def build_investment_prompt(db: sqlite3.Connection, month_summary_data: dict) -> str:
    invs = db.execute("SELECT * FROM investments WHERE active=1 ORDER BY sort_order, id").fetchall()
    holdings = db.execute(
        """SELECT h.*, i.name AS investment_name FROM holdings h
           JOIN investments i ON i.id = h.investment_id
           ORDER BY h.investment_id, h.sort_order, h.id"""
    ).fetchall()

    lines = []
    lines.append("Preciso de ajuda para decidir onde alocar o meu investimento deste mês.")
    lines.append("")
    lines.append(f"Hoje é {datetime.date.today().isoformat()}.")
    lines.append(
        "IMPORTANTE: o teu conhecimento interno está desatualizado em relação a esta data. "
        "Todos os números de mercado que precisas estão neste prompt, obtidos agora em tempo "
        "real, e são a tua única fonte de factos. Não cites de memória preços, cotações, níveis "
        "de índices, taxas de juro nem notícias: ou usas os dados abaixo, ou dizes explicitamente "
        "que não tens esse dado. Se a tua recomendação depender de algo que não está aqui, diz "
        "qual é o dado em falta em vez de o inventares."
    )
    lines.append("")
    lines.append(f"Orçamento total para investimentos este mês: {month_summary_data['poupanca_total'] * month_summary_data['split_investimentos']:.2f} {BASE_CURRENCY}")
    lines.append("")
    lines.append("As minhas categorias de investimento e percentagem-alvo de cada uma:")
    inv_by_id = {i["id"]: i for i in invs}
    for i in invs:
        contrib = next((x["contributed_this_month"] for x in month_summary_data["investimentos"] if x["id"] == i["id"]), 0)
        lines.append(f"- {i['name']}: {i['pct']*100:.1f}% do orçamento de investimentos (contribuição calculada este mês: {contrib:.2f} {BASE_CURRENCY})")
    lines.append("")
    lines.append("As minhas posições atuais (o que já tenho):")
    if holdings:
        for h in holdings:
            if h["manual_value"] is not None:
                lines.append(
                    f"- [{h['investment_name']}] {h['name'] or h['ticker'] or 'posição'}: "
                    f"valor atual indicado manualmente {h['manual_value']} {h['currency']} (sem histórico de compra)"
                )
                continue
            price_info = fetch_price(h["ticker"]) if h["ticker"] else {"ok": False}
            if not price_info.get("ok"):
                lines.append(
                    f"- [{h['investment_name']}] {h['ticker']} ({h['name'] or 'sem nome'}): "
                    f"{h['quantity']} unidades (sem cotação de momento)"
                )
                continue
            eur_price = price_in_eur(price_info)
            value = eur_price * float(h["quantity"] or 0)
            cost = float(h["avg_cost"] or 0) * float(h["quantity"] or 0)
            gain_txt = ""
            if cost:
                gain_txt = f", ganho/perda {value - cost:+.2f} EUR ({(value / cost - 1) * 100:+.1f}%)"
            perf = perf_summary(price_info["ticker"])
            lines.append(
                f"- [{h['investment_name']}] {price_info['ticker']} ({price_info.get('name') or 'sem nome'}): "
                f"{h['quantity']} unidades a {eur_price:.2f} {BASE_CURRENCY} = {value:.2f} {BASE_CURRENCY}"
                f", custo médio {float(h['avg_cost'] or 0):.2f} {BASE_CURRENCY}{gain_txt}"
                f" | o ativo variou: {fmt_perf(perf)}"
            )
    else:
        lines.append("(ainda não registei posições individuais na app)")
    lines.append("")

    mode_row = db.execute("SELECT value FROM settings WHERE key='ai_advice_mode'").fetchone()
    mode = json.loads(mode_row["value"]) if mode_row else "balanced"
    mode_instructions = {
        "autonomous": (
            "Tens total liberdade para te afastares das minhas percentagens-alvo se, dado o "
            "contexto de mercado atual, achares que faz mais sentido investir de outra forma -- "
            "trata as percentagens como uma referência de fundo, não uma regra a seguir."
        ),
        "balanced": (
            "Usa as minhas percentagens-alvo como guia principal, mas ajusta-as moderadamente se "
            "houver um sinal de mercado forte que o justifique -- explica sempre que te desvies."
        ),
        "user_focused": (
            "Respeita rigorosamente as minhas percentagens-alvo. A tua sugestão deve dizer-me "
            "exatamente quanto investir em cada categoria segundo essas percentagens; não proponhas "
            "desvios, só onde/o quê especificamente comprar dentro de cada uma."
        ),
    }
    lines.append(mode_instructions.get(mode, mode_instructions["balanced"]))

    notes_row = db.execute("SELECT value FROM settings WHERE key='ai_advice_notes'").fetchone()
    notes = json.loads(notes_row["value"]) if notes_row else ""
    if notes.strip():
        lines.append(f"Notas minhas adicionais a ter em conta: {notes.strip()}")

    lines.append("")
    lines.extend(market_briefing())

    def headline_lines(items: list[dict]) -> list[str]:
        out = []
        for item in items:
            when = ""
            if item.get("published"):
                when = datetime.date.fromtimestamp(item["published"]).isoformat() + ", "
            out.append(f"- [{item['query']}] {item['title']} ({when}{item['publisher']})")
        return out

    # Todas as posições, à vez: até 4 manchetes cada, 36 no total.
    mine = portfolio_news(holding_assets(db), per_symbol=4, limit=36, strict=True)
    if mine:
        lines.append("")
        lines.append(
            "Manchetes recentes sobre as minhas posições (Yahoo Finance, obtidas agora). "
            "São títulos, não artigos completos: usa-os como pistas do que está a acontecer, "
            "não afirmes detalhes que o título não diz."
        )
        lines.extend(headline_lines(mine))

    wide = portfolio_news(MARKET_BENCHMARKS, per_symbol=3, limit=12)
    seen = {i["title"] for i in mine}
    wide = [i for i in wide if i["title"] not in seen]
    if wide:
        lines.append("")
        lines.append("Manchetes sobre o mercado em geral (mesmos avisos):")
        lines.extend(headline_lines(wide))

    lines.append("")
    lines.append(
        "Com base nos dados acima, sugere concretamente onde e quanto devo investir este mês. "
        "Escreve em português de Portugal e organiza a resposta exatamente por esta ordem, em "
        "markdown:"
    )
    lines.append("")
    lines.append(
        "1. **Resumo** -- dois ou três pontos com a decisão e o motivo principal. Sem preâmbulos.\n"
        f"2. **Onde investir** -- uma tabela markdown com as colunas | Categoria | Ativo | Valor ({BASE_CURRENCY}) "
        "| Porquê |, uma linha por sugestão. Acaba com uma linha **Total** cuja soma tem de "
        "bater certo com o orçamento do mês -- se arredondares, o total tem de mostrar isso. "
        "Se te desviares das minhas percentagens-alvo, diz em que linha e porquê.\n"
        "3. **O que dizem as notícias** -- o que as manchetes acima acrescentam, agrupado por tema "
        "e não uma a uma. Liga cada tema às minhas posições concretas; se uma manchete não muda "
        "nada para mim, não a menciones.\n"
        "4. **Riscos** -- o que pode correr mal nesta sugestão, e qual o número que devo vigiar "
        "para perceber que está a correr mal.\n"
        "5. **A carteira em si** -- uma observação sobre concentração, peso por categoria ou "
        "posições que mereçam atenção independentemente deste mês."
    )
    lines.append("")
    lines.append(
        "Regras: justifica sempre com os números que te dei, nomeando-os. Se algum ativo te "
        "parecer caro ou barato, diz com base em que número dos que estão aqui. Não repitas "
        "listas de dados que já tenho -- o valor está na leitura, não no resumo. Termina com um "
        "aviso curto de que isto não é aconselhamento financeiro profissional."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- schema --

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS months (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  year INTEGER NOT NULL,
  month INTEGER NOT NULL,
  ordenado REAL NOT NULL DEFAULT 0,
  extras REAL NOT NULL DEFAULT 0,
  pct_revolut REAL NOT NULL DEFAULT 0.7,
  pct_poupanca REAL NOT NULL DEFAULT 0.8,
  split_geral REAL NOT NULL DEFAULT 0.12,
  split_cofres REAL NOT NULL DEFAULT 0.58,
  split_investimentos REAL NOT NULL DEFAULT 0.3,
  santander_anterior REAL NOT NULL DEFAULT 0,
  revolut_anterior REAL NOT NULL DEFAULT 0,
  joint_partner_contribution REAL NOT NULL DEFAULT 0,
  joint_share REAL,
  joint_me_actual REAL,
  joint_partner_actual REAL,
  no_savings INTEGER NOT NULL DEFAULT 0,
  estimated INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  closed INTEGER NOT NULL DEFAULT 0,
  historical_import INTEGER NOT NULL DEFAULT 0,
  computed_trf_revolut REAL NOT NULL DEFAULT 0,
  computed_cc_livre REAL NOT NULL DEFAULT 0,
  computed_revolut_pos_despesas REAL NOT NULL DEFAULT 0,
  computed_poupanca_total REAL NOT NULL DEFAULT 0,
  computed_livre_resto REAL NOT NULL DEFAULT 0,
  computed_despesas_santander REAL NOT NULL DEFAULT 0,
  computed_despesas_revolut REAL NOT NULL DEFAULT 0,
  computed_sem_registo REAL NOT NULL DEFAULT 0,
  UNIQUE(year, month)
);
CREATE TABLE IF NOT EXISTS expense_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  obs TEXT NOT NULL DEFAULT '',
  default_planned REAL NOT NULL DEFAULT 0,
  mutable INTEGER NOT NULL DEFAULT 0,
  account TEXT NOT NULL DEFAULT 'S',
  joint_account INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS month_expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  month_id INTEGER NOT NULL REFERENCES months(id) ON DELETE CASCADE,
  template_id INTEGER REFERENCES expense_templates(id) ON DELETE SET NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  obs TEXT NOT NULL DEFAULT '',
  planned REAL NOT NULL DEFAULT 0,
  actual REAL,
  mutable INTEGER NOT NULL DEFAULT 0,
  account TEXT NOT NULL DEFAULT 'S',
  joint_account INTEGER NOT NULL DEFAULT 0,
  closed INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  external_source TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL DEFAULT '',
  external_due_on TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  pct REAL NOT NULL DEFAULT 0,
  opening_balance REAL NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  goal_amount REAL,
  goal_date TEXT,
  rate_mode TEXT NOT NULL DEFAULT 'none',
  rate_value REAL NOT NULL DEFAULT 0,
  rate_tax REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS investments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  pct REAL NOT NULL DEFAULT 0,
  opening_balance REAL NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS balance_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  item_id INTEGER NOT NULL DEFAULT 0,
  month_id INTEGER NOT NULL REFERENCES months(id) ON DELETE CASCADE,
  expected REAL NOT NULL,
  real REAL NOT NULL,
  UNIQUE(kind, item_id, month_id)
);
CREATE TABLE IF NOT EXISTS pot_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  month_id INTEGER NOT NULL REFERENCES months(id) ON DELETE CASCADE,
  pot_id INTEGER NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
  contributed REAL NOT NULL DEFAULT 0,
  UNIQUE(month_id, pot_id)
);
CREATE TABLE IF NOT EXISTS investment_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  month_id INTEGER NOT NULL REFERENCES months(id) ON DELETE CASCADE,
  investment_id INTEGER NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
  contributed REAL NOT NULL DEFAULT 0,
  UNIQUE(month_id, investment_id)
);
CREATE TABLE IF NOT EXISTS geral_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  month_id INTEGER NOT NULL UNIQUE REFERENCES months(id) ON DELETE CASCADE,
  contributed REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS holdings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  investment_id INTEGER NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
  ticker TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  quantity REAL NOT NULL DEFAULT 0,
  avg_cost REAL NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'EUR',
  notes TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  manual_value REAL
);
"""

DEFAULT_SETTINGS = {
    "language": "en",
    "account_labels": {"S": "Account", "R": "Account 2"},
    # Esquemas opcionais, desligados num servidor novo: com tudo desligado há uma só conta, sem
    # transferências nem conta conjunta. Uma base de dados que já existia fica com ambos ligados
    # (ver init_db), para o comportamento de quem já usava a app não mudar.
    "use_transfer": False,
    "use_joint": False,
    "currency": "EUR",
    "geral_opening_balance": 0,
    "joint_account_name": "Joint account",
    "auto_balance_geral": True,
    "auto_balance_target": "geral",
    "round_pots_enabled": True,
    "use_target_accumulated_mode": True,
    "anthropic_api_key": "",
    "openai_api_key": "",
    "gemini_api_key": "",
    "deepseek_api_key": "",
    "ai_provider": "anthropic",
    "joint_account_opening_balance": 0,
    "ai_advice_mode": "balanced",
    "ai_advice_notes": "",
    "joint_partner_name": "Partner",
    # Como se divide a conta da casa: "percent" usa joint_my_share; "income" divide
    # na proporção dos ordenados (o meu do mês contra joint_partner_income).
    "joint_split_mode": "percent",
    "joint_my_share": 0.5,
    "joint_partner_income": 0,
    "joint_from_account": "S",
    "joint_model": 2,
    "last_advice": None,
    "last_savings_advice": None,
    "savings_priorities": "",
}


# Tables that make up a full data export. users/sessions are deliberately absent:
# an export is the user's financial data, not their credentials.
EXPORT_TABLES = [
    "months", "month_expenses", "expense_templates", "pots", "investments",
    "holdings", "pot_snapshots", "investment_snapshots", "geral_snapshots",
]
SECRET_SETTINGS = ("anthropic_api_key", "openai_api_key", "gemini_api_key", "deepseek_api_key")


def build_export(db: sqlite3.Connection) -> dict:
    """Every row the app owns, as plain JSON -- the data, without the secrets."""
    data: dict[str, Any] = {
        "app": "Rumo",
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "tables": {},
    }
    for table in EXPORT_TABLES:
        rows = db.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed list above
        data["tables"][table] = [dict(r) for r in rows]

    settings = {}
    for r in db.execute("SELECT key, value FROM settings").fetchall():
        if r["key"] in SECRET_SETTINGS:
            continue
        try:
            settings[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            settings[r["key"]] = r["value"]
    data["tables"]["settings"] = settings
    return data


def migrate_schema(db: sqlite3.Connection) -> None:
    """Additive-only column migrations for DBs created before a field existed."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(months)")}
    if "santander_anterior" not in existing:
        db.execute("ALTER TABLE months ADD COLUMN santander_anterior REAL NOT NULL DEFAULT 0")
    tpl_cols = {row[1] for row in db.execute("PRAGMA table_info(expense_templates)")}
    if "joint_account" not in tpl_cols:
        db.execute("ALTER TABLE expense_templates ADD COLUMN joint_account INTEGER NOT NULL DEFAULT 0")
    exp_cols = {row[1] for row in db.execute("PRAGMA table_info(month_expenses)")}
    if "joint_account" not in exp_cols:
        db.execute("ALTER TABLE month_expenses ADD COLUMN joint_account INTEGER NOT NULL DEFAULT 0")
    for col in ("external_source", "external_id", "external_due_on"):
        if col not in exp_cols:
            db.execute(f"ALTER TABLE month_expenses ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_month_expenses_external
           ON month_expenses(month_id, external_source, external_id)
           WHERE external_source <> '' AND external_id <> ''"""
    )
    month_cols = {row[1] for row in db.execute("PRAGMA table_info(months)")}
    if "joint_partner_contribution" not in month_cols:
        db.execute("ALTER TABLE months ADD COLUMN joint_partner_contribution REAL NOT NULL DEFAULT 0")
    pot_cols = {row[1] for row in db.execute("PRAGMA table_info(pots)")}
    if "goal_amount" not in pot_cols:
        db.execute("ALTER TABLE pots ADD COLUMN goal_amount REAL")
    if "goal_date" not in pot_cols:
        db.execute("ALTER TABLE pots ADD COLUMN goal_date TEXT")
    for col, ddl in (("rate_mode", "TEXT NOT NULL DEFAULT 'none'"), ("rate_value", "REAL NOT NULL DEFAULT 0"),
                     ("rate_tax", "REAL NOT NULL DEFAULT 0")):
        if col not in pot_cols:
            db.execute(f"ALTER TABLE pots ADD COLUMN {col} {ddl}")
    holdings_cols = {row[1] for row in db.execute("PRAGMA table_info(holdings)")}
    if holdings_cols and "manual_value" not in holdings_cols:
        db.execute("ALTER TABLE holdings ADD COLUMN manual_value REAL")
    for table in ("pots", "investments"):
        if "ai_locked" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN ai_locked INTEGER NOT NULL DEFAULT 0")
    pot_cols = {row[1] for row in db.execute("PRAGMA table_info(pots)")}
    if "level" not in pot_cols:
        db.execute("ALTER TABLE pots ADD COLUMN level INTEGER NOT NULL DEFAULT 0")
    if "purpose" not in pot_cols:
        db.execute("ALTER TABLE pots ADD COLUMN purpose TEXT NOT NULL DEFAULT ''")
    month_cols = {row[1] for row in db.execute("PRAGMA table_info(months)")}
    for col, spec in (
        ("joint_share", "REAL"), ("joint_me_actual", "REAL"), ("joint_partner_actual", "REAL"),
        ("joint_extra", "REAL NOT NULL DEFAULT 0"),
        ("no_savings", "INTEGER NOT NULL DEFAULT 0"), ("estimated", "INTEGER NOT NULL DEFAULT 0"),
        ("computed_sem_registo", "REAL NOT NULL DEFAULT 0"),
        ("fatia_jobs", "TEXT NOT NULL DEFAULT '[]'"),
        ("moves_done", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in month_cols:
            db.execute(f"ALTER TABLE months ADD COLUMN {col} {spec}")


def set_move_done(db: sqlite3.Connection, month_id: int, key: str, on: bool) -> dict:
    """Marca (ou desmarca) um passo dos Movimentos como feito. Fica na DB, por mês,
    para o Rumo e o Bolso verem as mesmas marcas: {chave: hora unix em que ficou feito}."""
    if not re.fullmatch(r"[a-zA-Z]+(-[A-Za-z0-9]+)?", key or "") or len(key) > 40:
        raise ValueError("passo inválido")
    row = db.execute("SELECT moves_done FROM months WHERE id=?", (month_id,)).fetchone()
    if row is None:
        raise LookupError("mês não encontrado")
    done = json.loads(row["moves_done"] or "{}")
    if on:
        done.setdefault(key, int(time.time()))
    else:
        done.pop(key, None)
    db.execute("UPDATE months SET moves_done=? WHERE id=?", (json.dumps(done), month_id))
    db.commit()
    return done


def migrate_joint_model(db: sqlite3.Connection) -> None:
    """Conta conjunta, modelo 2 (2026-10-16).

    Até aqui o 🏠 numa despesa queria dizer «isto é o que eu transfiro para a
    conjunta» -- a minha metade. Agora quer dizer «isto é pago pela conjunta», e o
    valor é o da casa inteira: a parte de cada um sai da divisão. As linhas antigas
    eram a minha metade de uma conta a meias, por isso passam ao dobro e a minha
    parte fica exactamente igual ao que era. O que o(a) parceiro(a) pôs fica como
    valor real; os meses em que ficou a zero passam a «previsto»."""
    if db.execute("SELECT 1 FROM settings WHERE key='joint_model'").fetchone():
        return
    db.execute("UPDATE month_expenses SET planned = planned * 2, actual = actual * 2 WHERE joint_account = 1")
    db.execute("UPDATE expense_templates SET default_planned = default_planned * 2 WHERE joint_account = 1")
    db.execute(
        "UPDATE months SET joint_partner_actual = joint_partner_contribution "
        "WHERE joint_partner_contribution > 0 AND joint_partner_actual IS NULL"
    )


def init_db(db_path: Path) -> None:
    fresh = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        migrate_schema(db)
        if not fresh:
            migrate_joint_model(db)
            for k in ("use_transfer", "use_joint"):
                db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, 'true')", (k,))
            # Contas que já tinham nomes escolhidos mantêm-nos; só uma base antiga sem nomes recebe os de antes.
            db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('account_labels', ?)",
                       (json.dumps({"S": "Santander", "R": "Revolut"}),))
            # Uma base que já existia estava em português: mantém o idioma e os nomes de antes.
            for k, v in (("language", "pt-PT"), ("joint_account_name", "Conta Conjunta"), ("joint_partner_name", "Parceiro(a)")):
                db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))
        db.commit()
        for k, v in DEFAULT_SETTINGS.items():
            db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (k, json.dumps(v, ensure_ascii=False)),
            )
        db.commit()
    if fresh:
        seed_path = ROOT / "seed.json"
        if seed_path.exists():
            try:
                import_seed(db_path, json.loads(seed_path.read_text(encoding="utf-8")))
            except Exception as exc:  # pragma: no cover - best effort seed
                print(f"aviso: falha ao importar seed.json: {exc}", flush=True)


def import_seed(db_path: Path, seed: dict) -> None:
    """Import the historical model extracted from the original spreadsheet."""
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")

        pot_ids = {}
        for i, p in enumerate(seed.get("pots", [])):
            cur = db.execute(
                "INSERT INTO pots(name, pct, sort_order) VALUES (?, ?, ?)",
                (p["name"], p["pct"], i),
            )
            pot_ids[p["name"]] = cur.lastrowid

        inv_ids = {}
        for i, p in enumerate(seed.get("investimentos", [])):
            cur = db.execute(
                "INSERT INTO investments(name, pct, sort_order) VALUES (?, ?, ?)",
                (p["name"], p["pct"], i),
            )
            inv_ids[p["name"]] = cur.lastrowid

        split = seed.get("split", {"geral": 0.12, "cofres": 0.58, "investimentos": 0.3})

        # Items that feed the "transfer to the joint account" summary by default;
        # editable per-item afterwards in Definições / no próprio mês.
        joint_default_names = {"casa", "combustível", "combustivel", "supermercado"}

        templates_by_key = {}
        for i, item in enumerate(seed.get("expense_items_seed", [])):
            is_joint = item["name"].strip().lower() in joint_default_names
            cur = db.execute(
                """INSERT INTO expense_templates
                   (category, name, obs, default_planned, mutable, account, joint_account, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.get("category") or "Outros",
                    item["name"],
                    item.get("obs") or "",
                    item.get("planned", 0),
                    1 if item.get("mutable") else 0,
                    item.get("account") or "S",
                    1 if is_joint else 0,
                    i,
                ),
            )
            templates_by_key[(item.get("category"), item["name"])] = cur.lastrowid

        # "Casa" (house/rent) never existed as an expense item in the source sheet — it
        # was only a savings pot name — but the user wants it counted in the joint-account
        # transfer by default, so seed a starting item for it.
        if ("CASA", "Casa") not in templates_by_key:
            cur = db.execute(
                """INSERT INTO expense_templates
                   (category, name, obs, default_planned, mutable, account, joint_account, sort_order)
                   VALUES ('CASA', 'Casa', '', 0, 0, 'S', 1, ?)""",
                (len(seed.get("expense_items_seed", [])),),
            )
            templates_by_key[("CASA", "Casa")] = cur.lastrowid

        months = seed.get("months", [])
        year = 2026
        month_ids = {}
        for idx, m in enumerate(months):
            month_num = idx + 1
            cur = db.execute(
                """INSERT INTO months
                   (year, month, ordenado, extras, pct_revolut, pct_poupanca,
                    split_geral, split_cofres, split_investimentos, revolut_anterior,
                    closed, computed_trf_revolut, computed_cc_livre,
                    computed_revolut_pos_despesas, computed_poupanca_total, computed_livre_resto,
                    computed_despesas_santander, computed_despesas_revolut)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    year, month_num, m["ordenado"], m["extras"], m["pct_revolut"],
                    m["pct_poupanca"], split["geral"], split["cofres"], split["investimentos"],
                    m["revolut_anterior"],
                    m["trf_revolut"], m["cc_livre"], m["revolut_pos_despesas"],
                    m["poupanca_contribuicao_total"], m["livre_resto"],
                    m["despesas_santander"], m["despesas_revolut"],
                ),
            )
            month_ids[m["month"]] = cur.lastrowid
            db.execute("UPDATE months SET historical_import=1 WHERE id=?", (cur.lastrowid,))

        # December's expense catalogue is used verbatim as December's actual line items;
        # earlier months only have category totals in the source, so we synthesize a single
        # rollup line per category (still gives correct planned totals for the waterfall/history).
        dez_id = month_ids.get("DEZ")
        if dez_id:
            for i, item in enumerate(seed.get("expense_items_seed", [])):
                db.execute(
                    """INSERT INTO month_expenses
                       (month_id, template_id, category, name, obs, planned, mutable, account, sort_order)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dez_id,
                        templates_by_key.get((item.get("category"), item["name"])),
                        item.get("category") or "Outros",
                        item["name"],
                        item.get("obs") or "",
                        item.get("planned", 0),
                        1 if item.get("mutable") else 0,
                        item.get("account") or "S",
                        i,
                    ),
                )

        cat_totals = {
            "JAN": None,
        }
        for idx, m in enumerate(months):
            mid = month_ids[m["month"]]
            if m["month"] == "DEZ":
                continue
            despesa_s = m["despesas_santander"]
            despesa_r = m["despesas_revolut"]
            if despesa_s:
                db.execute(
                    """INSERT INTO month_expenses
                       (month_id, category, name, obs, planned, account, sort_order)
                       VALUES (?, 'Histórico', 'Despesas (total importado)', 'Detalhe não disponível no ficheiro original', ?, 'S', 0)""",
                    (mid, despesa_s),
                )
            if despesa_r:
                db.execute(
                    """INSERT INTO month_expenses
                       (month_id, category, name, obs, planned, account, sort_order)
                       VALUES (?, 'Histórico', 'Despesas (total importado)', 'Detalhe não disponível no ficheiro original', ?, 'R', 1)""",
                    (mid, despesa_r),
                )

        for p in seed.get("pots", []):
            pid = pot_ids[p["name"]]
            for row in p.get("monthly", []):
                mid = month_ids.get(row["month"])
                if mid:
                    db.execute(
                        "INSERT INTO pot_snapshots(month_id, pot_id, contributed) VALUES (?, ?, ?)",
                        (mid, pid, row["contributed"]),
                    )
        for p in seed.get("investimentos", []):
            iid = inv_ids[p["name"]]
            for row in p.get("monthly", []):
                mid = month_ids.get(row["month"])
                if mid:
                    db.execute(
                        "INSERT INTO investment_snapshots(month_id, investment_id, contributed) VALUES (?, ?, ?)",
                        (mid, iid, row["contributed"]),
                    )
        for m in months:
            mid = month_ids[m["month"]]
            db.execute(
                "INSERT INTO geral_snapshots(month_id, contributed) VALUES (?, ?)",
                (mid, m["poupanca_geral"]),
            )

        # Jul-Dez of the source file were never actually lived through — they're stale
        # placeholder rows copied forward from last year's 2.2 template, not real entries.
        # Jan-Jun are confirmed accurate. Drop the stale six entirely (cascades to their
        # expenses/snapshots) rather than keep fabricated "future" months in the timeline.
        stale_ids = [month_ids[mon] for mon in MONTH_NAMES[6:] if mon in month_ids]
        if stale_ids:
            db.executemany("DELETE FROM months WHERE id=?", [(i,) for i in stale_ids])

        db.commit()
    # Kept (Jan-Jun) historical months retain the exact figures derived from the original
    # spreadsheet above (computed_* columns set directly) and are marked closed=1 so the
    # calc engine below never recomputes over them and silently rewrites real history.
    # historical_import=1 also excludes them from the live cumulative accumulation (see
    # month_summary): the user was never disciplined about recording month-to-month
    # carry-over, so that running total is reset — opening_balance on pots/investments/
    # settings is the fresh, user-set baseline going forward.


# ------------------------------------------------------------- calc engine --

def setting(db: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        value = json.loads(row["value"])
    except (ValueError, TypeError):
        return default
    return default if value is None else value


def joint_share_for(db: sqlite3.Connection, month: sqlite3.Row) -> float:
    """A minha fatia da conta da casa neste mês (0..1). Um valor guardado no mês
    manda; senão vale a regra das Definições -- percentagem fixa, ou a proporção
    entre o meu ordenado do mês e o rendimento do(a) parceiro(a)."""
    if month["joint_share"] is not None:
        return max(0.0, min(1.0, float(month["joint_share"])))
    if setting(db, "joint_split_mode", "percent") == "income":
        partner = float(setting(db, "joint_partner_income", 0) or 0)
        mine = month_income(month)
        if partner > 0 and mine > 0:
            return mine / (mine + partner)
    return max(0.0, min(1.0, float(setting(db, "joint_my_share", 0.5))))


def joint_split(db: sqlite3.Connection, month: sqlite3.Row, total: float) -> dict:
    """Quanto a casa gasta, quanto cabe a cada um, e quanto cada um pôs mesmo.
    Sem valor real escrito, assume-se que cada um pôs a sua parte."""
    total = round(float(total or 0), 2)
    share = joint_share_for(db, month)
    # Além das despesas, cada um pode pôr o mesmo valor a mais (fica a crescer na
    # conta): com 150, eu ponho a minha parte + 150 e o(a) parceiro(a) a dele + 150.
    try:
        extra = max(0.0, float(month["joint_extra"] or 0))
    except (IndexError, KeyError):
        extra = 0.0
    mine = round(total * share, 2)
    me_pred = round(mine + extra, 2)
    partner_pred = round(total - mine + extra, 2)
    me = me_pred if month["joint_me_actual"] is None else float(month["joint_me_actual"])
    partner = partner_pred if month["joint_partner_actual"] is None else float(month["joint_partner_actual"])
    return {"total": total, "share": share, "extra": extra, "me_pred": me_pred, "partner_pred": partner_pred,
            "me": round(me, 2), "partner": round(partner, 2), "net": round(me + partner - total, 2)}


# Uma secção de rendimento (LUCROS e afins) é dinheiro que entra: soma à conta da
# linha em vez de sair dela, e fica fora das despesas e da conta da casa. Antes
# contava como despesa -- 50 € em «OLX» tiravam 50 € ao mês.
INCOME_CATEGORIES = ("LUCROS", "LUCRO", "RENDIMENTOS", "RECEITAS", "ENTRADAS", "INCOME", "PROFITS", "EARNINGS")
NOT_INCOME_SQL = "upper(trim(category)) NOT IN ('LUCROS', 'LUCRO', 'RENDIMENTOS', 'RECEITAS', 'ENTRADAS', 'INCOME', 'PROFITS', 'EARNINGS')"


def is_income_line(e: Any) -> bool:
    return str(e["category"] or "").strip().upper() in INCOME_CATEGORIES


def scheme(db: sqlite3.Connection) -> tuple[bool, bool]:
    """(transferência entre duas contas ligada, conta conjunta ligada)."""
    return bool(setting(db, "use_transfer", False)), bool(setting(db, "use_joint", False))


def effective_lines(db: sqlite3.Connection, rows: list) -> list:
    """As linhas como o esquema escolhido as vê. Sem transferências, tudo sai da mesma conta; sem conta
    conjunta, uma linha marcada como da casa conta como despesa normal."""
    transfer_on, joint_on = scheme(db)
    if transfer_on and joint_on:
        return rows
    out = []
    for e in rows:
        d = dict(e)
        if not transfer_on:
            d["account"] = "S"
        if not joint_on:
            d["joint_account"] = 0
        out.append(d)
    return out


def joint_net_sum(db: sqlite3.Connection, year: int, month: int, exclude_id: int | None = None) -> float:
    """O que entrou menos o que saiu da conjunta, somado mês a mês até (inclusive)
    year/month. Os meses importados do Excel ficam de fora, como no resto."""
    if not scheme(db)[1]:
        return 0.0
    totals = {r["month_id"]: r["total"] for r in db.execute(
        f"SELECT month_id, SUM(planned) AS total FROM month_expenses WHERE joint_account=1 AND {NOT_INCOME_SQL} GROUP BY month_id"
    ).fetchall()}
    rows = db.execute(
        """SELECT * FROM months WHERE historical_import = 0
             AND (year < ? OR (year = ? AND month <= ?))""",
        (year, year, month),
    ).fetchall()
    return round(sum(joint_split(db, r, totals.get(r["id"], 0))["net"] for r in rows if r["id"] != exclude_id), 2)


def recompute_month(db: sqlite3.Connection, month_id: int, persist: bool, force: bool = False) -> dict:
    month = db.execute("SELECT * FROM months WHERE id = ?", (month_id,)).fetchone()
    if month is None:
        raise ValueError("mês não encontrado")
    if month["closed"] and not force:
        return month_summary(db, month_id)
    if not month["closed"] and not month["historical_import"]:
        # Mês aberto: os trabalhos do Fatia contam como estão agora; ao fechar ficam.
        jobs = fatia_jobs_live(month["year"], month["month"])
        if jobs != month_fatia_jobs(month):
            db.execute("UPDATE months SET fatia_jobs=? WHERE id=?", (json.dumps(jobs, ensure_ascii=False), month_id))
            month = db.execute("SELECT * FROM months WHERE id = ?", (month_id,)).fetchone()

    expenses = effective_lines(db, db.execute(
        "SELECT * FROM month_expenses WHERE month_id = ? ORDER BY sort_order, id", (month_id,)
    ).fetchall())

    gains = [e for e in expenses if is_income_line(e)]
    expenses = [e for e in expenses if not is_income_line(e)]
    lucros_revolut = sum(e["planned"] for e in gains if e["account"] == "R")
    income_santander = month_income(month) + month["santander_anterior"] + sum(e["planned"] for e in gains) - lucros_revolut
    # As despesas da casa são pagas pela conjunta e não saem das minhas contas;
    # das minhas sai só a minha transferência para lá.
    joint = joint_split(db, month, sum(e["planned"] for e in expenses if e["joint_account"]))
    joint_from = "R" if setting(db, "joint_from_account", "S") == "R" else "S"
    despesas_santander = sum(e["planned"] for e in expenses if e["account"] == "S" and not e["joint_account"])
    despesas_revolut = sum(e["planned"] for e in expenses if e["account"] == "R" and not e["joint_account"])
    if joint_from == "S":
        despesas_santander += joint["me"]
    else:
        despesas_revolut += joint["me"]

    cc_pos_despesas = income_santander - despesas_santander
    # Sem transferências há uma só conta: tudo o que sobra segue para o «fundo» da poupança, como se
    # a percentagem fosse 100%, e a segunda conta nunca aparece.
    trf_revolut = cc_pos_despesas * (month["pct_revolut"] if scheme(db)[0] else 1.0)
    cc_livre = cc_pos_despesas - trf_revolut

    revolut_disponivel = month["revolut_anterior"] + trf_revolut + lucros_revolut
    revolut_pos_despesas = revolut_disponivel - despesas_revolut
    poupanca_total = revolut_pos_despesas * month["pct_poupanca"]
    livre_resto = revolut_pos_despesas - poupanca_total
    sem_registo = 0.0
    if month["no_savings"]:
        # «Não consegui poupar nada»: nada para cofres, investimentos ou Geral, e
        # o que sobrava dá-se por gasto -- não passa como saldo para o mês seguinte.
        # Um buraco (sobra negativa) continua a contar como buraco.
        sem_registo = max(0.0, cc_livre) + max(0.0, revolut_pos_despesas)
        cc_livre = min(0.0, cc_livre)
        livre_resto = min(0.0, revolut_pos_despesas)
        poupanca_total = 0.0

    geral_amt = poupanca_total * month["split_geral"]
    cofres_amt = poupanca_total * month["split_cofres"]
    invest_amt = poupanca_total * month["split_investimentos"]

    pots = db.execute("SELECT * FROM pots WHERE active = 1 ORDER BY sort_order, id").fetchall()
    invs = db.execute("SELECT * FROM investments WHERE active = 1 ORDER BY sort_order, id").fetchall()

    round_row = db.execute("SELECT value FROM settings WHERE key='round_pots_enabled'").fetchone()
    round_enabled = json.loads(round_row["value"]) if round_row else True

    def floor_to_50c(amount: float) -> tuple[float, float]:
        """Round down to the nearest €0.50; returns (rounded, remainder)."""
        rounded = math.floor(amount / 0.5) * 0.5
        return rounded, amount - rounded

    pot_contribs = {}
    inv_contribs = {}
    rounding_remainder = 0.0
    for p in pots:
        raw = cofres_amt * p["pct"]
        if round_enabled:
            rounded, remainder = floor_to_50c(raw)
            pot_contribs[p["id"]] = rounded
            rounding_remainder += remainder
        else:
            pot_contribs[p["id"]] = raw
    for i in invs:
        raw = invest_amt * i["pct"]
        if round_enabled:
            rounded, remainder = floor_to_50c(raw)
            inv_contribs[i["id"]] = rounded
            rounding_remainder += remainder
        else:
            inv_contribs[i["id"]] = raw
    geral_amt += rounding_remainder
    # O que as % não distribuem (somam menos de 100%, por exemplo depois de cada
    # uma ser acertada para um valor redondo) também vai para o Geral -- antes
    # ficava por conta nenhuma.
    geral_amt += cofres_amt * max(0.0, 1 - sum(float(p["pct"] or 0) for p in pots))
    geral_amt += invest_amt * max(0.0, 1 - sum(float(i["pct"] or 0) for i in invs))

    if persist:
        db.execute(
            """UPDATE months SET
                 computed_trf_revolut=?, computed_cc_livre=?, computed_revolut_pos_despesas=?,
                 computed_poupanca_total=?, computed_livre_resto=?,
                 computed_despesas_santander=?, computed_despesas_revolut=?, computed_sem_registo=?
               WHERE id=?""",
            (
                trf_revolut, cc_livre, revolut_pos_despesas, poupanca_total, livre_resto,
                despesas_santander, despesas_revolut, sem_registo, month_id,
            ),
        )
        db.execute(
            "INSERT INTO geral_snapshots(month_id, contributed) VALUES (?, ?) "
            "ON CONFLICT(month_id) DO UPDATE SET contributed=excluded.contributed",
            (month_id, geral_amt),
        )
        for pid, amt in pot_contribs.items():
            db.execute(
                "INSERT INTO pot_snapshots(month_id, pot_id, contributed) VALUES (?, ?, ?) "
                "ON CONFLICT(month_id, pot_id) DO UPDATE SET contributed=excluded.contributed",
                (month_id, pid, amt),
            )
        for iid, amt in inv_contribs.items():
            db.execute(
                "INSERT INTO investment_snapshots(month_id, investment_id, contributed) VALUES (?, ?, ?) "
                "ON CONFLICT(month_id, investment_id) DO UPDATE SET contributed=excluded.contributed",
                (month_id, iid, amt),
            )
        db.commit()

    return month_summary(db, month_id)


def latest_open_month_id(db: sqlite3.Connection) -> int | None:
    row = db.execute("SELECT id FROM months WHERE closed=0 ORDER BY year DESC, month DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def contributions_sum(db: sqlite3.Connection, snapshot_table: str, id_col: str, entity_id: int, exclude_month_id: int | None = None) -> float:
    """Sum of an entity's non-historical monthly contributions (excludes opening_balance)."""
    query = f"""SELECT COALESCE(SUM(s.contributed), 0) AS total
                FROM {snapshot_table} s JOIN months mo ON mo.id = s.month_id
                WHERE mo.historical_import = 0 AND s.{id_col} = ?"""
    params: list = [entity_id]
    if exclude_month_id is not None:
        query += " AND mo.id != ?"
        params.append(exclude_month_id)
    return db.execute(query, params).fetchone()["total"]


def log_check(db: sqlite3.Connection, kind: str, item_id: int, expected: float, real: float) -> None:
    """Regista o valor real que o utilizador escreveu para o fim do mês anterior ao mês aberto, ao lado do que a
    app esperava (saldo de abertura + entradas). A diferença é o que rendeu (juros, ganhos) ou se perdeu. O
    esperado fica o do primeiro registo do mês: corrigir o real duas vezes não apaga a diferença."""
    mid = latest_open_month_id(db)
    if mid is None:
        return
    db.execute(
        """INSERT INTO balance_checks(kind, item_id, month_id, expected, real) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(kind, item_id, month_id) DO UPDATE SET real = excluded.real""",
        (kind, item_id, mid, round(expected, 2), round(real, 2)),
    )


def expected_balance(db: sqlite3.Connection, table: str, snapshot_table: str, id_col: str, entity_id: int) -> float:
    row = db.execute(f"SELECT opening_balance FROM {table} WHERE id=?", (entity_id,)).fetchone()
    return (row["opening_balance"] if row else 0.0) + contributions_sum(
        db, snapshot_table, id_col, entity_id, exclude_month_id=latest_open_month_id(db))


def solve_opening_balance(db: sqlite3.Connection, snapshot_table: str, id_col: str, entity_id: int, target_previous_balance: float) -> float:
    """The user types the REAL balance as of the end of last month (before this month's
    still-accruing contribution). Back-solve opening_balance so that
    opening_balance + (all past months' contributions) == target_previous_balance,
    leaving the current open month's contribution to add on top as usual."""
    current_month_id = latest_open_month_id(db)
    prior_contrib = contributions_sum(db, snapshot_table, id_col, entity_id, exclude_month_id=current_month_id)
    return target_previous_balance - prior_contrib


def month_summary(db: sqlite3.Connection, month_id: int) -> dict:
    m = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
    if m is None:
        raise ValueError("mês não encontrado")
    expenses = effective_lines(db, db.execute(
        "SELECT * FROM month_expenses WHERE month_id=? ORDER BY sort_order, id", (month_id,)
    ).fetchall())

    # Historical (imported) months don't count toward the live running total: the user
    # never reliably tracked month-to-month carry-over, so that accumulation is reset.
    # opening_balance (per pot/investment, plus the geral setting) is the fresh baseline;
    # only non-historical months' contributions accumulate on top of it. This-month's own
    # contribution is still shown even for a historical month (it's informational), it's
    # just excluded from what feeds forward into later months' totals.
    def cumulative(table: str, id_col: str) -> dict:
        rows = db.execute(
            f"""
            SELECT s.{id_col} AS eid, s.contributed, mo.year, mo.month, mo.historical_import
            FROM {table} s JOIN months mo ON mo.id = s.month_id
            WHERE mo.year < ? OR (mo.year = ? AND mo.month <= ?)
            ORDER BY mo.year, mo.month
            """,
            (m["year"], m["year"], m["month"]),
        ).fetchall()
        totals: dict[int, float] = {}
        this_month: dict[int, float] = {}
        for r in rows:
            if not r["historical_import"]:
                totals[r["eid"]] = totals.get(r["eid"], 0.0) + r["contributed"]
            if r["year"] == m["year"] and r["month"] == m["month"]:
                this_month[r["eid"]] = r["contributed"]
        return totals, this_month

    pot_totals, pot_this = cumulative("pot_snapshots", "pot_id")
    inv_totals, inv_this = cumulative("investment_snapshots", "investment_id")
    geral_row = db.execute(
        """SELECT COALESCE(SUM(g.contributed), 0) AS total
           FROM geral_snapshots g JOIN months mo ON mo.id = g.month_id
           WHERE mo.historical_import = 0
             AND (mo.year < ? OR (mo.year = ? AND mo.month <= ?))""",
        (m["year"], m["year"], m["month"]),
    ).fetchone()
    geral_this = db.execute(
        "SELECT contributed FROM geral_snapshots WHERE month_id=?", (month_id,)
    ).fetchone()
    setting_row = db.execute("SELECT value FROM settings WHERE key='geral_opening_balance'").fetchone()
    geral_opening = json.loads(setting_row["value"]) if setting_row else 0

    pots = db.execute("SELECT * FROM pots ORDER BY sort_order, id").fetchall()
    invs = db.execute("SELECT * FROM investments ORDER BY sort_order, id").fetchall()
    pace = pot_pace(db)

    geral_acumulado = geral_opening + geral_row["total"]
    pot_acc = {p["id"]: p["opening_balance"] + pot_totals.get(p["id"], 0.0) for p in pots}
    inv_acc = {i["id"]: i["opening_balance"] + inv_totals.get(i["id"], 0.0) for i in invs}
    poupanca_acumulada = geral_acumulado + sum(pot_acc.values()) + sum(inv_acc.values())
    joint = joint_split(db, m, sum(e["planned"] for e in expenses if e["joint_account"] and not is_income_line(e)))
    joint_opening = float(setting(db, "joint_account_opening_balance", 0) or 0)
    joint_account_acumulado = round(joint_opening + joint_net_sum(db, m["year"], m["month"]), 2)
    # O mês histórico não entra na soma, por isso o saldo à entrada é o mesmo.
    joint_start = joint_account_acumulado - (0 if m["historical_import"] else joint["net"])
    # O mínimo a pôr este mês é o que falta para as despesas da casa depois de
    # contar com o que já lá está; o previsto é a parte inteira de cada um.
    need = max(0.0, joint["total"] - max(0.0, joint_start))
    personal_total = sum(e["planned"] for e in expenses if not e["joint_account"] and not is_income_line(e))

    return {
        "id": m["id"],
        "year": m["year"],
        "month": m["month"],
        "month_name": MONTH_NAMES[m["month"] - 1],
        "ordenado": m["ordenado"],
        "extras": m["extras"],
        "pct_revolut": m["pct_revolut"],
        "pct_poupanca": m["pct_poupanca"],
        "split_geral": m["split_geral"],
        "split_cofres": m["split_cofres"],
        "split_investimentos": m["split_investimentos"],
        "revolut_anterior": m["revolut_anterior"],
        "santander_anterior": m["santander_anterior"],
        "fatia_jobs": month_fatia_jobs(m),
        "fatia_income": round(sum(float(j.get("total") or 0) for j in month_fatia_jobs(m)), 2),
        "notes": m["notes"],
        "moves_done": json.loads(m["moves_done"] or "{}"),
        "closed": bool(m["closed"]),
        "historical_import": bool(m["historical_import"]),
        "despesas_santander": m["computed_despesas_santander"],
        "despesas_revolut": m["computed_despesas_revolut"],
        "trf_revolut": m["computed_trf_revolut"],
        "cc_livre": m["computed_cc_livre"],
        "revolut_pos_despesas": m["computed_revolut_pos_despesas"],
        "poupanca_total": m["computed_poupanca_total"],
        "livre_resto": m["computed_livre_resto"],
        # Da conjunta só conta a minha parte: o resto do saldo é do(a) parceiro(a).
        "capital": poupanca_acumulada + m["computed_cc_livre"] + m["computed_livre_resto"] + joint_account_acumulado * joint["share"],
        "poupanca_acumulada": poupanca_acumulada,
        "geral_this_month": geral_this["contributed"] if geral_this else 0,
        "geral_acumulado": geral_acumulado,
        "geral_opening_balance": geral_opening,
        "no_savings": bool(m["no_savings"]),
        "estimated": bool(m["estimated"]),
        "sem_registo": m["computed_sem_registo"],
        "despesas_pessoais": personal_total,
        "joint_expenses_total": joint["total"],
        "joint_share": joint["share"],
        "joint_share_custom": m["joint_share"] is not None,
        "joint_extra": joint["extra"],
        "joint_me_predicted": joint["me_pred"],
        "joint_partner_predicted": joint["partner_pred"],
        "joint_me_actual": m["joint_me_actual"],
        "joint_partner_actual": m["joint_partner_actual"],
        "joint_me_min": round(need * joint["share"], 2),
        "joint_partner_min": round(need - need * joint["share"], 2),
        "joint_balance_start": round(joint_start, 2),
        # Nomes antigos, com o significado novo: o que eu e o(a) parceiro(a) pomos.
        "joint_transfer_total": joint["me"],
        "joint_partner_contribution": joint["partner"],
        "joint_total_this_month": round(joint["me"] + joint["partner"], 2),
        "joint_account_acumulado": joint_account_acumulado,
        "joint_account_opening_balance": joint_opening,
        "checks": {f"{r['kind']}:{r['item_id']}": {"expected": r["expected"], "real": r["real"]}
                   for r in db.execute("SELECT kind, item_id, expected, real FROM balance_checks WHERE month_id=?", (m["id"],))},
        "expenses": [
            dict(e) | {"mutable": bool(e["mutable"]), "closed": bool(e["closed"]), "joint_account": bool(e["joint_account"])}
            for e in expenses
        ],
        "pots": [
            _pot_payload(dict(p), pot_this.get(p["id"], 0.0),
                         pot_acc.get(p["id"], p["opening_balance"]), pace.get(p["id"]))
            for p in pots
        ],
        "investimentos": [
            dict(i) | {
                "active": bool(i["active"]),
                "contributed_this_month": inv_this.get(i["id"], 0.0),
                "accumulated": inv_acc.get(i["id"], i["opening_balance"]),
            }
            for i in invs
        ],
    }


# --------------------------------------------------- meses sem contas feitas --
# Quem não fez as contas de um mês ainda quer que o mês exista e bata certo com
# os seguintes. Estas funções enchem-no com as despesas do mês anterior ou com a
# média do último ano, e fecham-no marcado como «estimado».
LINE_COLS = ("template_id", "category", "name", "obs", "planned", "mutable", "account", "joint_account", "sort_order")


def own_lines(db: sqlite3.Connection, month_id: int) -> list[sqlite3.Row]:
    """As linhas escritas no Rumo: sem as do Giro e sem os totais do Excel."""
    return db.execute(
        """SELECT * FROM month_expenses WHERE month_id=? AND external_source=''
             AND category <> 'Histórico' ORDER BY sort_order, id""",
        (month_id,),
    ).fetchall()


def months_before(db: sqlite3.Connection, month: sqlite3.Row, limit: int) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT * FROM months WHERE year < ? OR (year = ? AND month < ?)
           ORDER BY year DESC, month DESC LIMIT ?""",
        (month["year"], month["year"], month["month"], limit),
    ).fetchall()


def lines_from_previous(db: sqlite3.Connection, month: sqlite3.Row) -> list[dict] | None:
    for prev in months_before(db, month, 24):
        lines = own_lines(db, prev["id"])
        if lines and not db.execute(
                "SELECT 1 FROM month_expenses WHERE month_id=? AND category='Histórico'", (prev["id"],)).fetchone():
            return [{k: r[k] for k in LINE_COLS} for r in lines]
    return None


def lines_from_average(db: sqlite3.Connection, month: sqlite3.Row) -> list[dict] | None:
    """Média por linha nos meses do último ano que têm despesas detalhadas. Uma
    linha que falte num mês conta como zero nesse mês -- é a média do que se gastou,
    não do que se gastou quando houve."""
    # Os meses vindos do Excel só têm totais por conta (categoria «Histórico») e
    # meia dúzia de linhas a zero: entrariam na média como meses sem gastos.
    sources = [m for m in months_before(db, month, 12) if own_lines(db, m["id"]) and not db.execute(
        "SELECT 1 FROM month_expenses WHERE month_id=? AND category='Histórico'", (m["id"],)).fetchone()]
    if not sources:
        return None
    sums: dict[Any, float] = {}
    latest: dict[Any, dict] = {}
    for src in reversed(sources):  # do mais antigo para o mais recente: fica a forma mais recente
        for r in own_lines(db, src["id"]):
            key = r["template_id"] or (r["category"], r["name"])
            sums[key] = sums.get(key, 0.0) + float(r["planned"] or 0)
            latest[key] = {k: r[k] for k in LINE_COLS}
    newest = {r["template_id"] or (r["category"], r["name"]) for r in own_lines(db, sources[0]["id"])}
    out = []
    for key, line in latest.items():
        avg = round(sums[key] / len(sources), 2)
        if avg > 0 or key in newest:
            out.append(line | {"planned": avg})
    out.sort(key=lambda l: (l["sort_order"], l["category"], l["name"]))
    return out


def replace_own_lines(db: sqlite3.Connection, month_id: int, lines: list[dict]) -> None:
    db.execute("DELETE FROM month_expenses WHERE month_id=? AND external_source=''", (month_id,))
    db.executemany(
        f"INSERT INTO month_expenses (month_id, {', '.join(LINE_COLS)}) VALUES (?{', ?' * len(LINE_COLS)})",
        [(month_id, *(line[k] for k in LINE_COLS)) for line in lines],
    )


def freeze_joint(db: sqlite3.Connection, month_id: int) -> None:
    """Ao fechar, o que estava «previsto» passa a valor escrito. Mudar a regra da
    divisão depois não pode reescrever o saldo da conjunta em meses já fechados."""
    m = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
    total = db.execute(
        f"SELECT COALESCE(SUM(planned), 0) FROM month_expenses WHERE month_id=? AND joint_account=1 AND {NOT_INCOME_SQL}", (month_id,)
    ).fetchone()[0]
    j = joint_split(db, m, total)
    db.execute(
        """UPDATE months SET joint_share=?, joint_me_actual=COALESCE(joint_me_actual, ?),
                             joint_partner_actual=COALESCE(joint_partner_actual, ?) WHERE id=?""",
        (j["share"], j["me"], j["partner"], month_id),
    )


def close_month(db: sqlite3.Connection, month_id: int, estimated: bool = False) -> None:
    recompute_month(db, month_id, persist=True, force=True)
    freeze_joint(db, month_id)
    db.execute("UPDATE months SET closed=1, estimated=MAX(estimated, ?) WHERE id=?", (1 if estimated else 0, month_id))
    db.commit()


def carry_forward(db: sqlite3.Connection, month_id: int, old_cc: float, old_rest: float) -> None:
    """Passa os novos saldos para o mês seguinte, mas só se lá ainda estiverem os
    valores automáticos -- um saldo escrito à mão é real e não se lhe toca."""
    m = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
    nxt = db.execute(
        """SELECT * FROM months WHERE year > ? OR (year = ? AND month > ?)
           ORDER BY year, month LIMIT 1""",
        (m["year"], m["year"], m["month"]),
    ).fetchone()
    if not nxt or nxt["closed"]:
        return
    sets = {}
    if abs(nxt["santander_anterior"] - old_cc) < 0.01:
        sets["santander_anterior"] = m["computed_cc_livre"]
    if abs(nxt["revolut_anterior"] - old_rest) < 0.01:
        sets["revolut_anterior"] = m["computed_livre_resto"]
    if sets:
        db.execute(f"UPDATE months SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), nxt["id"]))
        db.commit()
        recompute_month(db, nxt["id"], persist=True)


def estimate_month(db: sqlite3.Connection, month_id: int, expenses: str = "keep",
                   no_savings: bool | None = None, close: bool = False) -> dict:
    m = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
    if m is None:
        raise ValueError("mês não encontrado")
    old_cc, old_rest = m["computed_cc_livre"], m["computed_livre_resto"]
    note = None
    if expenses in {"prev", "avg"}:
        lines = lines_from_previous(db, m) if expenses == "prev" else lines_from_average(db, m)
        if lines is None:
            raise ValueError("Não há meses anteriores com despesas para copiar")
        replace_own_lines(db, month_id, lines)
        note = "despesas iguais às do mês anterior" if expenses == "prev" else "despesas pela média do último ano"
    if no_savings is not None:
        db.execute("UPDATE months SET no_savings=? WHERE id=?", (1 if no_savings else 0, month_id))
    if close:
        db.execute("UPDATE months SET closed=0 WHERE id=?", (month_id,))
        close_month(db, month_id, estimated=True)
    else:
        db.commit()
        recompute_month(db, month_id, persist=True, force=True)
    carry_forward(db, month_id, old_cc, old_rest)
    return month_summary(db, month_id) | {"estimate_note": note}


# --------------------------------------------------------------- HTTP glue --

def valid_username(username: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.@-]{2,40}", username))


def is_lan_or_mesh(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    mesh = ipaddress.ip_network("100.64.0.0/10")
    return ip.is_loopback or ip.is_private or ip in mesh


TRUSTED_PROXIES = [n.strip() for n in os.environ.get("RUMO_TRUSTED_PROXIES", "").split(",") if n.strip()]


def peer_is_trusted_proxy(peer: str) -> bool:
    """Só se acredita em CF-Connecting-IP / X-Forwarded-For quando o pedido chega de um proxy
    nosso: loopback, rede privada (proxy na mesma máquina ou LAN) ou RUMO_TRUSTED_PROXIES.
    Ligado diretamente à Internet, qualquer um podia fingir ser da LAN."""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private:
        return True
    return any(ip in ipaddress.ip_network(n, strict=False) for n in TRUSTED_PROXIES)


def first_forwarded_for(headers: Any, fallback: str) -> str:
    if not peer_is_trusted_proxy(fallback):
        return fallback
    cf_ip = headers.get("CF-Connecting-IP", "")
    if cf_ip:
        return cf_ip.strip()
    xff = headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",", 1)[0].strip()
    return fallback


_login_fails: dict[str, list[float]] = {}
_login_lock = threading.Lock()
LOGIN_MAX_FAILS = int(os.environ.get("RUMO_LOGIN_MAX_FAILS", "6"))
LOGIN_WINDOW = 15 * 60


def login_blocked(keys: list[str]) -> bool:
    cutoff = time.time() - LOGIN_WINDOW
    with _login_lock:
        for k in keys:
            hits = [t for t in _login_fails.get(k, []) if t > cutoff]
            _login_fails[k] = hits
            if len(hits) >= LOGIN_MAX_FAILS:
                return True
    return False


def login_failed(keys: list[str]) -> None:
    with _login_lock:
        for k in keys:
            _login_fails.setdefault(k, []).append(time.time())
        if len(_login_fails) > 5000:  # não crescer sem limite
            _login_fails.clear()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS, base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class RumoHandler(SimpleHTTPRequestHandler):
    server_version = "RumoServer/1.0"
    # HTTP/1.0 (the base class default) closes the socket after every response, so each
    # request paid for a fresh TCP handshake. On the LAN that is invisible; over the
    # meshnet, with ~145 ms of round trip, it was an extra round trip per call and the
    # app opens seven on boot. 1.1 keeps the connection open and they share it.
    # Everything written below sets an accurate Content-Length, which is what 1.1 needs
    # to know where one response ends and the next begins.
    protocol_version = "HTTP/1.1"
    # ...and an idle keep-alive connection must not pin a thread forever.
    timeout = 30

    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def accepts_gzip(self) -> bool:
        return "gzip" in getattr(self, "headers", {}).get("Accept-Encoding", "").lower()

    @property
    def db_path(self) -> Path:
        return self.server.db_path  # type: ignore[attr-defined]

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def end_headers(self) -> None:
        # A malformed request line makes send_error() fire before parse_request() has
        # set self.path. Reaching for it here used to raise AttributeError *inside* the
        # error path, killing the thread before the 400 was written -- the client got a
        # dropped connection instead of a response. Default to "" so errors still send.
        path = urllib.parse.urlparse(getattr(self, "path", "")).path
        if path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        else:
            # Always revalidate the SPA shell/JS/CSS instead of trusting a stale browser
            # cache after a deploy -- avoids "nothing happens when I click X" reports
            # that are actually just an old index.html sitting in the browser.
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'")
        super().end_headers()

    def list_directory(self, path: str):  # type: ignore[override]
        self.send_error(404)
        return None

    def send_head(self):  # type: ignore[override]
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in urllib.parse.unquote(parsed.path).split("/") if p]
        if any(part.startswith(".") for part in parts):
            self.send_error(404)
            return None
        # Só se serve o ecrã da app e os seus recursos; nunca código, testes, seed nem cache do Python.
        if parts and (len(parts) > 1 or os.path.splitext(parts[-1])[1].lower() not in STATIC_EXTS):
            self.send_error(404)
            return None
        if parts and parts[-1].startswith("test_"):
            self.send_error(404)
            return None
        compressed = self.send_head_gzip()
        if compressed is not None:
            return compressed
        return super().send_head()

    def send_head_gzip(self):
        """Serve a text asset gzipped, or return None to let the base class handle it.

        Returns a file-like object exactly as send_head() must, so do_GET copies it to
        the socket and do_HEAD discards it -- both already work that way.
        """
        if not self.accepts_gzip():
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            # "/" resolves to the directory here; the base class is what appends the
            # index file. Do the same, or the app shell -- the biggest asset of all --
            # would be the one thing served uncompressed.
            if not self.path.endswith("/"):
                return None  # base class will redirect to the trailing-slash form
            for index in ("index.html", "index.htm"):
                candidate = os.path.join(path, index)
                if os.path.isfile(candidate):
                    path = candidate
                    break
            else:
                return None
        if os.path.splitext(path)[1].lower() not in COMPRESSIBLE_EXT:
            return None
        try:
            stat = os.stat(path)
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            return None  # missing or unreadable: let the base class produce the 404
        if len(raw) < GZIP_MIN_BYTES:
            return None

        body = gzip.compress(raw, GZIP_LEVEL)
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Last-Modified", self.date_time_string(int(stat.st_mtime)))
        self.end_headers()
        return io.BytesIO(body)

    def read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("Pedido demasiado grande")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def write_json(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoding = None
        if len(body) >= GZIP_MIN_BYTES and self.accepts_gzip():
            body = gzip.compress(body, GZIP_LEVEL)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def error_json(self, status: int, message: str) -> None:
        self.write_json(status, {"ok": False, "error": message})

    def cookie_token(self) -> str | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def session_cookie(self, token: str, max_age: int | None = None) -> str:
        age = SESSION_DAYS * 86400 if max_age is None else max_age
        parts = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={age}"]
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def sweep_sessions_if_due(self, db: sqlite3.Connection) -> None:
        """Drop expired sessions, but at most once an hour (see SESSION_SWEEP_EVERY)."""
        global _session_sweep_at
        with _session_sweep_lock:
            if time.monotonic() < _session_sweep_at:
                return
            _session_sweep_at = time.monotonic() + SESSION_SWEEP_EVERY
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
        db.commit()

    def current_user(self) -> sqlite3.Row | None:
        token = self.cookie_token()
        if not token:
            return None
        with self.db() as db:
            self.sweep_sessions_if_due(db)
            return db.execute(
                """SELECT users.id, users.username FROM sessions
                   JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token_hash = ? AND sessions.expires_at >= ?""",
                (token_hash(token), now()),
            ).fetchone()

    def require_user(self) -> sqlite3.Row | None:
        user = self.current_user()
        if not user:
            self.error_json(401, "Precisas de entrar")
        return user

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash(token), user_id, now(), now() + SESSION_DAYS * 86400),
            )
        return token

    # ------------------------------------------------------------- routing --

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            self.write_json(200, {"ok": True, "name": "Rumo"})
            return
        if path == "/api/auth/me":
            user = self.current_user()
            with self.db() as db:
                count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            client_ip = first_forwarded_for(self.headers, self.client_address[0])
            can_bootstrap = count == 0 and (ALLOW_PUBLIC_BOOTSTRAP or is_lan_or_mesh(client_ip))
            self.write_json(200, {
                "ok": True,
                "user": {"username": user["username"]} if user else None,
                "can_register": can_bootstrap or (count > 0 and ALLOW_SIGNUP),
            })
            return

        if not path.startswith("/api/"):
            return super().do_GET()

        user = self.require_user()
        if not user:
            return

        with self.db() as db:
            if path == "/api/months":
                rows = db.execute("SELECT id FROM months ORDER BY year, month").fetchall()
                for r in rows:
                    refresh_fatia(db, r["id"])
                out = [month_summary(db, r["id"]) for r in rows]
                self.write_json(200, {"ok": True, "months": out})
                return

            m = re.fullmatch(r"/api/months/(\d+)", path)
            if m:
                try:
                    refresh_fatia(db, int(m.group(1)))
                    self.write_json(200, {"ok": True, "month": month_summary(db, int(m.group(1)))})
                except ValueError as exc:
                    self.error_json(404, str(exc))
                return

            if path == "/api/templates":
                rows = db.execute(
                    "SELECT * FROM expense_templates ORDER BY sort_order, id"
                ).fetchall()
                self.write_json(200, {"ok": True, "templates": [
                    dict(r) | {"mutable": bool(r["mutable"]), "active": bool(r["active"])} for r in rows
                ]})
                return

            if path == "/api/pots":
                rows = db.execute("SELECT * FROM pots ORDER BY sort_order, id").fetchall()
                self.write_json(200, {"ok": True, "pots": [dict(r) | {"active": bool(r["active"])} for r in rows]})
                return

            if path == "/api/investments":
                rows = db.execute("SELECT * FROM investments ORDER BY sort_order, id").fetchall()
                self.write_json(200, {"ok": True, "investimentos": [dict(r) | {"active": bool(r["active"])} for r in rows]})
                return

            if path == "/api/settings":
                rows = db.execute("SELECT key, value FROM settings").fetchall()
                settings = {r["key"]: json.loads(r["value"]) for r in rows}
                for key_setting in ("anthropic_api_key", "openai_api_key", "gemini_api_key", "deepseek_api_key"):
                    settings[f"{key_setting}_set"] = bool(settings.pop(key_setting, ""))
                # Served by /api/investment-advice instead: it is a document, and it
                # has no business riding along on every settings fetch.
                settings.pop("last_advice", None)
                settings.pop("last_savings_advice", None)
                self.write_json(200, {"ok": True, "settings": settings})
                return

            if path == "/api/export":
                body = json.dumps(build_export(db), ensure_ascii=False, indent=2).encode("utf-8")
                stamp = datetime.date.today().isoformat()
                encoding = "gzip" if self.accepts_gzip() else None
                if encoding:
                    body = gzip.compress(body, GZIP_LEVEL)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                if encoding:
                    self.send_header("Content-Encoding", encoding)
                    self.send_header("Vary", "Accept-Encoding")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'attachment; filename="rumo-{stamp}.json"')
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/savings-advice":
                row = db.execute("SELECT value FROM settings WHERE key='last_savings_advice'").fetchone()
                self.write_json(200, {"ok": True, "advice": json.loads(row["value"]) if row else None})
                return

            if path == "/api/investment-advice":
                row = db.execute("SELECT value FROM settings WHERE key='last_advice'").fetchone()
                stored = json.loads(row["value"]) if row else None
                self.write_json(200, {"ok": True, "advice": stored})
                return

            if path == "/api/holdings":
                rows = db.execute(
                    """SELECT h.*, i.name AS investment_name FROM holdings h
                       JOIN investments i ON i.id = h.investment_id
                       ORDER BY h.investment_id, h.sort_order, h.id"""
                ).fetchall()
                self.write_json(200, {"ok": True, "holdings": [dict(r) for r in rows]})
                return

            if path == "/api/market/catalog":
                rows = db.execute("SELECT id, name FROM investments").fetchall()
                self.write_json(200, {
                    "ok": True,
                    "catalog": MARKET_CATALOG,
                    "kinds": {str(r["id"]): guess_kind(r["name"]) for r in rows},
                })
                return

            if path == "/api/market/search":
                q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                self.write_json(200, {"ok": True, "results": search_symbols(q)})
                return

            if path == "/api/investments/performance":
                q = urllib.parse.parse_qs(parsed.query)
                rng = HISTORY_RANGES.get(q.get("range", ["6mo"])[0], "6mo")
                rows = db.execute(
                    "SELECT ticker, quantity, manual_value FROM holdings"
                ).fetchall()
                hist = holdings_history(rows, rng)
                manual = sum(float(r["manual_value"] or 0) for r in rows
                             if r["manual_value"] is not None)
                self.write_json(200, {
                    "ok": True, "range": rng,
                    "labels": hist["labels"], "values": hist["values"],
                    "excluded_manual": round(manual, 2),
                })
                return

            if path == "/api/market/fx":
                cur = urllib.parse.parse_qs(parsed.query).get("from", ["USD"])[0]
                rate = fx_to_eur(cur)
                if rate is None:
                    self.error_json(400, f"não consegui o câmbio {cur.upper()}→{BASE_CURRENCY}")
                    return
                self.write_json(200, {"ok": True, "from": cur.upper(), "rate": rate})
                return

            if path == "/api/market/quote":
                q = urllib.parse.parse_qs(parsed.query).get("ticker", [""])[0]
                self.write_json(200, {"ok": True, "quote": fetch_price(q)})
                return

            if path == "/api/market/overview":
                self.write_json(200, {"ok": True, "benchmarks": market_overview()})
                return

            if path == "/api/market/news":
                qs = urllib.parse.parse_qs(parsed.query)
                topic = (qs.get("q", [""])[0] or "").strip()
                if topic:
                    items = relevant_news(topic, topic, 10)
                else:
                    items = portfolio_news(holding_assets(db), limit=24, strict=True)
                self.write_json(200, {"ok": True, "news": items})
                return

            if path == "/api/holdings/prices":
                rows = db.execute(
                    """SELECT h.*, i.name AS investment_name FROM holdings h
                       JOIN investments i ON i.id = h.investment_id
                       ORDER BY h.investment_id, h.sort_order, h.id"""
                ).fetchall()
                out = []
                for r in rows:
                    h = dict(r)
                    if h.get("manual_value") is not None:
                        h["price_info"] = {"ok": True, "manual": True}
                        h["current_value"] = h["manual_value"]
                        h["cost_basis"] = None
                        h["gain_loss"] = None
                        h["gain_loss_pct"] = None
                        out.append(h)
                        continue
                    price_info = fetch_price(h["ticker"]) if h["ticker"] else {"ok": False, "error": "sem ticker"}
                    h["price_info"] = price_info
                    if price_info.get("ok"):
                        price = price_in_eur(price_info) or 0
                        h["current_value"] = price * h["quantity"]
                        h["cost_basis"] = h["avg_cost"] * h["quantity"]
                        h["gain_loss"] = h["current_value"] - h["cost_basis"]
                        h["gain_loss_pct"] = (h["gain_loss"] / h["cost_basis"] * 100) if h["cost_basis"] else None
                    out.append(h)
                self.write_json(200, {"ok": True, "holdings": out})
                return

        self.error_json(404, "API desconhecida")

    def do_HEAD(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        self.route_write("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self.route_write("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self.route_write("DELETE")

    def csrf_ok(self) -> bool:
        """Pedidos que mudam dados: Origin (se vier) tem de ser o próprio site e o corpo tem de ser JSON,
        o que um formulário de outro site não consegue enviar."""
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            hosts = {self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", "")}
            if urllib.parse.urlparse(origin).netloc not in hosts:
                return False
        elif origin == "null":
            return False
        if int(self.headers.get("Content-Length", "0") or "0") > 0:
            return self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"
        return True

    def route_write(self, method: str) -> None:
        path = urllib.parse.urlparse(self.path).path
        if not self.csrf_ok():
            self.error_json(403, "Pedido recusado (origem ou tipo de conteúdo)")
            return
        try:
            data = self.read_json() if method != "DELETE" else {}
        except Exception as exc:
            self.error_json(400, str(exc) or "JSON inválido")
            return

        if path == "/api/auth/register":
            self.handle_register(data)
            return
        if path == "/api/auth/login":
            self.handle_login(data)
            return
        if path == "/api/auth/logout":
            token = self.cookie_token()
            if token:
                with self.db() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
            self.write_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie("", 0)})
            return
        if path == "/api/giro/sync" and method == "POST":
            client_ip = first_forwarded_for(self.headers, self.client_address[0])
            if not is_lan_or_mesh(client_ip):
                self.error_json(403, "A ponte do Giro só aceita pedidos locais, LAN ou Meshnet")
                return
            with self.db() as db:
                sync = sync_giro_debts(db)
                month = None
                if sync.get("ok") and sync.get("month"):
                    month = recompute_month(db, int(sync["month"]["id"]), persist=True, force=True)
                self.write_json(200 if sync.get("ok") else 503, {"ok": bool(sync.get("ok")), "sync": sync, "month": month})
            return

        user = self.require_user()
        if not user:
            return

        try:
            with self.db() as db:
                self.route_authenticated(db, method, path, data)
        except ValueError as exc:
            self.error_json(400, str(exc))
        except sqlite3.IntegrityError as exc:
            self.error_json(409, str(exc))

    def route_authenticated(self, db: sqlite3.Connection, method: str, path: str, data: dict) -> None:
        if method == "POST" and path == "/api/months":
            self.create_month(db, data)
            return
        m = re.fullmatch(r"/api/months/(\d+)", path)
        if m and method == "PATCH":
            self.update_month(db, int(m.group(1)), data)
            return
        if m and method == "DELETE":
            # Só o último mês: é o engano que se desfaz (um "›" carregado cedo de
            # mais). Um mês do meio deixava um buraco que o "›" já não volta a criar,
            # e os do Excel são o registo real. O Giro não guarda nada dos meses do
            # Rumo, por isso do lado de lá não há nada a acertar.
            target = db.execute("SELECT * FROM months WHERE id=?", (int(m.group(1)),)).fetchone()
            last = db.execute("SELECT id FROM months ORDER BY year DESC, month DESC LIMIT 1").fetchone()
            if target is None:
                self.error_json(404, "mês não encontrado")
                return
            if target["historical_import"] or target["id"] != last["id"]:
                self.error_json(400, "Só se apaga o último mês — num mês anterior, reabre-o e corrige")
                return
            db.execute("DELETE FROM months WHERE id=?", (target["id"],))
            db.commit()
            self.write_json(200, {"ok": True})
            return
        m = re.fullmatch(r"/api/months/(\d+)/moves", path)
        if m and method == "POST":
            # {key, done} — uma marca de cada vez, para dois aparelhos não se apagarem.
            try:
                done = set_move_done(db, int(m.group(1)), str(data.get("key") or ""), bool(data.get("done")))
            except LookupError as exc:
                self.error_json(404, str(exc))
                return
            self.write_json(200, {"ok": True, "moves_done": done})
            return
        m = re.fullmatch(r"/api/months/(\d+)/estimate", path)
        if m and method == "POST":
            summary = estimate_month(
                db, int(m.group(1)), str(data.get("expenses") or "keep"),
                None if data.get("no_savings") is None else bool(data.get("no_savings")),
                bool(data.get("close")),
            )
            self.write_json(200, {"ok": True, "month": summary})
            return
        m = re.fullmatch(r"/api/months/(\d+)/recompute", path)
        if m and method == "POST":
            force = bool(data.get("force"))
            sync_giro_debts(db, int(m.group(1)))
            summary = recompute_month(db, int(m.group(1)), persist=True, force=force)
            self.write_json(200, {"ok": True, "month": summary})
            return

        if method == "POST" and path == "/api/expenses":
            self.create_expense(db, data)
            return
        m = re.fullmatch(r"/api/expenses/(\d+)", path)
        if m and method == "PATCH":
            self.update_expense(db, int(m.group(1)), data)
            return
        if m and method == "DELETE":
            row = db.execute("SELECT month_id, external_source FROM month_expenses WHERE id=?", (int(m.group(1)),)).fetchone()
            if row is None:
                self.error_json(404, "item não encontrado")
                return
            if row["external_source"] == "giro":
                self.error_json(409, "Esta linha vem do Giro: remove-a ou muda-a lá.")
                return
            db.execute("DELETE FROM month_expenses WHERE id=?", (int(m.group(1)),))
            db.commit()
            # O ecrã troca o mês pelo que vem aqui, como no PATCH: sem ele ficava
            # sem mês nenhum e dizia «Sem meses ainda».
            summary = recompute_month(db, row["month_id"], persist=True)
            self.write_json(200, {"ok": True, "month": summary})
            return

        if method == "POST" and path == "/api/templates":
            self.create_template(db, data)
            return
        m = re.fullmatch(r"/api/templates/(\d+)", path)
        if m and method == "PATCH":
            self.update_generic(db, "expense_templates", int(m.group(1)), data,
                                 {"category", "name", "obs", "default_planned", "mutable", "account", "joint_account", "active", "sort_order"})
            self.write_json(200, {"ok": True})
            return
        if m and method == "DELETE":
            db.execute("DELETE FROM expense_templates WHERE id=?", (int(m.group(1)),))
            db.commit()
            self.write_json(200, {"ok": True})
            return

        if method == "POST" and path == "/api/pots":
            cur = db.execute("INSERT INTO pots(name, pct, sort_order) VALUES (?, ?, ?)",
                              (data.get("name", ""), float(data.get("pct", 0)), int(data.get("sort_order", 0))))
            db.commit()
            self.write_json(201, {"ok": True, "id": cur.lastrowid})
            return
        m = re.fullmatch(r"/api/pots/(\d+)", path)
        if m and method == "PATCH":
            if "target_accumulated" in data:
                data = dict(data)
                real = float(data.pop("target_accumulated"))
                log_check(db, "pot", int(m.group(1)), expected_balance(db, "pots", "pot_snapshots", "pot_id", int(m.group(1))), real)
                data["opening_balance"] = solve_opening_balance(db, "pot_snapshots", "pot_id", int(m.group(1)), real)
            if "rate_mode" in data and data["rate_mode"] not in RATE_MODES:
                self.error_json(400, "Rate mode: use none, tanb or eur")
                return
            data = dict(data)
            for k, hi in (("rate_value", 1e9), ("rate_tax", 100)):
                if k in data:
                    try:
                        data[k] = min(hi, max(0.0, float(data[k] or 0)))
                    except (TypeError, ValueError):
                        data[k] = 0.0
            self.update_generic(db, "pots", int(m.group(1)), data,
                                {"name", "pct", "active", "sort_order", "opening_balance",
                                 "goal_amount", "goal_date", "ai_locked", "level", "purpose",
                                 "rate_mode", "rate_value", "rate_tax"}, coerce_bool={"ai_locked"})
            self.recompute_current(db)
            self.write_json(200, {"ok": True})
            return
        if m and method == "DELETE":
            db.execute("DELETE FROM pots WHERE id=?", (int(m.group(1)),))
            db.commit()
            self.write_json(200, {"ok": True})
            return

        if method == "POST" and path == "/api/investments":
            cur = db.execute("INSERT INTO investments(name, pct, sort_order) VALUES (?, ?, ?)",
                              (data.get("name", ""), float(data.get("pct", 0)), int(data.get("sort_order", 0))))
            db.commit()
            self.write_json(201, {"ok": True, "id": cur.lastrowid})
            return
        m = re.fullmatch(r"/api/investments/(\d+)", path)
        if m and method == "PATCH":
            if "target_accumulated" in data:
                data = dict(data)
                real = float(data.pop("target_accumulated"))
                log_check(db, "inv", int(m.group(1)), expected_balance(db, "investments", "investment_snapshots", "investment_id", int(m.group(1))), real)
                data["opening_balance"] = solve_opening_balance(db, "investment_snapshots", "investment_id", int(m.group(1)), real)
            self.update_generic(db, "investments", int(m.group(1)), data, {"name", "pct", "active", "sort_order", "opening_balance", "ai_locked"}, coerce_bool={"ai_locked"})
            self.recompute_current(db)
            self.write_json(200, {"ok": True})
            return
        if m and method == "DELETE":
            db.execute("DELETE FROM investments WHERE id=?", (int(m.group(1)),))
            db.commit()
            self.write_json(200, {"ok": True})
            return

        if method == "POST" and path == "/api/holdings":
            self.create_holding(db, data)
            return
        m = re.fullmatch(r"/api/holdings/(\d+)", path)
        if m and method == "PATCH":
            if {"current_value", "quantity", "total_cost"} & set(data):
                data = dict(data)
                current_value = data.pop("current_value", None)
                total_cost = data.pop("total_cost", None)
                try:
                    if current_value not in (None, ""):
                        current_value = amount_to_eur(
                            current_value, data.pop("current_value_currency", None))
                    if total_cost not in (None, ""):
                        total_cost = amount_to_eur(
                            total_cost, data.pop("total_cost_currency", None))
                except ValueError as exc:
                    self.error_json(400, str(exc))
                    return
                row = db.execute(
                    "SELECT ticker, quantity, avg_cost FROM holdings WHERE id=?", (int(m.group(1)),)
                ).fetchone()
                ticker = row["ticker"] if row else ""
                if not ticker and current_value is not None:
                    self.error_json(400, "Esta posição não tem ticker -- usa o campo de valor manual em vez disto.")
                    return
                if ticker:
                    price_info = fetch_price(ticker)
                    if not price_info.get("ok") or not price_in_eur(price_info):
                        self.error_json(400, f"Não consegui obter o preço de {ticker}: {price_info.get('error','sem preço')}")
                        return
                    price = price_in_eur(price_info)
                    if current_value is not None:
                        # Value-first edit: back-solve the quantity from today's price.
                        # avg_cost stays as set at first entry, so gain/loss keeps
                        # reflecting performance since then.
                        data["quantity"] = float(current_value) / price
                    if not row["avg_cost"] and total_cost in (None, ""):
                        # No cost basis yet (position created before it had a real
                        # value): anchor it to today rather than reporting an
                        # infinite gain. An explicit total_cost below wins over this.
                        data["avg_cost"] = price
                    # Keep the row on the symbol Yahoo actually prices.
                    if price_info["ticker"] != ticker:
                        data["ticker"] = price_info["ticker"]
                if total_cost not in (None, ""):
                    # What someone remembers is the total they spent, not a per-unit
                    # average -- convert against the quantity this same patch sets, so
                    # editing both at once stays consistent.
                    qty = float(data.get("quantity", row["quantity"]))
                    data["avg_cost"] = (float(total_cost) / qty) if qty else 0.0
            self.update_generic(
                db, "holdings", int(m.group(1)), data,
                {"investment_id", "ticker", "name", "quantity", "avg_cost", "currency", "notes", "sort_order", "manual_value"},
            )
            self.write_json(200, {"ok": True})
            return
        if m and method == "DELETE":
            db.execute("DELETE FROM holdings WHERE id=?", (int(m.group(1)),))
            db.commit()
            self.write_json(200, {"ok": True})
            return

        if method == "PATCH" and path == "/api/settings":
            data = dict(data)
            if "geral_target_accumulated" in data:
                current_month_id = latest_open_month_id(db)
                geral_contrib = db.execute(
                    """SELECT COALESCE(SUM(g.contributed), 0) AS total
                       FROM geral_snapshots g JOIN months mo ON mo.id = g.month_id
                       WHERE mo.historical_import = 0 AND (? IS NULL OR mo.id != ?)""",
                    (current_month_id, current_month_id),
                ).fetchone()["total"]
                real = float(data.pop("geral_target_accumulated"))
                old_open = db.execute("SELECT value FROM settings WHERE key='geral_opening_balance'").fetchone()
                log_check(db, "geral", 0, (float(json.loads(old_open["value"])) if old_open else 0.0) + geral_contrib, real)
                data["geral_opening_balance"] = real - geral_contrib
            if "joint_account_target_accumulated" in data:
                # O valor real no fim do mês anterior ao mês que se está a ver.
                ref = db.execute("SELECT year, month FROM months WHERE id=?",
                                 (data.pop("joint_month_id", None) or latest_open_month_id(db),)).fetchone()
                before = joint_net_sum(db, ref["year"], ref["month"] - 1) if ref else 0.0
                real = float(data.pop("joint_account_target_accumulated"))
                old_open = db.execute("SELECT value FROM settings WHERE key='joint_account_opening_balance'").fetchone()
                log_check(db, "joint", 0, (float(json.loads(old_open["value"])) if old_open else 0.0) + before, real)
                data["joint_account_opening_balance"] = round(real - before, 2)
            data.pop("joint_month_id", None)
            if "language" in data and data["language"] not in ("en", "pt-PT"):
                self.error_json(400, "Language: use en or pt-PT")
                return
            if "currency" in data:
                code = str(data["currency"] or "").strip().upper()
                if not re.fullmatch(r"[A-Z]{3}", code):
                    self.error_json(400, "Moeda: usa o código de 3 letras (EUR, USD, GBP…)")
                    return
                data["currency"] = code
                set_base_currency(code)
            for k, v in data.items():
                db.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v, ensure_ascii=False)),
                )
            db.commit()
            if {"round_pots_enabled", "geral_opening_balance", "joint_split_mode", "joint_my_share",
                    "joint_partner_income", "joint_from_account", "use_transfer", "use_joint"} & set(data):
                self.recompute_current(db)
            self.write_json(200, {"ok": True})
            return

        if method == "POST" and path == "/api/reorder":
            self.handle_reorder(db, data)
            return

        if method == "POST" and path == "/api/investment-advice":
            self.handle_investment_advice(db)
            return

        m = re.fullmatch(r"/api/rebalance/(inv|pot)(/undo)?", path)
        if method == "POST" and m:
            (self.handle_rebalance_undo if m.group(2) else self.handle_rebalance)(db, m.group(1))
            return

        if method == "POST" and path == "/api/savings-advice":
            self.handle_savings_advice(db)
            return

        self.error_json(404, "API desconhecida")

    def recompute_current(self, db: sqlite3.Connection) -> None:
        # Global pot/investment/settings changes affect every open month's waterfall, not
        # just the newest one -- recomputing only the latest left older open months with
        # stale per-pot snapshots (contributions computed under previous %/opening_balance
        # settings), which then leaked into the cumulative "accumulated" total.
        rows = db.execute("SELECT id FROM months WHERE closed=0 ORDER BY year, month").fetchall()
        for row in rows:
            recompute_month(db, row["id"], persist=True)

    def create_month(self, db: sqlite3.Connection, data: dict) -> None:
        year = int(data["year"])
        month = int(data["month"])
        existing = db.execute(
            "SELECT id FROM months WHERE year=? AND month=?", (year, month)
        ).fetchone()
        if existing is not None:
            self.write_json(200, {"ok": True, "month": month_summary(db, existing["id"])})
            return
        prev = db.execute(
            "SELECT * FROM months WHERE year < ? OR (year = ? AND month < ?) ORDER BY year DESC, month DESC LIMIT 1",
            (year, year, month),
        ).fetchone()
        pct_revolut = prev["pct_revolut"] if prev else 0.7
        pct_poupanca = prev["pct_poupanca"] if prev else 0.8
        split_geral = prev["split_geral"] if prev else 0.12
        split_cofres = prev["split_cofres"] if prev else 0.58
        split_inv = prev["split_investimentos"] if prev else 0.3
        revolut_anterior = prev["computed_livre_resto"] if prev else 0.0
        santander_anterior = prev["computed_cc_livre"] if prev else 0.0
        ordenado = prev["ordenado"] if prev else 0.0
        joint_extra = (prev["joint_extra"] or 0.0) if prev else 0.0

        cur = db.execute(
            """INSERT INTO months
               (year, month, ordenado, extras, pct_revolut, pct_poupanca,
                split_geral, split_cofres, split_investimentos, revolut_anterior, santander_anterior, joint_extra)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (year, month, ordenado, pct_revolut, pct_poupanca, split_geral, split_cofres, split_inv, revolut_anterior,
             santander_anterior, joint_extra),
        )
        month_id = cur.lastrowid

        source = str(data.get("expenses") or "catalog")
        new_row = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
        lines = None
        if source == "prev":
            lines = lines_from_previous(db, new_row)
        elif source == "avg":
            lines = lines_from_average(db, new_row)
        if lines is not None:
            replace_own_lines(db, month_id, lines)
        templates = [] if lines is not None else db.execute(
            "SELECT * FROM expense_templates WHERE active=1 ORDER BY sort_order, id"
        ).fetchall()
        for t in templates:
            db.execute(
                """INSERT INTO month_expenses
                   (month_id, template_id, category, name, obs, planned, mutable, account, joint_account, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (month_id, t["id"], t["category"], t["name"], t["obs"], t["default_planned"],
                 t["mutable"], t["account"], t["joint_account"], t["sort_order"]),
            )
        sync_giro_debts(db, month_id)
        db.commit()
        summary = recompute_month(db, month_id, persist=True, force=True)
        self.write_json(201, {"ok": True, "month": summary})

    def update_month(self, db: sqlite3.Connection, month_id: int, data: dict) -> None:
        fields = {
            "ordenado", "extras", "pct_revolut", "pct_poupanca", "split_geral",
            "split_cofres", "split_investimentos", "revolut_anterior", "santander_anterior", "notes",
            "joint_share", "joint_me_actual", "joint_partner_actual", "no_savings", "joint_extra",
        }
        row = db.execute("SELECT * FROM months WHERE id=?", (month_id,)).fetchone()
        if row is None:
            self.error_json(404, "mês não encontrado")
            return
        self.update_generic(db, "months", month_id, data, fields, coerce_bool={"no_savings"})
        if "closed" in data:
            if data["closed"] and not row["closed"]:
                close_month(db, month_id)
            elif not data["closed"]:
                # Reabrir tira também o «estimado»: daqui para a frente as contas são à mão.
                db.execute("UPDATE months SET closed=0, estimated=0 WHERE id=?", (month_id,))
                db.commit()
        summary = recompute_month(db, month_id, persist=True)
        # Desmarcar o «não poupei», reabrir ou mudar o ordenado mexe no que sobra;
        # o mês seguinte acompanha enquanto lá estiver o valor automático.
        carry_forward(db, month_id, row["computed_cc_livre"], row["computed_livre_resto"])
        self.write_json(200, {"ok": True, "month": summary})

    def create_expense(self, db: sqlite3.Connection, data: dict) -> None:
        month_id = int(data["month_id"])
        cur = db.execute(
            """INSERT INTO month_expenses
               (month_id, category, name, obs, planned, mutable, account, joint_account, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                month_id, data.get("category", "Outros"), data.get("name", ""),
                data.get("obs", ""), float(data.get("planned", 0)),
                1 if data.get("mutable") else 0, data.get("account", "S"),
                1 if data.get("joint_account") else 0,
                int(data.get("sort_order", 0)),
            ),
        )
        db.commit()
        summary = recompute_month(db, month_id, persist=True)
        self.write_json(201, {"ok": True, "id": cur.lastrowid, "month": summary})

    def update_expense(self, db: sqlite3.Connection, expense_id: int, data: dict) -> None:
        row = db.execute("SELECT month_id, external_source FROM month_expenses WHERE id=?", (expense_id,)).fetchone()
        if row is None:
            self.error_json(404, "item não encontrado")
            return
        fields = {"category", "name", "obs", "planned", "actual", "mutable", "account", "joint_account", "closed", "sort_order"}
        # As linhas do Giro são reescritas a cada sincronização: aqui só se mudam
        # de lugar. Os valores, o nome e a conta mudam-se no Giro.
        if row["external_source"] == "giro":
            fields = {"sort_order"}
        self.update_generic(db, "month_expenses", expense_id, data, fields, coerce_bool={"mutable", "closed", "joint_account"})
        summary = recompute_month(db, row["month_id"], persist=True)
        self.write_json(200, {"ok": True, "month": summary})

    def create_template(self, db: sqlite3.Connection, data: dict) -> None:
        cur = db.execute(
            """INSERT INTO expense_templates
               (category, name, obs, default_planned, mutable, account, joint_account, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("category", "Outros"), data.get("name", ""), data.get("obs", ""),
                float(data.get("default_planned", 0)), 1 if data.get("mutable") else 0,
                data.get("account", "S"), 1 if data.get("joint_account") else 0,
                int(data.get("sort_order", 0)),
            ),
        )
        db.commit()
        self.write_json(201, {"ok": True, "id": cur.lastrowid})

    def create_holding(self, db: sqlite3.Connection, data: dict) -> None:
        manual_value = data.get("manual_value")
        ticker = data.get("ticker", "").strip().upper()
        quantity = data.get("quantity")
        avg_cost = data.get("avg_cost")
        current_value = data.get("current_value")
        total_cost = data.get("total_cost")
        # Amounts arrive in whichever currency the user picked next to the field.
        try:
            if current_value not in (None, ""):
                current_value = amount_to_eur(current_value, data.get("current_value_currency"))
            if total_cost not in (None, ""):
                total_cost = amount_to_eur(total_cost, data.get("total_cost_currency"))
        except ValueError as exc:
            self.error_json(400, str(exc))
            return

        if ticker and (quantity not in (None, "") or current_value not in (None, "")):
            # Both routes need today's price: quantity-first to value the position and
            # anchor its cost, value-first to back-solve the quantity.
            price_info = fetch_price(ticker)
            if not price_info.get("ok") or not price_in_eur(price_info):
                self.error_json(400, f"Não consegui obter o preço de {ticker}: {price_info.get('error','sem preço')}")
                return
            price = price_in_eur(price_info)
            if quantity not in (None, ""):
                # Quantity-first: the units are what the broker actually shows you, and
                # the euro value follows from them. Going the other way makes the
                # position depend on two apps agreeing on a price, which they never do.
                quantity = float(quantity)
            else:
                quantity = float(current_value) / price
            if avg_cost in (None, ""):
                # A total spend is friendlier to supply than a per-unit average; with
                # neither, anchor the cost to today so gain/loss starts at zero.
                avg_cost = (float(total_cost) / quantity) if (total_cost not in (None, "") and quantity) else price
            avg_cost = float(avg_cost)
            # Store the symbol Yahoo actually prices, so "XAU" becomes "GC=F" once and
            # every later refresh hits a symbol that resolves.
            ticker = price_info["ticker"]
            if not data.get("name"):
                data = dict(data, name=price_info.get("name", ""))
        else:
            quantity = float(quantity or 0)
            avg_cost = float(avg_cost or 0)

        cur = db.execute(
            """INSERT INTO holdings (investment_id, ticker, name, quantity, avg_cost, currency, notes, sort_order, manual_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(data["investment_id"]), ticker, data.get("name", ""),
                quantity, avg_cost,
                data.get("currency", "EUR"), data.get("notes", ""), int(data.get("sort_order", 0)),
                float(manual_value) if manual_value not in (None, "") else None,
            ),
        )
        db.commit()
        self.write_json(201, {"ok": True, "id": cur.lastrowid})

    def update_generic(self, db: sqlite3.Connection, table: str, row_id: int, data: dict,
                        allowed: set[str], coerce_bool: set[str] | None = None) -> None:
        coerce_bool = coerce_bool or set()
        sets = []
        values: list[Any] = []
        for key, value in data.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append((1 if value else 0) if key in coerce_bool else value)
        if not sets:
            return
        values.append(row_id)
        db.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", values)
        db.commit()

    REORDERABLE_TABLES = {"pots", "investments", "expense_templates", "month_expenses", "holdings"}

    def handle_reorder(self, db: sqlite3.Connection, data: dict) -> None:
        table = data.get("table")
        ids = data.get("ids")
        if table not in self.REORDERABLE_TABLES or not isinstance(ids, list):
            self.error_json(400, "tabela ou lista de ids inválida")
            return
        for index, entity_id in enumerate(ids):
            db.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (index, int(entity_id)))
        db.commit()
        self.write_json(200, {"ok": True})

    def store_advice(self, db: sqlite3.Connection, key: str, text: str, extra: dict) -> dict:
        provider_row = db.execute("SELECT value FROM settings WHERE key='ai_provider'").fetchone()
        stored = {"text": text, "at": now(),
                  "provider": json.loads(provider_row["value"]) if provider_row else ""} | extra
        db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(stored)),
        )
        db.commit()
        return stored

    def handle_savings_advice(self, db: sqlite3.Connection) -> None:
        month_row = db.execute(
            "SELECT id FROM months WHERE closed=0 ORDER BY year DESC, month DESC LIMIT 1"
        ).fetchone()
        if not month_row:
            self.error_json(400, "Sem mês aberto para analisar.")
            return
        summary = month_summary(db, month_row["id"])
        ok, text = call_ai(db, build_savings_prompt(db, summary))
        if not ok:
            self.error_json(400 if "Adiciona a tua chave" in text else 502, text)
            return
        months = db.execute("SELECT COUNT(*) AS n FROM months").fetchone()["n"]
        stored = self.store_advice(db, "last_savings_advice", text, {"months": months})
        self.write_json(200, {"ok": True, "advice": stored})

    def handle_investment_advice(self, db: sqlite3.Connection) -> None:
        month_row = db.execute(
            "SELECT id FROM months WHERE closed=0 ORDER BY year DESC, month DESC LIMIT 1"
        ).fetchone()
        if not month_row:
            self.error_json(400, "Sem mês aberto para calcular o orçamento de investimento.")
            return
        summary = month_summary(db, month_row["id"])
        prompt = build_investment_prompt(db, summary)
        ok, text = call_ai(db, prompt)
        if not ok:
            # A failed call leaves the previous suggestion in place: it cost money and
            # a network error is no reason to throw it away.
            self.error_json(400 if "Adiciona a tua chave" in text else 502, text)
            return
        mode_row = db.execute("SELECT value FROM settings WHERE key='ai_advice_mode'").fetchone()
        stored = self.store_advice(db, "last_advice", text, {
            "mode": json.loads(mode_row["value"]) if mode_row else "",
            "budget": summary["poupanca_total"] * summary["split_investimentos"],
        })
        self.write_json(200, {"ok": True, "advice": stored})

    def handle_rebalance(self, db: sqlite3.Connection, kind: str) -> None:
        """A IA lê os números (e, nos investimentos, as manchetes) e muda as % dos itens
        sem cadeado. O antes fica guardado (settings.last_rebalance_<kind>) para desfazer."""
        table = REBALANCE_KINDS[kind][0]
        month_row = db.execute(
            "SELECT id FROM months WHERE closed=0 ORDER BY year DESC, month DESC LIMIT 1"
        ).fetchone()
        if not month_row:
            self.error_json(400, "Sem mês aberto.")
            return
        items = [dict(i) | ({"ai_locked": 1} if kind == "pot" and i["level"] == 1 else {})
                 for i in db.execute(f"SELECT * FROM {table} WHERE active=1 ORDER BY sort_order, id").fetchall()]
        if not [i for i in items if not i["ai_locked"]]:
            self.error_json(400, "Está tudo bloqueado: tira o cadeado a pelo menos um.")
            return
        ok, text = call_ai(db, rebalance_prompt(db, month_summary(db, month_row["id"]), kind, items))
        if not ok:
            self.error_json(400 if "Adiciona a tua chave" in text else 502, text)
            return
        try:
            result = parse_rebalance(text, items)
        except (ValueError, TypeError) as exc:
            self.error_json(502, f"{exc} Nada foi mudado.")
            return
        before = {int(i["id"]): round(float(i["pct"]), 4) for i in items}
        # Com «0,50 € certos» ligado, as % novas também dão valores redondos no mês (o que
        # sobra vai para o Geral, como no resto).
        if setting(db, "snap_round_pct", True) is not False:
            m = month_summary(db, month_row["id"])
            base = m["poupanca_total"] * (m["split_investimentos"] if kind == "inv" else m["split_cofres"])
            locked = {int(i["id"]) for i in items if i["ai_locked"]}
            if base > 0:
                result["after"] = {cid: (pct if cid in locked else round(round(pct * base / 0.5) * 0.5 / base, 6))
                                   for cid, pct in result["after"].items()}
        for cid, pct in result["after"].items():
            db.execute(f"UPDATE {table} SET pct=? WHERE id=?", (pct, cid))
        db.commit()
        self.recompute_current(db)
        stored = self.store_advice(db, f"last_rebalance_{kind}", result["resumo"], {
            "before": before, "after": result["after"], "reasons": result["reasons"],
            "names": {int(i["id"]): i["name"] for i in items},
            "locked": [int(i["id"]) for i in items if i["ai_locked"]],
            "fixed": [int(i["id"]) for i in items if kind == "pot" and i.get("level") == 1], "undone": False,
        })
        self.write_json(200, {"ok": True, "rebalance": stored})

    def handle_rebalance_undo(self, db: sqlite3.Connection, kind: str) -> None:
        table, key = REBALANCE_KINDS[kind][0], f"last_rebalance_{kind}"
        last = setting(db, key, None)
        if not isinstance(last, dict) or last.get("undone"):
            self.error_json(400, "Não há ajuste da IA para desfazer.")
            return
        for cid, pct in (last.get("before") or {}).items():
            db.execute(f"UPDATE {table} SET pct=? WHERE id=?", (float(pct), int(cid)))
        last["undone"] = True
        db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(last), key))
        db.commit()
        self.recompute_current(db)
        self.write_json(200, {"ok": True, "rebalance": last})

    def handle_register(self, data: Any) -> None:
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if not valid_username(username):
            self.error_json(400, "Usa 2-40 caracteres: letras, números, _, ., @ ou -")
            return
        if len(password) < 10:
            self.error_json(400, "A password precisa de pelo menos 10 caracteres")
            return
        client_ip = first_forwarded_for(self.headers, self.client_address[0])
        with self.db() as db:
            count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count == 0 and not (ALLOW_PUBLIC_BOOTSTRAP or is_lan_or_mesh(client_ip)):
                self.error_json(403, "Cria o primeiro utilizador pela LAN ou Meshnet")
                return
            if count > 0 and not ALLOW_SIGNUP:
                self.error_json(403, "Registo fechado neste servidor")
                return
            try:
                cur = db.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, hash_password(password), now()),
                )
            except sqlite3.IntegrityError:
                self.error_json(409, "Esse utilizador já existe")
                return
            user_id = cur.lastrowid
        token = self.create_session(int(user_id))
        self.write_json(201, {"ok": True, "user": {"username": username}}, {"Set-Cookie": self.session_cookie(token)})

    def handle_login(self, data: Any) -> None:
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        keys = ["ip:" + first_forwarded_for(self.headers, self.client_address[0]), "user:" + username.lower()]
        if login_blocked(keys):
            self.error_json(429, "Demasiadas tentativas. Espera uns 15 minutos.")
            return
        with self.db() as db:
            row = db.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            login_failed(keys)
            self.error_json(401, "Utilizador ou password inválidos")
            return
        token = self.create_session(int(row["id"]))
        self.write_json(200, {"ok": True, "user": {"username": row["username"]}}, {"Set-Cookie": self.session_cookie(token)})


def main() -> None:
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("RUMO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RUMO_PORT", "8099")))
    parser.add_argument("--data-dir", default=os.environ.get("RUMO_DATA_DIR", str(Path.home() / ".local/share/rumo")))
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    db_path = data_dir / "rumo.db"
    init_db(db_path)
    with sqlite3.connect(db_path) as _db:
        _db.row_factory = sqlite3.Row
        set_base_currency(setting(_db, "currency", "EUR"))

    server = ThreadingHTTPServer((args.host, args.port), RumoHandler)
    server.db_path = db_path  # type: ignore[attr-defined]
    print(f"Rumo a ouvir em http://{args.host}:{args.port} · dados em {db_path}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
