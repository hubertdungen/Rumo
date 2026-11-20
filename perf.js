'use strict';
/* Desempenho: histórico e crescimento de cada conta, cofre e investimento, numa gaveta.
   Só lê o que a app já tem (S.months, S.holdings). Separa o «esperado» (saldo de abertura + o que se pôs de parte)
   do «real» (o valor que o utilizador escreve em «valor real no fim do mês anterior»): a diferença é o que
   rendeu ou se perdeu. Carregado depois do script principal, de que usa os helpers (eur, lineChart, tipRows…). */

const PF = {range: 12, group: 'all', sel: null};
const PF_RANGES = [[6, '6 meses'], [12, '12 meses'], [0, 'Tudo']];
const PF_GROUPS = [['all', 'Tudo'], ['accounts', 'Contas'], ['pots', 'Cofres'], ['inv', 'Investimentos']];
const perfCol = v => v > 0.005 ? 'var(--good)' : v < -0.005 ? 'var(--bad)' : 'var(--dim)';
const perfSign = v => (v > 0.005 ? '+' : v < -0.005 ? '−' : '') + eur(Math.abs(v));
const perfMonthLabel = m => `${m.month_name} ${String(m.year).slice(2)}`;

function perfMonths(){
  const upto = S.months.slice(0, S.currentIdx + 1).filter(m => !m.historical_import);
  return PF.range ? upto.slice(-PF.range) : upto;
}

function perfItems(){
  const m = curMonth();
  if (!m) return [];
  const out = [];
  out.push({key: 'S', group: 'accounts', name: accLabel('S'), emoji: '🏦', color: 'var(--bad)', sub: 'o que sobra no fim do mês'});
  if (transferOn()) out.push({key: 'R', group: 'accounts', name: accLabel('R'), emoji: '💳', color: 'var(--rev)', sub: 'o que sobra no fim do mês'});
  if (jointOn()) out.push({key: 'joint', group: 'accounts', name: jointName(), emoji: '🏠', color: 'var(--gold)', sub: 'saldo da conta'});
  out.push({key: 'geral', group: 'pots', name: 'Poupança Geral', emoji: '🏛️', color: 'var(--accent)', sub: 'fundo livre'});
  m.pots.filter(p => p.active).forEach(p => out.push({key: 'pot:' + p.id, group: 'pots', name: p.name, emoji: emojiFor('pot', p.name, '💰'), color: 'var(--gold)', sub: ({1: 'fixo', 2: 'variável', 3: 'objetivo'})[Number(p.level)] || 'cofre'}));
  m.investimentos.filter(i => i.active).forEach(i => out.push({key: 'inv:' + i.id, group: 'inv', name: i.name, emoji: emojiFor('inv', i.name, '📊'), color: 'var(--purple)', sub: 'investimento'}));
  return out;
}

// Uma série por item: saldo a cada mês, entradas do mês e os valores reais registados (esperado contra real).
function perfSeries(key, months){
  const [kind, id] = key.split(':');
  const se = {labels: months.map(perfMonthLabel), bal: [], add: [], checks: [], dev: []};
  months.forEach((m, i) => {
    const gi = S.months.indexOf(m), prev = gi > 0 ? S.months[gi - 1] : null;
    let bal = null, add = null, chk = null, dev = null;
    if (kind === 'pot'){ const p = m.pots.find(x => String(x.id) === id); if (p){ bal = p.accumulated; add = p.contributed_this_month; } chk = (m.checks || {})['pot:' + id]; }
    else if (kind === 'inv'){ const p = m.investimentos.find(x => String(x.id) === id); if (p){ bal = p.accumulated; add = p.contributed_this_month; } chk = (m.checks || {})['inv:' + id]; }
    else if (kind === 'geral'){ bal = m.geral_acumulado; add = m.geral_this_month; chk = (m.checks || {})['geral:0']; }
    else if (kind === 'joint'){ bal = m.joint_account_acumulado; chk = (m.checks || {})['joint:0']; }
    else if (kind === 'S'){ bal = m.cc_livre; if (prev && !prev.historical_import) dev = m.santander_anterior - prev.cc_livre; }
    else if (kind === 'R'){ bal = m.livre_resto; if (prev && !prev.historical_import) dev = m.revolut_anterior - prev.livre_resto; }
    se.bal.push(bal); se.add.push(add);
    if (chk) se.checks.push({i, label: se.labels[i], expected: chk.expected, real: chk.real, diff: chk.real - chk.expected});
    se.dev.push(chk ? chk.real - chk.expected : dev);
  });
  return se;
}

