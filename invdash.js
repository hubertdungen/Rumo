'use strict';
/* Painel «Investimentos» do dashboard: evolução, alocação, alvo contra real, quem sobe e quem desce,
   entradas contra mercado, concentração e uma leitura em frases. Só lê o que a app já tem
   (S.holdings, S.months, S.perf). Carregado depois do script principal. */

const ID = {mode: 'total'};

function invDayOf(h){
  const pi = h.price_info;
  if (!pi || !pi.ok || pi.prev_close == null || !h.quantity) return null;
  const prevEur = pi.prev_close * (pi.fx_rate == null ? 1 : pi.fx_rate);
  const eurNow = h.current_value, eurPrev = prevEur * h.quantity;
  return {eur: eurNow - eurPrev, pct: eurPrev ? (eurNow / eurPrev - 1) * 100 : null};
}

function invMoverRows(list, key){
  const max = Math.max(1, ...list.map(r => Math.abs(r.pct)));
  return list.map(r => `<div class="idmov"${tipAttr(tipRows(r.label, [['Valor atual', eur(r.value)], ['Variação', `${r.eur >= 0 ? '+' : '−'}${eur(Math.abs(r.eur))} (${r.pct >= 0 ? '+' : ''}${r.pct.toFixed(1)}%)`], r.cat ? ['Categoria', escAttr(r.cat)] : null], null, r.pct >= 0 ? 'var(--good)' : 'var(--bad)'))}>
      <div class="row small" style="justify-content:space-between"><b>${escAttr(r.label)}</b><span style="color:${r.pct >= 0 ? 'var(--good)' : 'var(--bad)'};font-weight:700">${r.pct >= 0 ? '+' : ''}${r.pct.toFixed(1)}% <span class="muted">· ${r.eur >= 0 ? '+' : '−'}${eur(Math.abs(r.eur))}</span></span></div>
      <div class="gbar"><i style="width:${(Math.abs(r.pct) / max * 50).toFixed(1)}%;${r.pct >= 0 ? 'left:50%;background:var(--good)' : 'right:50%;background:var(--bad)'}"></i></div></div>`).join('');
}

