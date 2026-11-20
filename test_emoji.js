// Todos os nomes que existiam a 2026-10-17 têm um emoji próprio: node test_emoji.js [-v]
const assert = require('assert');
const { autoEmoji } = require('./emoji.js');

const names = `-|Acessórios|Adobe|Hobbies|Barbeiro|CARRO|CASA|CRÉDITOS|Calças|Camisas|Cartão|Casa|Claude|Combustível|
Comer Fora (RV)|Comer Fora (ST)|Compras Online|Consultas & Tratamentos|Conta Santander|Creatina|DESPESAS GER.|DESPORTO|DIY|
Design|Devoluções/Reembolsos|Diversos|DÍVIDAS|ENTRETENIMENTO|ESTADO|FACULDADE|Telemóvel|Google One / Gemini|Histórico|
INFORMÁTICA|Impostos/Taxas|Impressora 3D|Impressão 3D|Imprevistos|Investimento|Investimentos|Jogos|LUCROS|Levantmt. Númerário|
Manutenção|Manutenção Desktop|Manutenção Portátil|Material Desporto|Material Fac.|Medicação|Mensalidades Desp.|MidJourney|
Multas|OLX|OUTROS|Olx|OpenAi|OpenAi API|Outras Despesas|Outros|Poupanças Gerais|Prendas|Propinas|Proteína|
Queijos e Vinho|REDE|REVOLUT|ROUPA|Reserva|Revolut|Roupa Desportiva|Roupa Geral|SANTANDER|SAÚDE|SUBSCRIÇÕES|
Santander — mensalidade|Sapatos/Botas|Saídas à noite|Softwares|Spotify|Supermercado|T-Shirts|TRIBUNAL|Parceiro(a) — mensalidade|
Tlm + Internet|Transf. MB Way|Transferências|Transportes|Transportes |Upgrades|Vendas Online|ViaVerde|X-56 Joystick|
Despesas (total importado)|Eletricidade|Água|Gás|Saúde|Manut. Carro|Férias|Gadgets|DungenLabs|Informática|AI|Entretenimento|Estudos|
Filhos|Bens|Crypto|Acções|Bot|Geral`.split('|').map(s => s.replace(/\n/g, ''));

const missing = names.filter(n => autoEmoji(n, null) === null);
assert.deepStrictEqual(missing, [], 'sem emoji: ' + missing.join(', '));

const expect = {
  'Manutenção Portátil': '💻', 'Manutenção Desktop': '🖥️', 'LUCROS': '💵', 'Roupa Desportiva': '🎽', 'Manut. Carro': '🚗', 'Manutenção': '🔧', 'Hobbies': '🎯', 'AI': '🤖',
  'Mensalidades Desp.': '🏋️', 'Material Fac.': '🎓', 'Santander — mensalidade': '💳',
  'Conta Santander': '🏦', 'Google One / Gemini': '☁️', 'Gadgets': '🎒', 'Acções': '📈', 'Propinas': '🎓',
  'Levantmt. Númerário': '🏧', 'Transf. MB Way': '📲', 'Transferências': '🔄', 'Outras Despesas': '📎', 'Vendas Online': '🏷️', 'Compras Online': '📦', '-': '➖', 'DESPESAS GER.': '🧾',
};
// Em inglês também, sem roubar palavras portuguesas parecidas.
Object.assign(expect, {
  'Groceries': '🛒', 'Gym': '🏋️', 'Rent': '🏠', 'Car insurance': '🚗', 'Netflix': '📺', 'Tuition fees': '🎓',
  'Eating out': '🍽️', 'Night out': '🍻', 'Phone bill': '📱', 'Savings': '💰', 'Holidays': '✈️', 'Gifts': '🎁',
  'T-shirt': '👕', 'Shirts': '👔', 'Credit card': '💳', 'Refunds': '↩️', 'Salary': '💵', 'Misc': '🎲',
  'Cartão': '💳', 'Ordenado': '💵',
  'Eletricidade': '💡', 'Luz': '💡', 'EDP': '💡', 'Electricity': '💡', 'Água': '💧', 'EPAL': '💧', 'Water': '💧',
  'Gás': '🔥', 'Condomínio': '🏢', 'Seguro Casa': '🛡️', 'Limpeza': '🧹', 'Lixo': '🗑️', 'Renda': '🏠', 'Combustível': '⛽', 'Gas station': '⛽',
});
Object.entries({Talho:'🥩',Carne:'🥩',Carnes:'🥩',Peixe:'🐟',Fruta:'🍎',Legumes:'🥦',Padaria:'🥖',Leite:'🥛',Bebidas:'🥤',Gelados:'🍦',Mercearia:'🛒',Supermercado:'🛒'}).forEach(([n,e])=>assert.strictEqual(autoEmoji(n),e,n));
assert.notStrictEqual(autoEmoji('Cartão'), '🚗');                       // «car» não apanha «cartão»
assert.notStrictEqual(autoEmoji('Rentabilidade'), '🏠');                 // «rent» não apanha «rentabilidade»
for (const [name, emoji] of Object.entries(expect)) assert.strictEqual(autoEmoji(name), emoji, name);

if (process.argv.includes('-v')) for (const n of names) console.log(autoEmoji(n), n);
console.log('ok');
