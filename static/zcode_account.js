/* Optional domestic ZCode card, independent of the existing Qoder connection. */
(() => {
  let busy=false,lastReady=false;
  const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={not_connected:'尚未连接',checking:'正在检查',authorizing:'等待官方授权',ready:'国内账户已连接',error:'连接需处理'};
  const phases={queued:'正在排队，尚未发送',dispatch_ready:'排队结束，准备调用',submitted:'已提交当前内容',working:'ZCode正在处理',returned:'ZCode已回传，等待检查',validated_response:'回复已接收，审核与装配继续',response_format_failed:'回复已收到，但格式检查未完成；原答和已完成内容保留',failed_or_unknown:'本次请求未完成，已有内容保留',not_dispatched:'本次未发送模型请求，可重新检查后重试'};
  async function request(action,data={}){
    const response=await fetch('/api/zcode-account/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const row=await response.json();if(!response.ok)throw new Error(row.error||'连接未完成');return row;
  }
  function card(){
    let el=document.getElementById('zcode-account-card');if(el)return el;
    el=document.createElement('section');el.id='zcode-account-card';el.className='panel';
    el.innerHTML=`<div class="provider-head"><h2>ZCode 国内账户连接 <small style="font-size:13px;font-weight:400">实验</small></h2><span id="za-badge" class="provider-state"></span></div>
      <p class="hint">通过智谱 BigModel 国内账户官方登录，无需填写 API Key。连接检查不试写；实际额度和适用范围以官方套餐为准。</p>
      <p id="za-message" role="status" aria-live="polite"></p>
      <ol id="za-login-steps" class="provider-note" style="display:flex;gap:28px;flex-wrap:wrap;padding-left:22px"><li>测试连接</li><li>打开官方登录页</li><li>完成网页授权</li><li>返回本页自动连接</li></ol>
      <p id="za-login-help" class="notice" aria-live="polite"></p>
      <label class="provider-note proposal-review"><input type="checkbox" id="za-enable-full-model" style="width:auto"> 启用GLM-5.3（额度恢复后再开启；不会自动切换当前模型）</label>
      <div class="grid"><div class="field"><label for="za-model">本产品使用的模型</label><select id="za-model"><option value="">连接后请选择模型</option></select><p class="provider-note">这里使用独立配置，不会自动跟随ZCode桌面端切换。选择后会保存到本产品实际使用的配置。</p></div><div class="field"><label>连接验证范围</label><div class="provider-note">官方登录及内置国内模型配置；模型实际权限、额度和真实生成以官方返回为准。</div></div></div>
      <div class="provider-actions"><button id="za-connect" class="primary" type="button">连接引擎</button><a id="za-login" class="secondary hidden" target="_blank" rel="noopener noreferrer">打开智谱官方登录页</a><button id="za-check" class="secondary" type="button">测试连接</button><button id="za-cancel" class="secondary hidden" type="button">取消本次登录</button><button id="za-disconnect" class="secondary" type="button">断开连接</button></div>
      <p class="provider-note">登录问题可直接在本页处理：点击「连接引擎」，打开官方页面并完成授权，再返回本页查看结果。无需查看后台或输入命令；测试连接不会生成内容。</p>
      <p class="provider-note">连接并选定模型后，可在原“AI引擎”中选择 ZCode 国内账户。新PPT任务按正文页数预留请求：常规15～20页、详细30～40页、大型50～70页，另加最多6次局部修复，合计不超过96次，单路执行；旧任务保留原预算。新Word按规模最多32/64/128次（包含修复）；旧任务保留原预算，文件识别最多48次。连接成功不代表整份方案通过验收。</p><div id="za-progress" class="notice hidden" aria-live="polite"></div>`;
    const settings=document.getElementById('view-settings'),qoder=document.getElementById('qoder-account-card');
    if(qoder?.parentElement===settings)qoder.after(el);else settings.prepend(el);
    for(const action of ['connect','check','disconnect'])el.querySelector('#za-'+action).onclick=async()=>{
      if(busy)return;busy=true;try{paint(await request(action));}catch(e){el.querySelector('#za-message').textContent=e.message;}finally{busy=false;}
    };
    el.querySelector('#za-cancel').onclick=async()=>{
      if(busy)return;busy=true;try{paint(await request('disconnect'));}catch(e){el.querySelector('#za-message').textContent=e.message;}finally{busy=false;}
    };
    el.querySelector('#za-model').onchange=async e=>{
      if(!e.target.value)return;try{paint(await request('model',{model:e.target.value}));if(typeof loadConfig==='function')await loadConfig(false);}catch(e){el.querySelector('#za-message').textContent=e.message;}
    };return el;
  }
  function paint(row){
    const el=card(),ready=row.status==='ready';el.querySelector('#za-badge').textContent=labels[row.status]||row.status;
    el.querySelector('#za-message').textContent=row.message||'';
    const fullModel=el.querySelector('#za-enable-full-model');fullModel.checked=row.glm53_enabled!==false;
    fullModel.disabled=!!row.connecting||!!row.executing;
    fullModel.onchange=async e=>{if(busy)return;busy=true;try{paint(await request('preferences',{glm53_enabled:e.target.checked}));}catch(err){el.querySelector('#za-message').textContent=err.message;}finally{busy=false;}};
    for(const action of ['connect','check'])el.querySelector('#za-'+action).disabled=!!row.connecting||!!row.executing;
    el.querySelector('#za-connect').disabled ||= row.next_action==='update';
    el.querySelector('#za-disconnect').disabled=!!row.executing;
    el.querySelector('#za-cancel').classList.toggle('hidden',!row.connecting||row.status!=='authorizing');
    const step=ready?3:row.login_url?2:row.status==='authorizing'?1:0;
    el.querySelectorAll('#za-login-steps li').forEach((item,i)=>{item.style.fontWeight=i===step?'700':'400';item.setAttribute('aria-current',i===step?'step':'false');});
    let help=ready?'国内账户已连接。需要更新登录或切换账户时，可点击“连接引擎”。':
      row.status==='authorizing'?(row.login_url?'打开下方官方登录页完成授权，本页会自动接收结果。':'正在准备官方登录链接…'):
      row.next_action==='update'?'当前问题来自软件版本兼容性，重复登录无效。请更新本产品后点击“测试连接”。':
      row.reason_code==='ZCODE_LOGIN_REQUIRED'?'已保存的登录不被当前 ZCode 接受。请点击“连接引擎”，完成后再测试连接。':
      row.reason_code==='ZCODE_LOGIN_TIMEOUT'?'本次授权等待已结束。点击“连接引擎”即可重新开始。':
      '先点击“测试连接”复用已有登录；需要登录或授权失效时，点击“连接引擎”。';
    if(row.status==='authorizing'&&row.login_expires_at)help+=` 剩余等待约 ${Math.max(0,Math.ceil(row.login_expires_at-Date.now()/1000))} 秒，可随时取消。`;
    el.querySelector('#za-login-help').textContent=help;
    const select=el.querySelector('#za-model');
    const options='<option value="">请选择本次使用的模型</option>'+(row.models||[]).map(m=>`<option value="${escape(m.id)}">${escape(m.label||m.ref?.modelId||m.id)}</option>`).join('');
    if(select.innerHTML!==options)select.innerHTML=options;select.value=row.selected_model||'';select.disabled=!ready||row.executing;
    const link=el.querySelector('#za-login');link.classList.toggle('hidden',!row.login_url);
    if(row.login_url)link.href=row.login_url;else link.removeAttribute('href');
    const progress=el.querySelector('#za-progress');progress.classList.add('hidden');progress.textContent='';
    if(ready!==lastReady){lastReady=ready;if(typeof loadConfig==='function')loadConfig(false).catch(()=>{});}
  }
  async function poll(){try{const response=await fetch('/api/zcode-account/status');if(!response.ok)throw new Error();const row=await response.json();if(!row.enabled)return;paint(row);setTimeout(poll,2500);}catch(_){const el=card();el.querySelector('#za-message').textContent='暂时无法连接本机服务，正在重试。若持续失败，请重新打开本程序。';el.querySelector('#za-badge').textContent='等待本机服务';setTimeout(poll,5000);}}
  poll();
})();