function perfStats(se){
  const v = se.bal.filter(x => x != null);
  const first = v.length ? v[0] : 0, last = v.length ? v[v.length - 1] : 0;
  const adds = se.add.slice(1).reduce((a, x) => a + (x || 0), 0);
  const realGain = se.checks.reduce((a, c) => a + c.diff, 0);
  const monthly = [];
  for (let i = 1; i < se.bal.length; i++) if (se.bal[i] != null && se.bal[i - 1] != null) monthly.push({label: se.labels[i], d: se.bal[i] - se.bal[i - 1]});
  const best = monthly.length ? monthly.reduce((a, b) => b.d > a.d ? b : a) : null;
  const worst = monthly.length ? monthly.reduce((a, b) => b.d < a.d ? b : a) : null;
  const avgBal = v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0;
  return {first, last, delta: last - first, adds, other: last - first - adds, realGain, hasReal: se.checks.length > 0, best, worst, avgBal, n: v.length};
}

function perfSpark(values, color){
  const clean = values.map(x => x == null ? 0 : x);
  if (clean.length < 2) return '<svg width="90" height="28"></svg>';
  const {line} = sparklinePath(clean, 90, 28, 2);
  return `<svg width="90" height="28" viewBox="0 0 90 28"><path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round"/></svg>`;
}

