/* Optional connection card. Existing forms, routes and default selection remain owned by index.html. */
(() => {
  let timer, busy = false, lastReady = false;
  const labels = {not_connected:'尚未连接',connecting:'正在连接',authentication_required:'需要登录',ready:'账户已连接',unavailable:'暂不可用'};
  const phases = {login_required:'等待官方登录',connected:'账户检查完成',connection_unavailable:'连接需处理',disconnected:'已断开本产品连接',admitted:'已接收当前任务',generating:'Qoder正在处理',returned:'Qoder已回传，等待检查',validated_response:'回复已接收，后续审核与装配继续'};
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  async function request(action, data={}) {
    const response = await fetch('/api/qoder-account/'+action, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const row = await response.json();
    if(!response.ok) throw new Error(row.error || '连接操作未完成');
    return row;
  }
  function card() {
    let el = document.getElementById('qoder-account-card');
    if(el) return el;
    el = document.createElement('section'); el.id='qoder-account-card'; el.className='panel';
    el.innerHTML=`<div class="provider-head"><h2><span id="qa-product">Qoder</span>账户连接 <small style="font-size:13px;font-weight:400">实验</small></h2><span id="qa-badge" class="provider-state"></span></div>
      <p class="hint" id="qa-account-hint">使用官方账户登录，无需填写模型API Key。连接检查不发起写稿，生成时可能消耗账户额度。</p>
      <p id="qa-message" role="status" aria-live="polite"></p>
      <div class="grid"><div class="field"><label for="qa-model">账户可用模型</label><select id="qa-model"><option value="">连接后请选择模型</option></select><div id="qa-catalog-note" class="provider-note"></div></div><div class="field"><label>账户用量</label><div id="qa-usage" class="provider-note">暂不可读取</div></div></div>
      <div class="provider-actions"><button id="qa-connect" class="primary" type="button">连接引擎</button><a id="qa-login-link" class="secondary hidden" target="_blank" rel="noopener noreferrer">打开官方登录页</a><button id="qa-check" class="secondary" type="button">测试连接</button><button id="qa-disconnect" class="secondary" type="button">断开连接</button></div>
      <p id="qa-limit" class="provider-note"></p><div id="qa-progress" class="notice hidden" aria-live="polite"></div>`;
    document.getElementById('view-settings').prepend(el);
    for(const action of ['connect','check','disconnect']) {
      el.querySelector('#qa-'+action).onclick=async()=>{
        if(busy) return; busy=true;
        try { paint(await request(action)); } catch(err) { el.querySelector('#qa-message').textContent=err.message; }
        finally { busy=false; }
      };
    }
    el.querySelector('#qa-model').onchange=async e=>{
      if(!e.target.value) return;
      try { paint(await request('model',{model:e.target.value})); if(typeof loadConfig==='function') await loadConfig(false); }
      catch(err) { el.querySelector('#qa-message').textContent=err.message; }
    };
    return el;
  }
  function paint(row) {
    const el=card(), ready=row.status==='ready';
    const product=row.product_label||'Qoder';
    el.querySelector('#qa-product').textContent=product;
    el.querySelector('#qa-account-hint').textContent=`使用${product}官方账户登录，无需填写模型API Key。连接检查不发起写稿，生成时可能消耗账户额度。`;
    el.querySelector('#qa-badge').textContent=labels[row.status] || row.status;
    el.querySelector('#qa-message').textContent=row.message || '';
    el.querySelector('#qa-connect').disabled=!!row.connecting;
    el.querySelector('#qa-check').disabled=!!row.connecting;
    const options=(row.models||[]).filter(m=>m.isEnabled===true);
    const select=el.querySelector('#qa-model');
    const html='<option value="">请选择本次使用的模型</option>'+options.map(m=>`<option value="${escape(m.value)}">${escape(m.displayName||m.value)}${m.isFree===true&&m.priceFactor===0?'（目录标注免费）':''}</option>`).join('');
    if(select.innerHTML!==html) select.innerHTML=html;
    select.value=row.selected_model||''; select.disabled=!ready||row.executing;
    el.querySelector('#qa-catalog-note').textContent=ready&&row.edition==='cn'&&!options.some(m=>String(m.displayName||m.value).toLowerCase()==='qoduck')
      ?'当前连接的官方模型目录未提供 Qoduck。模型费用与每日优惠以 Qoder 账户为准。':'';
    const loginLink=el.querySelector('#qa-login-link');
    loginLink.classList.toggle('hidden',!row.official_login_url);
    if(row.official_login_url)loginLink.href=row.official_login_url;else loginLink.removeAttribute('href');
    const usage=row.usage;
    el.querySelector('#qa-usage').textContent=typeof usage?.totalUsagePercentage==='number'?`账户额度已使用 ${usage.totalUsagePercentage}%${usage.isQuotaExceeded?'；额度不足':''}`:'暂不可读取，以Qoder账户记录为准';
    const caps=row.word_call_caps;
    const budget=caps?`新建 Word 方案按规模最多 ${caps.regular_solution} / ${caps.detailed_technical_solution} / ${caps.large_technical_tender} 次写稿请求（含修复）`:`方案生成每轮最多 ${row.max_jobs_per_run} 次请求`;
    el.querySelector('#qa-limit').textContent=`${budget}。每次文件识别最多 ${row.recognition_max_jobs_per_run||24} 次请求。`;
    const progress=el.querySelector('#qa-progress');progress.classList.add('hidden');progress.textContent='';
    if(ready!==lastReady){lastReady=ready;if(typeof loadConfig==='function')loadConfig(false).catch(()=>{});}
  }
  async function poll() {
    try {
      const response=await fetch('/api/qoder-account/status');
      if(!response.ok) return;
      const row=await response.json();
      if(!row.enabled) return;
      paint(row); timer=setTimeout(poll,2500);
    } catch(_) {timer=setTimeout(poll,5000);}
  }
  poll();
})();
