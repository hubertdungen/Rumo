"""Segurança básica: IP de proxy, limite de logins, CSRF. python3 test_seguranca.py"""
import server as r

# Ligado diretamente à Internet, ninguém pode fingir que vem da LAN com cabeçalhos
assert r.first_forwarded_for({"CF-Connecting-IP": "192.168.1.5"}, "8.8.8.8") == "8.8.8.8"
assert not r.is_lan_or_mesh(r.first_forwarded_for({"X-Forwarded-For": "10.0.0.2"}, "8.8.8.8"))
# Atrás de um proxy na mesma máquina ou na LAN, o IP verdadeiro vem do cabeçalho
assert r.first_forwarded_for({"CF-Connecting-IP": "203.0.113.9"}, "127.0.0.1") == "203.0.113.9"
assert r.first_forwarded_for({"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}, "192.168.1.2") == "203.0.113.9"

# Depois de falhas a mais, bloqueia por endereço e por utilizador
keys = ["ip:203.0.113.9", "user:x"]
assert not r.login_blocked(keys)
for _ in range(r.LOGIN_MAX_FAILS):
    r.login_failed(keys)
assert r.login_blocked(keys) and r.login_blocked(["ip:outro", "user:x"])

# Moeda de base: só códigos de três letras
r.set_base_currency("usd"); assert r.BASE_CURRENCY == "USD"
r.set_base_currency("dollars"); assert r.BASE_CURRENCY == "USD"
r.set_base_currency("EUR")
print("ok")