// Barras com sinal (verde sobe, vermelho desce): cada uma com o seu cartão de informação.
function perfBars(labels, values, name){
  const w = 640, h = 120, padL = 40, padB = 20, padT = 8, n = labels.length;
  const vals = values.filter(v => v != null);
  if (!vals.some(v => Math.abs(v) > 0.005)) return '<div class="sub" style="padding:18px 0;text-align:center">sem valores para mostrar neste período.</div>';
  const max = Math.max(...vals.map(Math.abs), 1), zero = padT + (h - padT - padB) / 2, half = (h - padT - padB) / 2;
  const bw = Math.min(34, (w - padL - 8) / n * 0.62), step = (w - padL - 8) / n;
  let g = `<line x1="${padL}" x2="${w - 8}" y1="${zero}" y2="${zero}" stroke="var(--line)"/><text x="${padL - 6}" y="${padT + 8}" font-size="9" fill="var(--dim)" text-anchor="end">${compact(max)}</text><text x="${padL - 6}" y="${h - padB}" font-size="9" fill="var(--dim)" text-anchor="end">−${compact(max)}</text>`;
  labels.forEach((l, i) => {
    const cx = padL + step * i + step / 2, v = values[i];
    if (v != null){
      const bh = Math.max(1.5, Math.abs(v) / max * half), y = v >= 0 ? zero - bh : zero;
      g += `<rect class="pbar"${tipAttr(tipRows(l, [[name, perfSign(v)]], null, v >= 0 ? 'var(--good)' : 'var(--bad)'))} x="${(cx - bw / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="3" fill="${v >= 0 ? 'var(--good)' : 'var(--bad)'}"/>`;
    }
    if (n <= 12 || i % 2 === 0) g += `<text x="${cx.toFixed(1)}" y="${h - 6}" font-size="9.5" fill="var(--dim)" text-anchor="middle">${l}</text>`;
  });
  return `<svg class="barchart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
}

function perfKpi(label, value, color, sub){
  return `<div class="metric tile"><div class="lbl">${label}</div><div class="val"${color ? ` style="color:${color}"` : ''}>${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
}

// ---- «leitura»: o que os números querem dizer conforme o tipo de cofre ou de investimento ----
function perfReading(item, se, st){
  const [kind, id] = item.key.split(':');
  const months = Math.max(1, se.labels.length - 1);
  const real = st.hasReal ? `${perfSign(st.realGain)} face ao esperado` : null;
  const noReal = 'Ainda não há «valor real» registado: escreve-o no campo «valor real no fim do mês anterior» para a app medir o que rendeu ou se perdeu.';
  if (kind === 'pot'){
    const p = curMonth().pots.find(x => String(x.id) === id);
    const lv = Number(p && p.level) || 0;
    if (lv === 1){
      const yr = st.hasReal && st.avgBal > 0 ? st.realGain / st.avgBal * 12 / months * 100 : null;
      return ['📌 Cofre fixo', 'Guardado; não devia mexer. O que importa é conservar o valor e, se rende, quanto.',
        !st.hasReal ? noReal : st.realGain < -0.5 ? `Perdeu ${eur(-st.realGain)} face ao esperado: num cofre fixo isso significa que saiu dinheiro. Confirma se foi de propósito.`
          : `${st.realGain > 0.5 ? `Rendeu ${eur(st.realGain)} acima do esperado${yr == null ? '' : ` (cerca de ${fmtPct(yr / 100)}% ao ano)`}.` : 'Sem diferença face ao esperado: o valor manteve-se.'}`];
    }
    if (lv === 2){
      const spent = se.add.slice(1).filter(x => x != null && x < 0).reduce((a, x) => a + x, 0), top = se.add.slice(1).filter(x => x != null && x > 0).reduce((a, x) => a + x, 0);
      const avgSpend = -spent / months, runway = avgSpend > 0.5 ? st.last / avgSpend : null;
      return ['🔄 Cofre variável', 'Dinheiro posto para se ir gastando: sobe com os reforços e desce com o uso. O que importa é o saldo cobrir o consumo.',
        `Reforçado ${eur(top)} e gasto ${eur(-spent)} no período${avgSpend > 0.5 ? `; a este ritmo o saldo atual dá para ${runway < 1 ? 'menos de 1 mês' : Math.floor(runway) + (Math.floor(runway) === 1 ? ' mês' : ' meses')} de consumo` : ''}.${st.hasReal ? ` Valor real ${real}.` : ''}`];
    }
    if (lv === 3){
      const g = p && p.goal;
      let txt = 'Sem objetivo definido: indica o valor e a data no cofre para medir o ritmo.';
      if (g){
        const need = (() => { if (!p.goal_date) return null; const [gy, gm] = p.goal_date.split('-').map(Number), now = curMonth(); const left = (gy - now.year) * 12 + (gm - now.month); return left > 0 ? g.missing / left : null; })();
        txt = g.months_needed === 0 ? 'Objetivo atingido.' : `${g.pct.toFixed(0)}% do objetivo (${eur(g.have)} de ${eur(g.goal)}).${g.months_needed != null ? ` Ao ritmo atual faltam ${g.months_needed} meses.` : ' Sem entradas, não chega lá.'}${need != null ? ` Para cumprir a data precisavas de ${eur(need)} por mês; fazes ${eur(g.per_month)}.` : ''}`;
      }
      return ['🎯 Cofre com objetivo', 'Juntar até um valor, sem lhe tocar. O que importa é o ritmo contra a data.', txt + (st.hasReal ? ` Valor real ${real}.` : '')];
    }
    return ['💰 Cofre', 'Escolhe o tipo do cofre (fixo, variável ou objetivo) para uma leitura à medida.', st.hasReal ? `Valor real ${real}.` : noReal];
  }
  if (kind === 'geral') return ['🏛️ Poupança Geral', 'Acumula e nunca se tira: o que importa é a regularidade das entradas.', st.hasReal ? `Valor real ${real}.` : noReal];
  if (kind === 'inv'){
    const inv = curMonth().investimentos.find(x => String(x.id) === id), n = (inv && inv.name || '').toLowerCase();
    const hs = S.holdings.filter(h => String(h.investment_id) === id);
    const cost = hs.reduce((a, h) => a + (h.cost_basis || 0), 0), val = hs.reduce((a, h) => a + (h.current_value || 0), 0), gain = hs.filter(h => h.cost_basis).reduce((a, h) => a + (h.gain_loss || 0), 0);
    const type = /cript|crypto|bitcoin|btc|eth/.test(n) ? ['🪙 Cripto', 'Muito volátil: lê o ganho ou perda total, não o mês. Só conta o que consegues manter em baixas fortes.']
      : /a[cç][oõ]es|stock|etf|fundo|bolsa|index/.test(n) ? ['📈 Ações e fundos', 'Longo prazo: compara o valor com o investido e deixa o ruído de cada mês.']
      : /ouro|bens|gold|metal|prata|imob/.test(n) ? ['🪙 Bens e metais', 'Reserva de valor: ganhos pequenos são normais; o que conta é preservar o poder de compra.']
      : /bot|trad|auto/.test(n) ? ['🤖 Automático', 'Mede-se pelo que devolve ao investido; se não ganha de forma consistente, rever ou desligar.']
      : ['📊 Investimento', 'Compara o valor atual com o investido.'];
    const line = hs.length ? `Investido ${eur(cost)}, valor atual ${eur(val)}${cost ? `: ${perfSign(gain)} (${fmtPct(gain / cost)}%)` : ''}.` : 'Sem posições associadas: adiciona-as em Investir para ver o ganho ou a perda.';
    return [type[0], type[1], line + (st.hasReal ? ` Valor real ${real}.` : '')];
  }
  if (kind === 'joint') return ['🏠 Conta conjunta', 'Saldo partilhado: o que importa é cobrir as despesas da casa sem sobressaltos.', st.hasReal ? `Valor real ${real}.` : noReal];
  return ['🏦 Conta', 'Aqui mede-se o desvio: o que sobrou de verdade contra o que a app previa para o fim do mês anterior. Acima do previsto, gastaste menos; abaixo, mais.',
    st.n > 1 ? `Desvio acumulado no período: ${perfSign(se.dev.filter(x => x != null).reduce((a, x) => a + x, 0))}.` : ''];
}

function perfHoldings(id){
  const hs = S.holdings.filter(h => String(h.investment_id) === id);
  if (!hs.length) return '';
  const max = Math.max(1, ...hs.map(h => Math.abs(h.gain_loss || 0)));
  return `<div class="card" style="margin-top:12px"><h3 style="margin:0 0 8px;font-size:14px">Posições</h3>${hs.map(h => {
    const hasCost = !!h.cost_basis, g = h.gain_loss || 0;
    return `<div class="prow-h"${tipAttr(tipRows(h.ticker || h.name, [['Valor atual', eur(h.current_value || 0)], hasCost ? ['Investido', eur(h.cost_basis)] : null, hasCost ? ['Ganho / perda', perfSign(g)] : null, hasCost ? ['Variação', fmtPct((h.gain_loss_pct || 0) / 100) + '%'] : null], null, 'var(--purple)'))}>
      <div class="row small" style="justify-content:space-between"><span>${escAttr(h.ticker || h.name)}</span><span class="muted">${eur(h.current_value || 0)}${hasCost ? ` · <b style="color:${perfCol(g)}">${perfSign(g)} (${fmtPct((h.gain_loss_pct || 0) / 100)}%)</b>` : ' · valor manual'}</span></div>
      ${hasCost ? `<div class="gbar"><i style="width:${(Math.abs(g) / max * 50).toFixed(1)}%;${g >= 0 ? 'left:50%;background:var(--good)' : 'right:50%;background:var(--bad)'}"></i></div>` : ''}
    </div>`; }).join('')}</div>`;
}

function perfDetail(item, months){
  const se = perfSeries(item.key, months), st = perfStats(se), [kind, id] = item.key.split(':');
  const isAcc = kind === 'S' || kind === 'R';
  const [rt, rsub, rtxt] = perfReading(item, se, st);
  const pot = kind === 'pot' ? curMonth().pots.find(x => String(x.id) === id) : null;
  // com juros definidos, uma linha tracejada prolonga o saldo pelos próximos 12 meses
  let labels = se.labels.slice(), series = [{values: se.bal, color: item.color, name: isAcc ? 'Sobra' : 'Saldo'}];
  if (pot && pot.interest){
    const last = months[months.length - 1];
    const fut = pot.interest.projection.map((_, i) => { const t = last.month + i + 1; return `${String(['JAN','FEV','MAR','ABR','MAI','JUN','JUL','AGO','SET','OUT','NOV','DEZ'][(t - 1) % 12])} ${String((last.year + Math.floor((t - 1) / 12)) % 100).padStart(2, '0')}`; });
    labels = labels.concat(fut);
    const pad = se.bal.map(() => null); pad[pad.length - 1] = se.bal[se.bal.length - 1];
    series = [series[0], {values: pad.concat(pot.interest.projection), color: item.color, name: 'Previsão com juros', dashed: true}];
    series[0] = {...series[0], values: se.bal.concat(fut.map(() => null))};
  }
  const kpis = isAcc ? [
      perfKpi('Sobra este mês', eur(st.last), item.color, 'previsto pela app'),
      perfKpi('Desvio acumulado', perfSign(se.dev.filter(x => x != null).reduce((a, x) => a + x, 0)), perfCol(se.dev.filter(x => x != null).reduce((a, x) => a + x, 0)), 'real contra previsto'),
      perfKpi('Melhor mês', st.best ? `${perfSign(st.best.d)}` : '—', null, st.best ? st.best.label : ''),
      perfKpi('Pior mês', st.worst ? `${perfSign(st.worst.d)}` : '—', null, st.worst ? st.worst.label : ''),
    ] : [
      perfKpi('Saldo', eur(st.last), item.color, `${perfSign(st.delta)} no período`),
      perfKpi('Esperado (entradas)', perfSign(st.adds), null, 'o que se pôs de parte'),
      perfKpi('Real contra esperado', st.hasReal ? perfSign(st.realGain) : '—', st.hasReal ? perfCol(st.realGain) : null, st.hasReal ? 'rendeu ou perdeu' : 'sem valor real'),
      perfKpi('Melhor / pior mês', st.best ? `${perfSign(st.best.d)}` : '—', null, st.worst ? `pior ${perfSign(st.worst.d)} (${st.worst.label})` : ''),
    ];
  const barsTitle = isAcc ? 'Desvio de cada mês: real contra previsto' : 'Real contra esperado, por mês';
  return `
    <div class="flexrow" style="align-items:flex-end"><div><div class="small muted">${item.emoji} ${escAttr(item.sub)}</div><h2 style="margin:0">${escAttr(item.name)}</h2></div>
      <div style="text-align:right"><div style="font-size:26px;font-weight:800;color:${item.color}">${eur(st.last)}</div>
        <span class="delta ${st.delta >= 0 ? 'up' : 'down'}">${st.delta >= 0 ? '▲' : '▼'} ${eur(Math.abs(st.delta))}${st.first > 0.005 ? ` (${fmtPct(st.delta / st.first)}%)` : ''}</span></div></div>
    <div class="perf-read"><b>${rt}</b><span>${rsub}</span><p>${rtxt}</p></div>
    <div class="grid cols-4" style="margin-top:12px">${kpis.join('')}</div>
    <div class="card" style="margin-top:12px"><h3 style="margin:0 0 4px;font-size:14px">${isAcc ? 'Sobra no fim de cada mês' : 'Saldo ao longo do tempo'}</h3>
      ${se.bal.filter(x => x != null).length > 1 ? lineChart(labels.map((l, i) => i % 2 === 0 || labels.length < 9 ? l : ''), series, {h: 210, tipLabels: labels}) : '<div class="sub" style="padding:18px 0;text-align:center">ainda há poucos meses para um gráfico.</div>'}
      ${pot && pot.interest ? `<div class="small muted">A tracejado: previsão a 12 meses com os juros definidos.</div>` : ''}</div>
    <div class="card" style="margin-top:12px"><h3 style="margin:0 0 4px;font-size:14px">${barsTitle}</h3>
      ${perfBars(se.labels, se.dev, 'Diferença')}
      <div class="small muted">${isAcc ? 'Verde: sobrou mais do que o previsto. Vermelho: sobrou menos.' : 'Cada barra é o «valor real» que escreveste contra o esperado (saldo de abertura + entradas): verde rendeu, vermelho perdeu. Meses sem barra não têm valor real registado.'}</div></div>
    ${!isAcc ? `<div class="card" style="margin-top:12px"><h3 style="margin:0 0 4px;font-size:14px">Entradas de cada mês</h3>${perfBars(se.labels, se.add, 'Entrada')}</div>` : ''}
    ${kind === 'inv' ? perfHoldings(id) : ''}`;
}

function perfOverview(months){
  const items = perfItems().filter(it => PF.group === 'all' || it.group === PF.group);
  const rows = items.map(it => { const se = perfSeries(it.key, months); return {it, se, st: perfStats(se)}; });
  const real = rows.filter(r => r.it.group !== 'accounts');
  const totalBal = real.reduce((a, r) => a + r.st.last, 0), totalGain = real.reduce((a, r) => a + r.st.realGain, 0), totalAdds = real.reduce((a, r) => a + r.st.adds, 0);
  const caps = months.map(m => m.capital), capDelta = caps.length ? caps[caps.length - 1] - caps[0] : 0;
  const hs = S.holdings.filter(h => h.cost_basis), mGain = hs.reduce((a, h) => a + (h.gain_loss || 0), 0), mCost = hs.reduce((a, h) => a + h.cost_basis, 0);
  const maxAbs = Math.max(1, ...rows.map(r => Math.abs(r.st.hasReal || r.it.group === 'accounts' ? (r.it.group === 'accounts' ? r.se.dev.filter(x => x != null).reduce((a, x) => a + x, 0) : r.st.realGain) : r.st.delta)));
  return `
    <div class="grid cols-4">
      ${perfKpi('Capital', eur(caps[caps.length - 1] || 0), 'var(--accent)', `${perfSign(capDelta)} no período`)}
      ${perfKpi('Posto de parte', perfSign(totalAdds), null, 'cofres, geral e investimentos')}
      ${perfKpi('Rendeu / perdeu (real)', totalGain || real.some(r => r.st.hasReal) ? perfSign(totalGain) : '—', perfCol(totalGain), 'valor real contra esperado')}
      ${perfKpi('Investimentos (mercado)', hs.length ? perfSign(mGain) : '—', perfCol(mGain), hs.length && mCost ? `${fmtPct(mGain / mCost)}% sobre ${eur(mCost)}` : 'sem posições')}
    </div>
    <div class="card" style="margin-top:12px"><h3 style="margin:0 0 4px;font-size:14px">Capital ao longo do tempo</h3>
      ${caps.length > 1 ? lineChart(months.map((m, i) => i % 2 === 0 || months.length < 9 ? perfMonthLabel(m) : ''), [{values: caps, color: 'var(--accent)', name: 'Capital'}, {values: months.map(m => m.poupanca_acumulada), color: 'var(--gold)', name: 'Poupado e investido'}], {h: 200, tipLabels: months.map(perfMonthLabel)}) : '<div class="sub" style="padding:18px 0;text-align:center">ainda há poucos meses para um gráfico.</div>'}</div>
    <div class="perf-chips" style="margin-top:12px">${PF_GROUPS.map(([k, n]) => `<button type="button" class="${PF.group === k ? 'on' : ''}" data-pgroup="${k}">${n}</button>`).join('')}</div>
    <div class="perf-list">${rows.map(({it, se, st}) => {
      const acc = it.group === 'accounts', main = acc ? se.dev.filter(x => x != null).reduce((a, x) => a + x, 0) : (st.hasReal ? st.realGain : null);
      return `<button type="button" class="prow" data-psel="${it.key}">
        <span class="pname"><span class="pemo">${it.emoji}</span><span><b>${escAttr(it.name)}</b><small>${escAttr(it.sub)}</small></span></span>
        ${perfSpark(se.bal, it.color)}
        <span class="pnum"><b>${eur(st.last)}</b><small style="color:${perfCol(st.delta)}">${perfSign(st.delta)} no período</small></span>
        <span class="pgain"><b style="color:${main == null ? 'var(--dim)' : perfCol(main)}">${main == null ? '—' : perfSign(main)}</b><small>${acc ? 'desvio real/previsto' : main == null ? 'sem valor real' : 'real contra esperado'}</small>
          <i class="gb"><u style="width:${main == null ? 0 : (Math.abs(main) / maxAbs * 100).toFixed(1)}%;background:${main == null ? 'transparent' : perfCol(main)}"></u></i></span>
      </button>`; }).join('') || '<div class="sub">nada para mostrar.</div>'}</div>
    <div class="small muted" style="margin-top:10px">«Real contra esperado» usa o valor que escreves em «valor real no fim do mês anterior»: a app compara-o com o que previa (saldo de abertura + o que puseste de parte) e a diferença é o que rendeu ou se perdeu. Vai ganhando conhecimento a cada mês em que o registas.</div>`;
}

function renderPerformance(){
  const el = document.querySelector('.perf');
  if (!el) return;
  const months = perfMonths(), items = perfItems();
  const item = PF.sel && items.find(i => i.key === PF.sel);
  el.innerHTML = `
    <header><div class="perf-title">${item ? '<button type="button" class="ghost icon-btn" data-pback aria-label="voltar">←</button>' : ''}<h2 style="margin:0">${item ? 'Desempenho' : 'Desempenho detalhado'}</h2></div>
      <button type="button" class="ghost icon-btn" data-pclose aria-label="fechar">✕</button></header>
    <div class="perf-chips">${PF_RANGES.map(([n, l]) => `<button type="button" class="${PF.range === n ? 'on' : ''}" data-prange="${n}">${l}</button>`).join('')}</div>
    <div class="perf-body">${months.length < 2 ? '<div class="sub">Ainda há poucos meses registados para analisar o crescimento.</div>' : item ? perfDetail(item, months) : perfOverview(months)}</div>`;
}

function openPerformance(kind, id){
  PF.sel = kind && kind !== 'overview' ? (id != null ? `${kind}:${id}` : kind) : null;
  let back = document.querySelector('.perf-back');
  if (!back){
    back = document.createElement('div');
    back.className = 'perf-back';
    back.innerHTML = '<aside class="perf" role="dialog" aria-modal="true" aria-label="Desempenho"></aside>';
    document.body.appendChild(back);
    const close = () => { back.classList.remove('on'); setTimeout(() => back.remove(), 220); document.removeEventListener('keydown', onKey); };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', e => {
      const t = e.target;
      if (t === back || t.closest('[data-pclose]')) return close();
      if (t.closest('[data-pback]')){ PF.sel = null; return renderPerformance(); }
      const sel = t.closest('[data-psel]'); if (sel){ PF.sel = sel.dataset.psel; renderPerformance(); back.querySelector('.perf').scrollTop = 0; return; }
      const rg = t.closest('[data-prange]'); if (rg){ PF.range = Number(rg.dataset.prange); return renderPerformance(); }
      const gp = t.closest('[data-pgroup]'); if (gp){ PF.group = gp.dataset.pgroup; return renderPerformance(); }
    });
    requestAnimationFrame(() => back.classList.add('on'));
  }
  renderPerformance();
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-perf]');
  if (b){ const [k, i] = b.dataset.perf.split(':'); openPerformance(k, i); }
});
