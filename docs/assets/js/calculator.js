/* Cost calculator for CXRReportGen on Azure. All compute is client-side. */
(function () {
  const ids = [
    'region','studies_day','images_study','dicom_mb','png_kb',
    'hot_days','cool_days','archive_days','phi_deid',
    'sku','instance_count','autoscale_max',
    'p95_latency_ms','target_throughput',
    'reservation','hours_month',
    'pe_count','egress_gb','expressroute',
    'foundry_addons','aisearch_tier','cosmos_rus','la_gb_day',
    'support_plan','defender'
  ];
  let pricing = null;

  async function loadPricing() {
    try {
      const r = await fetch('assets/data/pricing.json', { cache: 'no-cache' });
      pricing = await r.json();
      const stamp = document.getElementById('rates-stamp');
      if (stamp) stamp.textContent = `Rates verified ${pricing._meta.last_verified}`;
      hydrate();
      compute();
    } catch (e) {
      console.error(e);
      document.getElementById('rates-stamp').textContent = 'Could not load pricing.json';
    }
  }

  function hydrate() {
    const regionSel = document.getElementById('region');
    Object.entries(pricing.regions).forEach(([id, r]) => {
      const o = document.createElement('option');
      o.value = id; o.textContent = `${r.label} (×${r.modifier.toFixed(2)})`;
      regionSel.appendChild(o);
    });
    const skuSel = document.getElementById('sku');
    Object.keys(pricing.compute_hourly).forEach(s => {
      const o = document.createElement('option');
      o.value = s; o.textContent = `${s}  ($${pricing.compute_hourly[s].toFixed(2)}/hr)`;
      skuSel.appendChild(o);
    });
    const supSel = document.getElementById('support_plan');
    Object.entries(pricing.support_plan_month).forEach(([k, v]) => {
      const o = document.createElement('option');
      o.value = k; o.textContent = `${k} ($${v}/mo)`;
      supSel.appendChild(o);
    });
    const aiSel = document.getElementById('aisearch_tier');
    ['none','basic','s1','s2'].forEach(t => {
      const o = document.createElement('option');
      o.value = t; o.textContent = t.toUpperCase();
      aiSel.appendChild(o);
    });
    const resSel = document.getElementById('reservation');
    Object.entries(pricing.reservation_discount).forEach(([k, v]) => {
      const o = document.createElement('option');
      o.value = k; o.textContent = `${k} (${Math.round(v * 100)}% off)`;
      resSel.appendChild(o);
    });

    // Restore from localStorage
    const saved = JSON.parse(localStorage.getItem('cxr_calc') || '{}');
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      if (id in saved) {
        if (el.type === 'checkbox') el.checked = !!saved[id];
        else el.value = saved[id];
      }
      el.addEventListener('input', () => { save(); compute(); });
      el.addEventListener('change', () => { save(); compute(); });
    });
  }

  function readInputs() {
    const get = id => document.getElementById(id);
    const num = id => parseFloat(get(id).value || 0);
    const int = id => parseInt(get(id).value || 0, 10);
    const chk = id => get(id).checked;
    const str = id => get(id).value;
    return {
      region: str('region') || 'eastus',
      studies_day: int('studies_day'),
      images_study: int('images_study'),
      dicom_mb: num('dicom_mb'),
      png_kb: num('png_kb'),
      hot_days: int('hot_days'),
      cool_days: int('cool_days'),
      archive_days: int('archive_days'),
      phi_deid: chk('phi_deid'),
      sku: str('sku'),
      instance_count: int('instance_count'),
      autoscale_max: int('autoscale_max'),
      p95_latency_ms: int('p95_latency_ms'),
      target_throughput: int('target_throughput'),
      reservation: str('reservation') || 'none',
      hours_month: int('hours_month'),
      pe_count: int('pe_count'),
      egress_gb: num('egress_gb'),
      expressroute: chk('expressroute'),
      foundry_addons: chk('foundry_addons'),
      aisearch_tier: str('aisearch_tier') || 'none',
      cosmos_rus: int('cosmos_rus'),
      la_gb_day: num('la_gb_day'),
      support_plan: str('support_plan') || 'none',
      defender: chk('defender')
    };
  }

  function save() {
    const data = {};
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      data[id] = el.type === 'checkbox' ? el.checked : el.value;
    });
    localStorage.setItem('cxr_calc', JSON.stringify(data));
  }

  function calc(input, sku, instanceCount, hoursMonth, regionMod) {
    const inferences_month   = input.studies_day * 30;
    const dicom_gb_month     = (input.studies_day * input.images_study * input.dicom_mb * 30) / 1024;
    const png_gb_month       = (input.studies_day * input.images_study * input.png_kb * 30) / 1024 / 1024;
    const findings_gb_month  = (input.studies_day * 30 * 5) / 1024 / 1024;
    const new_gb_month       = dicom_gb_month + png_gb_month + findings_gb_month;

    // Storage tiering: assume each month's data lives proportionally in each tier through retention.
    const totalRetentionDays = Math.max(1, input.hot_days + input.cool_days + input.archive_days);
    const hotGb     = new_gb_month * (input.hot_days     / 30);
    const coolGb    = new_gb_month * (input.cool_days    / 30);
    const archiveGb = new_gb_month * (input.archive_days / 30);

    const sp = pricing.storage_per_gb_month;
    const storage =
      hotGb * sp.hot + coolGb * sp.cool + archiveGb * sp.archive +
      (inferences_month * input.images_study * 4 / 10000) * sp.transactions_per_10k;

    // Compute
    const hourly = pricing.compute_hourly[sku] || 0;
    const reservation = pricing.reservation_discount[input.reservation] || 0;
    const compute = instanceCount * hoursMonth * hourly * (1 - reservation);

    // Networking
    const net =
      input.pe_count * 730 * pricing.networking.private_endpoint_hour +
      input.egress_gb * pricing.networking.egress_per_gb +
      (input.expressroute ? pricing.networking.expressroute_port_month + pricing.networking.expressroute_circuit_month : 0);

    // Fixed dependencies
    const acr = pricing.acr_standard_month;
    const kv = (inferences_month * 2 / 10000) * pricing.key_vault_per_10k_ops;
    const appInsights = input.la_gb_day * 30 * pricing.app_insights_per_gb;
    const logAnalytics = input.la_gb_day * 30 * pricing.log_analytics_per_gb;

    // Foundry add-ons
    let aiSearch = 0, cosmos = 0;
    if (input.foundry_addons) {
      const tier = input.aisearch_tier;
      if (tier !== 'none') aiSearch = pricing.ai_search[`${tier}_month`] || 0;
      cosmos = (input.cosmos_rus / 100) * 730 * pricing.cosmos_db.ru_per_100_per_hour + 1 * pricing.cosmos_db.storage_per_gb_month;
    }

    // DICOM receiver pipeline: AHDS DICOM service + Service Bus (sessions queue) + Event Grid (system topic)
    let hds = 0, sb = 0, eg = 0;
    if (input.phi_deid) {
      // AHDS DICOM service: storage + transactions. (De-id is consumption-based and not modeled here.)
      const dicom_tx_per_instance = 6; // store + retrieve metadata + WADO + change-feed events
      hds =
        dicom_gb_month * pricing.azure_health_data_services.dicom_storage_per_gb_month +
        (inferences_month * input.images_study * dicom_tx_per_instance / 10000) * pricing.azure_health_data_services.dicom_per_10k_transactions;

      // Service Bus Standard sessions queue: flat base + per-million ops above the included free tier
      const sb_ops = inferences_month * input.images_study * 2; // 1 enqueue (from EG subscription) + 1 dequeue (worker)
      const sb_ops_millions = sb_ops / 1_000_000;
      const sb_billable_millions = Math.max(0, sb_ops_millions - (pricing.service_bus.free_ops_per_million || 0));
      sb = pricing.service_bus.standard_base_month + sb_billable_millions * pricing.service_bus.standard_ops_per_million;

      // Event Grid system topic: 1 event per arriving DICOM instance; first 100k ops/month free
      const eg_ops = inferences_month * input.images_study;
      const eg_billable = Math.max(0, eg_ops - (pricing.event_grid.free_ops_per_month || 0));
      eg = (eg_billable / 1_000_000) * pricing.event_grid.ops_per_million;
    }

    // Container Apps DICOM worker: reads SB sessions, pulls DICOM from AHDS, posts to AML endpoint
    // (assume 2 vCPU x 4 GiB, 4s per study)
    const seconds = inferences_month * input.images_study * 4;
    const containerApps =
      seconds * 2 * pricing.container_apps.vcpu_second +
      seconds * 4 * pricing.container_apps.memory_gib_second +
      (inferences_month / 1_000_000) * pricing.container_apps.request_per_million;

    const support = pricing.support_plan_month[input.support_plan] || 0;
    const defender = input.defender ? 8 * pricing.defender_for_cloud_per_resource_month : 0;

    const lines = [
      ['Compute (AML endpoint)', compute],
      ['Storage (Hot/Cool/Archive + transactions)', storage],
      ['Networking (private endpoints + egress + ExpressRoute)', net],
      ['Azure Container Registry', acr],
      ['Key Vault operations', kv],
      ['Application Insights ingestion', appInsights],
      ['Log Analytics ingestion', logAnalytics],
      ['Azure AI Search', aiSearch],
      ['Azure Cosmos DB', cosmos],
      ['Azure Health Data Services (DICOM)', hds],
      ['Service Bus (sessions queue, Standard)', sb],
      ['Event Grid (DICOM events)', eg],
      ['Container Apps (DICOM worker)', containerApps],
      ['Support plan', support],
      ['Microsoft Defender for Cloud', defender]
    ];
    const subtotal = lines.reduce((acc, [, v]) => acc + v, 0);
    const total = subtotal * regionMod;
    return { lines, total, inferences_month, new_gb_month };
  }

  function compute() {
    if (!pricing) return;
    const input = readInputs();
    const regionMod = (pricing.regions[input.region]?.modifier) || 1;

    // Throughput-driven instance count recommendation
    const reqPerInst = pricing.requests_per_instance_per_minute;
    const recommended = Math.max(1, Math.ceil(input.target_throughput / reqPerInst));

    const main = calc(input, input.sku, input.instance_count, input.hours_month, regionMod);

    document.getElementById('total-monthly').textContent = fmt(main.total);
    document.getElementById('total-annual').textContent  = fmt(main.total * 12);
    document.getElementById('cost-per-study').textContent =
      main.inferences_month ? fmt(main.total / main.inferences_month) : '$0.00';
    document.getElementById('cost-per-1k').textContent =
      main.inferences_month ? fmt(main.total / main.inferences_month * 1000) : '$0.00';
    document.getElementById('recommended-instances').textContent = recommended;
    document.getElementById('new-data-gb').textContent = main.new_gb_month.toFixed(1);

    const tbody = document.getElementById('lines-body');
    tbody.innerHTML = '';
    main.lines.forEach(([label, amt]) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${label}</td><td class="num">${fmt(amt * regionMod)}</td>`;
      tbody.appendChild(tr);
    });

    // Sensitivity
    const sens = document.getElementById('sensitivity');
    const doubleVol = calc({...input, studies_day: input.studies_day * 2}, input.sku, input.instance_count, input.hours_month, regionMod);
    const reservedInput = {...input, reservation: input.reservation === 'none' ? '1yr' : input.reservation};
    const reserved = calc(reservedInput, input.sku, input.instance_count, input.hours_month, regionMod);
    const biggerSku = Object.keys(pricing.compute_hourly)[Math.min(2, Object.keys(pricing.compute_hourly).indexOf(input.sku) + 1)];
    const bigger = calc(input, biggerSku, input.instance_count, input.hours_month, regionMod);

    sens.innerHTML = `
      <div class="sensitivity-card"><div class="label">2× studies/day</div><div class="value">${fmt(doubleVol.total)}</div></div>
      <div class="sensitivity-card"><div class="label">With 1-yr reserved</div><div class="value">${fmt(reserved.total)}</div></div>
      <div class="sensitivity-card"><div class="label">Upgrade to ${biggerSku}</div><div class="value">${fmt(bigger.total)}</div></div>
      <div class="sensitivity-card"><div class="label">Recommended instances (throughput)</div><div class="value">${recommended}</div></div>
    `;
  }

  function fmt(n) {
    return n.toLocaleString('en-US', { style: 'currency', currency: pricing?._meta.currency || 'USD', maximumFractionDigits: 2 });
  }

  // Export CSV / Markdown
  function buildExport(format) {
    const input = readInputs();
    const regionMod = (pricing.regions[input.region]?.modifier) || 1;
    const r = calc(input, input.sku, input.instance_count, input.hours_month, regionMod);
    const headers = ['Line item', 'Monthly USD'];
    const rows = r.lines.map(([l, v]) => [l, (v * regionMod).toFixed(2)]);
    rows.push(['TOTAL', r.total.toFixed(2)]);
    if (format === 'csv') {
      return [headers, ...rows].map(row => row.map(c => `"${c}"`).join(',')).join('\n');
    }
    let md = `# CXRReportGen monthly cost estimate\n\nRegion: **${pricing.regions[input.region].label}**  ·  SKU: **${input.sku}**  ·  Instances: **${input.instance_count}**\n\n`;
    md += `| Line item | Monthly USD |\n|---|---:|\n`;
    rows.forEach(([l, v]) => { md += `| ${l} | $${v} |\n`; });
    md += `\n_Estimates only. Validate at https://azure.microsoft.com/pricing/calculator/_\n`;
    return md;
  }

  function download(name, mime, body) {
    const blob = new Blob([body], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name; document.body.appendChild(a); a.click();
    a.remove(); URL.revokeObjectURL(url);
  }

  document.addEventListener('click', e => {
    if (e.target.matches('#btn-csv')) download('cxr-cost.csv', 'text/csv', buildExport('csv'));
    if (e.target.matches('#btn-md')) {
      const md = buildExport('md');
      navigator.clipboard.writeText(md).then(() => { e.target.textContent = 'Copied'; setTimeout(() => e.target.textContent = 'Copy as Markdown', 1500); });
    }
    if (e.target.matches('#btn-reset')) {
      localStorage.removeItem('cxr_calc');
      location.reload();
    }
    if (e.target.matches('#btn-reload-pricing')) loadPricing();
  });

  loadPricing();
})();
