/* One account workflow progress surface. API providers keep their existing UI. */
(()=>{
  const retiredRuns=new Set();let last='',sending=false;
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function hide(){
    document.getElementById('account-agent-tasks')?.classList.add('hidden');last='';
    document.querySelectorAll('[data-account-superseded]').forEach(e=>{e.classList.remove('hidden');e.removeAttribute('data-account-superseded');});
    const start=document.querySelector('#tender-understand[data-account-busy]');
    if(start){start.disabled=false;start.removeAttribute('data-account-busy');}
    if(typeof state!=='undefined')state.accountWaitingMessage='';
  }
  function context(){
    if(typeof state==='undefined')return null;
    const view=document.querySelector('.tab.active')?.dataset.view;
    if(!['tender','new'].includes(view))return null;
    if(view==='new'&&document.getElementById('result-step')?.classList.contains('hidden'))return null;
    const provider=view==='tender'?state.tenderProvider:state.genProvider||state.result?.provider_name||state.recommendation?.provider_name||document.getElementById('provider')?.value;
    const runId=(view==='tender'?state.tenderRunId:state.runId)||'';
    return isAccountProgressEngine(provider)&&runId&&!retiredRuns.has(runId)?{view,provider,runId}:null;
  }
  document.addEventListener('mmf:view-change',hide);
  document.addEventListener('mmf:history-removed',e=>{
    const runId=e.detail?.runId;if(!runId)return;retiredRuns.add(runId);hide();
    if(typeof state==='undefined')return;
    if(state.tenderRunId===runId&&isAccountProgressEngine(state.tenderProvider)){
      clearTimeout(state.tenderPoll);state.tenderRunId=null;state.tenderPack=null;state.tenderRecognitionStatus=null;
      try{sessionStorage.removeItem('mmf_tender')}catch(_){}
      const pack=document.getElementById('tender-pack');if(pack)pack.innerHTML='';
      if(typeof setTenderStage==='function')setTenderStage(1);
    }
    const provider=state.genProvider||state.result?.provider_name||state.recommendation?.provider_name||document.getElementById('provider')?.value;
    if(state.runId===runId&&isAccountProgressEngine(provider)){
      if(typeof stopGenerationWatch==='function')stopGenerationWatch();state.runId=null;state.result=null;state.recommendation=null;
      try{sessionStorage.removeItem('mmf_gen')}catch(_){}
      if(typeof setStep==='function')setStep(1);
    }
  });
  function panel(c){
    let p=document.getElementById('account-agent-tasks');
    if(!p){
      const style=document.createElement('style');style.textContent='.account-progress{margin-top:20px;border:1px solid #c5d6e8;border-radius:12px;padding:20px;background:#f5f9ff}.account-progress .progress-heading{display:flex;justify-content:space-between;gap:16px;align-items:center}.account-progress progress{width:100%;height:16px;accent-color:#2f78b7;margin:12px 0}.account-progress .progress-detail{padding-top:12px;border-top:1px solid #dae3ee}.account-progress .progress-records{max-height:230px;overflow:auto;font-size:14px}.account-progress .progress-records p{margin:8px 0;overflow-wrap:anywhere}.account-progress textarea{display:block;width:100%;box-sizing:border-box;margin:12px 0}';document.head.append(style);
      p=document.createElement('section');p.id='account-agent-tasks';p.className='account-progress hidden';
      p.innerHTML='<h3>账号任务进度与处理</h3><div id="account-agent-workflow" role="status" aria-live="polite"></div><div id="account-agent-list"></div><p id="account-agent-error" role="alert"></p>';
    }
    document.getElementById(c.view==='tender'?'account-agent-tender-slot':'account-agent-generation-slot')?.append(p);
    return p;
  }
  async function action(name,body){
    if(sending)return;sending=true;
    document.querySelectorAll('#account-agent-tasks button').forEach(b=>b.disabled=true);
    try{
      const r=await fetch('/api/account-agents/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const v=await r.json();if(!r.ok)throw Error(v.error||'操作未完成');last='';
    }catch(e){document.getElementById('account-agent-error').textContent=e.message;}
    finally{sending=false;document.querySelectorAll('#account-agent-tasks button').forEach(b=>b.disabled=false);}
  }
  function render(data){
    const c=context(),g=data.progress;if(!c||!g||g.run_id!==c.runId){hide();return;}
    const tenderRecognition=c.view==='tender'&&g.stage==='需求识别';
    if(tenderRecognition&&g.state==='idle'){hide();return;}
    if(tenderRecognition){
      const start=document.getElementById('tender-understand');
      if(start){
        const busy=g.state==='running'||g.state==='waiting_for_user';
        start.disabled=busy;
        start.toggleAttribute('data-account-busy',busy);
      }
    }
    if(c.view==='tender'&&g.stage==='需求识别'&&g.state==='completed'&&g.total>0&&g.done===g.total){
      void syncCompletedTenderRecognition(c.runId);
    }
    const p=panel(c);p.classList.remove('hidden');
    const old=c.view==='tender'?document.getElementById('tender-process-status'):document.getElementById('gen-progress-text')?.closest('.notice');
    if(old){old.classList.add('hidden');old.setAttribute('data-account-superseded','1');}
    const restoring=c.view==='tender'?document.getElementById('tender-extraction-result'):null;
    if(restoring?.textContent.trim()==='正在恢复任务状态'){restoring.classList.add('hidden');restoring.setAttribute('data-account-superseded','1');}
    if(typeof state!=='undefined')state.accountWaitingMessage='';
    document.getElementById('account-agent-error').textContent='';
    const signature=JSON.stringify(g);if(signature===last)return;
    const previous=p.querySelector('.agent-answer'),draft=previous?.value,previousNonce=previous?.dataset.nonce;
    const opened=!!p.querySelector('details')?.open;last=signature;
    const label={running:'运行中',waiting_for_user:'等待确认',completed:'已完成',needs_attention:'待处理'}[g.state]||'正在同步';
    const engine=isCursorEngine(c.provider)?(state.providers?.find(row=>row.provider_name===c.provider)?.display_name?.replace('（试用）','')||'Cursor'):c.provider==='zcode_cn'?'ZCode':'Qoder CN';
    const work=tenderRecognition?g.recognition_work:null;
    const total=work?.total||g.total, done=work?.done??g.done;
    const percent=total?'（'+Math.min(100,Math.floor(done*100/total))+'%）':'';
    const workLabel=work?.basis==='plan'?'识别单元':'当前分块子块';
    const amount=work?'已校验 '+done+'/'+total+' 个'+workLabel+percent+' · 已完成 '+g.done+'/'+g.total+' 个文件分块':
      g.total?(g.indeterminate?'已完成 ':'')+g.done+'/'+g.total+g.unit+(g.indeterminate?'':percent):
      g.state==='completed'?'本阶段已完成':g.state==='running'?'正在准备执行计划':'进度已保留，详见原因';
    document.getElementById('account-agent-workflow').innerHTML='<div class="progress-heading"><strong>'+esc(engine)+' · '+esc(g.stage)+'</strong><span>'+esc(label)+'</span></div><progress aria-label="'+esc((work?workLabel:g.stage)+'进度')+'" '+(g.indeterminate&&!work?'':'max="'+(Number(total)||1)+'" value="'+Math.min(done,total)+'"')+'></progress><div>'+esc(amount)+'</div><p>'+esc(g.message)+'</p>';
    const t=g.task,u=g.usage||{},controls=tenderRecognition?(g.recognition_controls||{}):{};
    const hasReceipt=Number(u.returned_requests||0)>0;
    const cost=typeof u.cost==='number'
      ?'USD '+u.cost.toLocaleString(undefined,{maximumFractionDigits:8})+(u.cost_complete?'':'（部分回执）')
      :hasReceipt?'回执未提供金额':'尚无最终回执，费用待核实';
    const credits=typeof u.total_credits==='number'
      ?' · '+u.total_credits.toLocaleString(undefined,{maximumFractionDigits:6})+' Qoder 积分'+(u.credits_complete?'':'（部分回执）'):'';
    const quota=typeof u.account_usage_percent==='number'
      ?'<p class="hint">Qoder 账户最近一次检查：额度已使用 '+esc(u.account_usage_percent.toLocaleString())+'%'+(u.account_quota_exceeded?'，额度已超限':'')+'。这是账户总体用量。</p>':'';
    const tokens=u.total_tokens==null?'未提供 token 用量':Number(u.total_tokens).toLocaleString()+' tokens'+(u.usage_complete?'':'（部分回执有用量）');
    const question=t?.question&&t.nonce?'<div class="progress-detail"><p><b>'+esc(t.question.question)+'</b></p>'+(g.pending_count>1?'<p class="hint">另有'+(g.pending_count-1)+'项待确认，按顺序处理。</p>':'')+'<div>'+(t.question.choices||[]).map(o=>'<button type="button" class="secondary agent-choice" data-value="'+esc(o.label)+'">'+esc(o.label)+'</button>').join(' ')+'</div><textarea class="compact agent-answer" data-nonce="'+esc(t.nonce)+'" aria-label="回复Agent" maxlength="3000" placeholder="选择选项或填写决定"></textarea><button type="button" class="primary agent-submit">回复并继续原会话</button> <button type="button" class="secondary agent-cancel">停止当前内容任务</button></div>':(t?.retry_available||t?.state==='content_ready')?'<button type="button" class="secondary agent-continue">'+(t.retry_available?'接续未发送的步骤':'接续原任务')+'</button>':'';
    const next=controls.split||controls.retry||controls.recheck||controls.retry_unsent?'<button type="button" class="primary agent-retry-recognition">'+(controls.split?'拆小后接续识别':controls.retry_unsent?'接续未发送的识别':controls.recheck?'核对原回复并接续识别':'重新识别')+'</button>':'';
    const local=controls.accept_local?'<button type="button" class="secondary agent-accept-local">继续使用本地解析</button>':'';
    const choice=controls.choice;
    const choose=choice?'<button type="button" class="primary agent-recognition-choice">'+(choice.action==='repair'?'整理原回复并接续':'重试当前子块并接续')+'</button>':'';
    const hint=choice?'<p class="hint" role="alert">'+(choice.action==='repair'?'当前子块可在本机整理。接续后续子块时会调用 Cursor。':'当前子块会重新调用 '+(c.provider==='zcode_cn'?'ZCode':'Cursor')+' 一次，校验通过后继续识别。')+' 已校验的子块会直接复用。</p>':'';
    const recognitionActions=next||local||choose?hint+'<div class="actions">'+choose+next+local+'</div>':'';
    document.getElementById('account-agent-list').innerHTML=question+recognitionActions+'<details '+(opened?'open':'')+'><summary>执行记录与用量</summary><p>官方回执：'+Number(u.returned_requests||0)+'次回复（识别'+Number(u.recognition_requests||0)+'次，知识推荐'+Number(u.recommendation_requests||0)+'次，写稿'+Number(u.generation_requests||0)+'次）；'+esc(tokens)+'。回执费用：'+esc(cost+credits)+'。</p>'+quota+'<p class="hint">账号内容回复经过原流程检查、装配后才形成完整文件。'+(isCursorEngine(c.provider)?'用量来自本任务的 Cursor SDK 回执。':c.provider==='zcode_cn'?'ZCode 桌面用量图不作为本任务用量依据。':'Qoder 单次用量取自已保存的官方回执。')+'</p><div class="progress-records">'+(g.records||[]).map(r=>'<p>'+esc(r.task_id)+' · '+esc(r.label)+(r.error_code?' · '+esc(r.error_code):'')+'</p>').join('')+'</div>'+(g.record_count>10?'<p class="hint">仅展示最近10条，后台保留全部'+g.record_count+'条记录。</p>':'')+'</details>';
    const answer=p.querySelector('.agent-answer');if(answer&&previousNonce===t?.nonce)answer.value=draft||'';
    p.querySelectorAll('.agent-choice').forEach(b=>b.onclick=()=>{answer.value=b.dataset.value;});
    p.querySelector('.agent-submit')?.addEventListener('click',()=>action('answer',{key:t.key,nonce:t.nonce,answer:answer.value}));
    p.querySelector('.agent-cancel')?.addEventListener('click',()=>action('cancel',{key:t.key}));
    p.querySelector('.agent-continue')?.addEventListener('click',()=>action('continue',{key:t.key}));
    p.querySelector('.agent-retry-recognition')?.addEventListener('click',e=>{
      if(context()?.runId!==c.runId)return;
      e.currentTarget.disabled=true;
      startTenderRecognition();
    });
    p.querySelector('.agent-recognition-choice')?.addEventListener('click',e=>{
      if(context()?.runId!==c.runId)return;
      e.currentTarget.disabled=true;
      startTenderRecognition(choice);
    });
    p.querySelector('.agent-accept-local')?.addEventListener('click',()=>{
      if(context()?.runId===c.runId)acceptLocalTenderFallback();
    });
    const needsChoice=!!(t?.question&&t.nonce)||!!(controls.split||controls.retry||controls.recheck||controls.retry_unsent||controls.accept_local||controls.choice)||g.state==='waiting_for_user';
    if(needsChoice && window.mmfAlertManualStep){
      const promptText=t?.question?.question||g.message||'页面上有一项需要你选择，选完后生成才会继续。';
      window.mmfAlertManualStep('需要你确认后才能继续', promptText);
    }
  }
  async function poll(){
    try{
      const c=context();if(!c){hide();return;}
      const r=await fetch('/api/account-agents?run_id='+encodeURIComponent(c.runId));if(!r.ok)throw Error();const data=await r.json();
      if(JSON.stringify(context())!==JSON.stringify(c)){hide();return;}render(data);
    }catch(_){
      const p=document.getElementById('account-agent-error');if(p&&context())p.textContent='暂时无法同步进度，正在自动重连。';
    }finally{setTimeout(poll,2000);}
  }
  document.addEventListener('mmf:account-handling-focus',()=>{last='';document.getElementById('account-agent-tasks')?.scrollIntoView({behavior:'smooth',block:'center'});});
  poll();
})();
