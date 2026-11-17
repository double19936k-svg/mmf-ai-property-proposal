(() => {
  const root = document.getElementById('cursor-settings');
  let timer;
  let requestPending = false;

  function render(row) {
    clearTimeout(timer);
    if (!row || (!row.models.length && !root.firstElementChild)) return;
    if (!root.firstElementChild) {
      root.innerHTML = `<section class="panel">
        <div class="provider-head"><h2>Cursor 账户</h2><span class="provider-state"></span></div>
        <div class="field"><label for="cursor-model">本产品使用的模型</label><select id="cursor-model"></select></div>
        <div class="provider-actions">
          <button class="primary" type="button" data-action="connect">连接引擎</button>
          <button class="secondary" type="button" data-action="check">测试连接</button>
          <button class="secondary" type="button" data-action="disconnect">断开连接</button>
        </div>
        <p class="provider-note" role="status"></p>
      </section>`;
      root.querySelectorAll('[data-action]').forEach(button => {
        button.onclick = () => act(button.dataset.action);
      });
      root.querySelector('select').onchange = event => {
        if (event.target.value) act('model', {provider_name:event.target.value});
      };
    }
    const select = root.querySelector('select');
    const options = row.models.map(model => {
      const option = new Option(model.label, model.provider_name);
      option.disabled = row.connected && !model.available;
      return option;
    });
    if (!row.models.some(model => model.provider_name === row.provider_name)) {
      options.unshift(new Option('请选择可用模型', ''));
    }
    select.replaceChildren(...options);
    select.value = row.models.some(model => model.provider_name === row.provider_name) ? row.provider_name : '';
    select.disabled = requestPending || !!row.busy || !row.models.length;
    root.querySelectorAll('button').forEach(button => {
      button.disabled = requestPending || !!row.busy || (button.dataset.action === 'disconnect' && row.disconnected);
    });
    root.querySelector('.provider-state').textContent = row.busy ? '正在处理' :
      row.connected ? '已连接' : row.disconnected ? '已断开' : '未连接';
    root.querySelector('[role="status"]').textContent = row.message || (
      row.connected ? '新提交的任务将使用此处保存的模型。' : '请连接 Cursor 账户并选择模型。');
    if (row.busy) timer = setTimeout(poll, 1200);
  }

  async function poll() {
    try {
      const row = await api('/api/cursor-account/status');
      if (!row.busy) await refreshProviderHealth();
      render(row);
    } catch (error) {
      root.querySelector('[role="status"]').textContent = error.message;
      timer = setTimeout(poll, 3000);
    }
  }

  async function act(action, data={}) {
    if (requestPending) return;
    requestPending = true;
    clearTimeout(timer);
    root.querySelectorAll('button,select').forEach(control => { control.disabled = true; });
    let row;
    try {
      row = await api('/api/cursor-account/' + action, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data),
      });
      if (!row.busy) await refreshProviderHealth();
    } catch (error) {
      row = {...state.cursorAccount, message:error.message};
    } finally {
      requestPending = false;
    }
    render(row);
  }

  window.renderCursorSettings = render;
})();