function invDashCard(m, st){
  const col = v => v >= 0 ? 'var(--good)' : 'var(--bad)';
  const cats = m.investimentos.filter(i => i.active);
  const catColor = (i) => CAT_COLORS[(i * 3 + 3) % CAT_COLORS.length];
  const totalVal = st.value || 1;

  // 1. evolução
  const perf = S.perf[S.perfRange];
  let chart;
  if (perf === 'loading' || perf === undefined) chart = '<div class="sub" style="padding:30px 0;text-align:center">a carregar o histórico…</div>';
  else if (!perf.values || perf.values.length < 2) chart = '<div class="sub" style="padding:30px 0;text-align:center">sem histórico suficiente para estas posições.</div>';
  else {
    const ds = downsample(perf.labels.map(d => d.slice(8, 10) + '/' + d.slice(5, 7)), perf.values);
    const first = perf.values[0], last = perf.values[perf.values.length - 1], d = last - first;
    const hi = Math.max(...perf.values), lo = Math.min(...perf.values);
    chart = lineChart(ds.labels.map((l, i) => i % 4 === 0 ? l : ''), [{values: ds.values, color: 'var(--purple)', name: 'Valor'}], {h: 190, tipLabels: ds.labels})
      + `<div class="idstats"><span>no período <b style="color:${col(d)}">${d >= 0 ? '▲' : '▼'} ${eur(Math.abs(d))} (${first ? fmtPct(d / first) : '0'}%)</b></span><span>máximo <b>${eur(hi)}</b></span><span>mínimo <b>${eur(lo)}</b></span></div>`;
  }

  // 2. alocação
  const allocRows = cats.map((c, i) => ({c, val: st.byCat[c.name] || 0, color: catColor(i)})).sort((a, b) => b.val - a.val);
  const donutSvg = donut(allocRows.filter(r => r.val > 0).map(r => ({value: r.val, color: r.color,
    tip: tipRows(r.c.name, [['Valor', eur(r.val)], ['Peso real', (r.val / totalVal * 100).toFixed(1) + '%'], ['Alvo', ((r.c.pct || 0) * 100).toFixed(1) + '%']], null, r.color)})), {size: 170, stroke: 26});
  const alloc = `<div class="idalloc"><div class="iddonut">${donutSvg}<div class="idcenter"><small>total</small><b>${eur(st.value)}</b></div></div>
    <div class="idlegend">${allocRows.map(r => `<div${tipAttr(tipRows(r.c.name, [['Valor', eur(r.val)], ['Peso', (r.val / totalVal * 100).toFixed(1) + '%']], null, r.color))}><i style="background:${r.color}"></i><span>${escAttr(r.c.name)}</span><b>${(r.val / totalVal * 100).toFixed(0)}%</b></div>`).join('')}</div></div>`;

  // 3. alvo contra real
  const targets = allocRows.map(r => {
    const actual = r.val / totalVal * 100, target = (r.c.pct || 0) * 100, diff = actual - target;
    const tone = Math.abs(diff) < 3 ? 'var(--dim)' : diff > 0 ? 'var(--gold)' : 'var(--blue)';
    return `<div class="idtgt"${tipAttr(tipRows(r.c.name, [['Valor', eur(r.val)], ['Peso real', actual.toFixed(1) + '%'], ['Alvo', target.toFixed(1) + '%'], ['Desvio', (diff >= 0 ? '+' : '') + diff.toFixed(1) + ' p.p.']], Math.abs(diff) < 3 ? 'Perto do alvo.' : diff > 0 ? 'Acima do alvo: as próximas entradas podem ir para outras categorias.' : 'Abaixo do alvo: boa categoria para reforçar.', r.color))}>
      <div class="row small" style="justify-content:space-between;margin-bottom:3px"><span><i class="dot" style="background:${r.color}"></i> ${escAttr(r.c.name)}</span><span class="muted">${eur(r.val)} · <b>${actual.toFixed(0)}%</b> de ${target.toFixed(0)}% <span class="iddiff" style="color:${tone};border-color:${tone}">${diff >= 0 ? '+' : '−'}${Math.abs(diff).toFixed(0)} p.p.</span></span></div>
      <div class="progress" style="position:relative"><div style="width:${Math.min(100, actual).toFixed(1)}%;background:${r.color}"></div>
        <div style="position:absolute;top:-2px;bottom:-2px;left:${Math.min(100, target).toFixed(1)}%;width:2px;background:var(--txt);opacity:.75"></div></div></div>`;
  }).join('');

  // 4. quem sobe e quem desce (desde a compra, ou hoje)
  const rows = S.holdings.map(h => {
    const label = h.ticker || h.name || '—';
    const cat = (cats.find(c => c.id === h.investment_id) || {}).name;
    if (ID.mode === 'day'){ const d = invDayOf(h); return d && d.pct != null ? {label, cat, value: h.current_value || 0, eur: d.eur, pct: d.pct} : null; }
    return h.cost_basis && h.gain_loss_pct != null ? {label, cat, value: h.current_value || 0, eur: h.gain_loss || 0, pct: h.gain_loss_pct} : null;
  }).filter(Boolean).sort((a, b) => b.pct - a.pct);
  const ups = rows.filter(r => r.pct >= 0).slice(0, 4), downs = rows.filter(r => r.pct < 0).slice(-4).reverse();
  const movers = `<div class="seg idseg" role="tablist"><button type="button" role="tab" class="${ID.mode === 'total' ? 'on' : ''}" data-idmode="total">Desde a compra</button><button type="button" role="tab" class="${ID.mode === 'day' ? 'on' : ''}" data-idmode="day">Hoje</button></div>
    ${rows.length ? `<div class="idcols"><div><div class="idh" style="color:var(--good)">▲ Mais sobem</div>${ups.length ? invMoverRows(ups) : '<div class="sub">nenhuma a subir</div>'}</div>
      <div><div class="idh" style="color:var(--bad)">▼ Mais descem</div>${downs.length ? invMoverRows(downs) : '<div class="sub">nenhuma a descer</div>'}</div></div>` : '<div class="sub" style="margin-top:12px">Sem posições com cotação e custo para comparar.</div>'}`;

  // 5. entradas contra mercado
  const hist = S.months.slice(0, S.currentIdx + 1).filter(x => !x.historical_import).slice(-12);
  const flows = hist.map(x => x.investimentos.reduce((a, i) => a + (i.contributed_this_month || 0), 0));
  const flowSum = flows.reduce((a, v) => a + v, 0);
  const gain = st.quoted - st.cost;
  const costShare = st.quoted ? Math.min(100, st.cost / st.quoted * 100) : 100;
  const flowBlock = `<div class="split-bar" style="height:24px;margin:8px 0"><div style="width:${costShare.toFixed(1)}%;background:var(--purple)">${costShare > 25 ? 'investido ' + eur(st.cost) : ''}</div>${gain > 0 ? `<div style="width:${(100 - costShare).toFixed(1)}%;background:var(--good)">${100 - costShare > 12 ? '+' + eur(gain) : ''}</div>` : ''}</div>
    <div class="small muted">${gain >= 0 ? `Do valor com cotação, <b>${eur(st.cost)}</b> é dinheiro teu e <b style="color:var(--good)">${eur(gain)}</b> foi o mercado a render.` : `O valor com cotação está <b style="color:var(--bad)">${eur(-gain)}</b> abaixo do que investiste.`}</div>
    <h3 class="idh2">Entradas mensais para investir <small>${flowSum ? `${eur(flowSum / Math.max(1, flows.length))} em média` : ''}</small></h3>${perfBars(hist.map(perfMonthLabel), flows, 'Entrada')}`;

  // 6. concentração e moedas
  const byVal = S.holdings.filter(h => (h.current_value || 0) > 0).sort((a, b) => b.current_value - a.current_value);
  const top1 = byVal[0] ? byVal[0].current_value / totalVal * 100 : 0, top3 = byVal.slice(0, 3).reduce((a, h) => a + h.current_value, 0) / totalVal * 100;
  const cur = {};
  S.holdings.forEach(h => { const c = (h.price_info && h.price_info.currency) || 'EUR'; cur[c] = (cur[c] || 0) + (h.current_value || 0); });
  const curRows = Object.entries(cur).sort((a, b) => b[1] - a[1]);
  const conc = `<div class="idkpis"><div><small>Posições</small><b>${S.holdings.length}</b></div><div><small>Maior posição</small><b style="color:${top1 > 30 ? 'var(--gold)' : 'inherit'}">${top1.toFixed(0)}%</b></div><div><small>3 maiores</small><b>${top3.toFixed(0)}%</b></div></div>
    ${byVal.slice(0, 5).map((h, i) => `<div class="idbar"${tipAttr(tipRows(h.ticker || h.name, [['Valor', eur(h.current_value)], ['Peso', (h.current_value / totalVal * 100).toFixed(1) + '%']], null, 'var(--purple)'))}><span>${escAttr(h.ticker || h.name)}</span><i><u style="width:${(h.current_value / byVal[0].current_value * 100).toFixed(1)}%"></u></i><b>${(h.current_value / totalVal * 100).toFixed(0)}%</b></div>`).join('')}
    <h3 class="idh2">Moedas</h3>${curRows.map(([c, v]) => `<div class="idbar"${tipAttr(tipRows(c, [['Valor', eur(v)], ['Peso', (v / totalVal * 100).toFixed(1) + '%']], null, 'var(--blue)'))}><span>${escAttr(c)}</span><i><u style="width:${(v / totalVal * 100).toFixed(1)}%;background:var(--blue)"></u></i><b>${(v / totalVal * 100).toFixed(0)}%</b></div>`).join('')}`;

  // 7. leitura em frases
  const tips = [];
  const over = allocRows.map(r => ({n: r.c.name, d: r.val / totalVal * 100 - (r.c.pct || 0) * 100})).sort((a, b) => b.d - a.d);
  if (over.length > 1 && over[0].d >= 5) tips.push(['⚖️', `<b>${escAttr(over[0].n)}</b> pesa ${over[0].d.toFixed(0)} p.p. acima do alvo: as próximas entradas rendem mais noutras categorias.`]);
  if (over.length > 1 && over[over.length - 1].d <= -5) tips.push(['🎯', `<b>${escAttr(over[over.length - 1].n)}</b> está ${Math.abs(over[over.length - 1].d).toFixed(0)} p.p. abaixo do alvo: é a melhor candidata a receber o próximo reforço.`]);
  if (top1 > 30) tips.push(['🧺', `<b>${escAttr(byVal[0].ticker || byVal[0].name)}</b> é ${top1.toFixed(0)}% da carteira: uma posição só a pesar tanto aumenta o risco.`]);
  const all = S.holdings.filter(h => h.cost_basis && h.gain_loss_pct != null).sort((a, b) => b.gain_loss_pct - a.gain_loss_pct);
  if (all.length > 1) tips.push(['🏆', `Melhor posição: <b>${escAttr(all[0].ticker || all[0].name)}</b> (${all[0].gain_loss_pct >= 0 ? '+' : ''}${all[0].gain_loss_pct.toFixed(0)}%). Pior: <b>${escAttr(all[all.length - 1].ticker || all[all.length - 1].name)}</b> (${all[all.length - 1].gain_loss_pct >= 0 ? '+' : ''}${all[all.length - 1].gain_loss_pct.toFixed(0)}%).`]);
  if (st.dayPct != null && Math.abs(st.dayPct) >= 1) tips.push([st.dayDelta >= 0 ? '📈' : '📉', `Hoje a carteira ${st.dayDelta >= 0 ? 'subiu' : 'desceu'} ${Math.abs(st.dayPct).toFixed(1)}% (${eur(Math.abs(st.dayDelta))}).`]);
  if (st.value - st.quoted > 0.5) tips.push(['✍️', `${eur(st.value - st.quoted)} estão em valores manuais, sem ganho nem perda calculável: só contam para o total.`]);
  const read = tips.length ? `<div class="idtips">${tips.map(([i, t]) => `<div><span>${i}</span><p>${t}</p></div>`).join('')}</div>` : '<div class="sub">Sem alertas: a carteira está perto do alvo.</div>';

  return `
    <div class="card section-gap idcard">
      <div class="flexrow">
        <h2 style="margin:0">Investimentos</h2>
        <div class="row" style="gap:10px"><span class="sub">${pricesAgeLabel()}</span><button id="btnGoInvestDash" class="small" onclick="document.querySelector('.tabs button[data-tab=invest]').click()">abrir Investir →</button></div>
      </div>
      ${investKpis(st)}
      <div class="idgrid">
        <div class="idpanel"><div class="flexrow"><h3 class="idh2" style="margin:0">Evolução</h3><div class="row small" style="gap:6px">${Object.entries(PERF_RANGES).map(([k, lbl]) => `<button class="pill f-perf-range ${S.perfRange === k ? 'good' : ''}" data-range="${k}" style="cursor:pointer">${lbl}</button>`).join('')}</div></div>${chart}
          <div class="small muted">Reavalia as posições que tens <b>hoje</b> a preços passados.</div></div>
        <div class="idpanel"><h3 class="idh2" style="margin-top:0">Alocação</h3>${alloc}</div>
      </div>
      <div class="idgrid">
        <div class="idpanel"><h3 class="idh2" style="margin-top:0">Alvo contra real</h3>${targets || '<div class="sub">sem categorias</div>'}<div class="small muted" style="margin-top:6px">A barra é o peso real; o traço é a tua percentagem-alvo.</div></div>
        <div class="idpanel"><h3 class="idh2" style="margin-top:0">Quem sobe e quem desce</h3>${movers}</div>
      </div>
      <div class="idgrid">
        <div class="idpanel"><h3 class="idh2" style="margin-top:0">Entradas contra mercado</h3>${flowBlock}</div>
        <div class="idpanel"><h3 class="idh2" style="margin-top:0">Concentração</h3>${conc}</div>
      </div>
      <div class="idpanel" style="margin-top:12px"><h3 class="idh2" style="margin-top:0">Leitura rápida</h3>${read}</div>
    </div>`;
}

document.addEventListener('click', e => {
  const b = e.target.closest('[data-idmode]');
  if (b){ ID.mode = b.dataset.idmode; renderDashboard(); }
});
