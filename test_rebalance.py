"""Verificação mínima do ajuste da IA (limites, cadeado, soma) e do filtro de notícias: python3 test_rebalance.py"""
import server as r

items = [{"id": 1, "pct": 0.30, "ai_locked": 0}, {"id": 2, "pct": 0.30, "ai_locked": 0}, {"id": 3, "pct": 0.40, "ai_locked": 0}]
# A IA quer 70% na 1 (+40), inventa um id 9 e esquece a 3.
out = r.parse_rebalance('bla {"resumo": "ok", "itens": [{"id": 1, "pct": 70, "porque": "x"},'
                        ' {"id": 2, "pct": 20}, {"id": 9, "pct": 50}]} bla', items)
a = out["after"]
assert set(a) == {1, 2, 3}                                   # o id 9 não existe: ignorado
assert abs(sum(a.values()) - 1) < 0.001                      # soma 100%
assert round(a[1] / a[3], 3) == round(45 / 40, 3)            # 70 cortado para 30+15; a 3 fica nos 40
assert out["reasons"][1] == "x" and out["resumo"] == "ok"

# Com cadeado na 3 (40%): fica igual, e as livres repartem os 60% que sobram.
items[2]["ai_locked"] = 1
a = r.parse_rebalance('{"itens": [{"id": 1, "pct": 40}, {"id": 2, "pct": 20}, {"id": 3, "pct": 0}]}', items)["after"]
assert a[3] == 0.40 and abs(a[1] + a[2] - 0.60) < 0.001 and a[1] > a[2]

try:
    r.parse_rebalance("sem json nenhum", items)
    raise AssertionError("devia recusar")
except ValueError:
    pass
assert r.name_words("Eli Lilly and Company") == ["Eli", "Lilly"]
assert r.name_words("Paládio — XPD") == ["Paládio", "XPD"]
assert r.headline_names_asset("Eli Lilly beats estimates", "LLY", "Eli Lilly and Company")
assert not r.headline_names_asset("3 Great Biotech Stocks To Watch", "LLY", "Eli Lilly and Company")
print("ok")
