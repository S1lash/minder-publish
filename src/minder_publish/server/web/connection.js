/* Provider-specific setup. Credentials are entered only in their private settings. */
const mounts = new WeakMap();
const providers = [
  {id:'codex',name:'Codex',kind:'local'},
  {id:'claude-code',name:'Claude Code',kind:'local'},
  {id:'chatgpt',name:'ChatGPT',kind:'cloud'},
  {id:'claude',name:'Claude',kind:'cloud'},
  {id:'other',name:'Other',kind:'other'},
];

export async function mountConnection({root,api,t,escape,date,bytes,onOpenLimits,onCreateToken,onError,isCurrent}) {
  mounts.get(root)?.controller.abort();
  const mounted={controller:new AbortController()};mounts.set(root,mounted);
  const current=()=>mounts.get(root)===mounted&&root.isConnected&&isCurrent();
  const text=(key,values)=>escape(t(key,values));
  const safeURL=value=>{
    try {const url=new URL(value,location.href);return ['https:','http:'].includes(url.protocol)&&!url.username&&!url.password?url.href:null;}
    catch {return null;}
  };
  let distribution,connections,tokens,limits;
  let selected=providers.some(provider=>provider.id===root.dataset.publishProvider)?root.dataset.publishProvider:'codex';
  let copies={};
  let copySequence=0, copySource;
  const button=(name,key,classes='')=>`<button type="button" class="${classes}" data-connection-copy="${name}">${text(key)}</button>`;
  const code=value=>`<code class="connection-code">${escape(value)}</code>`;
  const link=(value,key)=>{
    const url=safeURL(value);return url?`<a class="connection-download" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${text(key)}</a>`:'';
  };
  const asset=(name,key)=>link(distribution.assets?.[name]?.url,key);
  const step=(title,body)=>`<li class="connection-step"><div class="connection-step-body"><h3>${text(title)}</h3>${body}</div></li>`;
  const copyStatus=()=>'<p class="connection-copy-status" role="status" hidden></p><div class="connection-manual-copy" hidden></div>';

  async function copy(value,trigger) {
    const attempt=++copySequence;
    const stillVisible=()=>current() && attempt===copySequence && trigger.isConnected;
    const status=root.querySelector('.connection-copy-status');
    const fallback=root.querySelector('.connection-manual-copy');
    status.hidden=true;fallback.hidden=true;copySource=undefined;
    trigger.disabled=true;
    try {
      await navigator.clipboard.writeText(value);
      if(!stillVisible())return;
      status.textContent=t('Copied.');status.hidden=false;
    }catch {
      if(!stillVisible())return;
      copySource=trigger;
      fallback.innerHTML=`<strong>${text('Copy manually')}</strong><p>${text('Your browser blocked clipboard access. Select and copy these instructions.')}</p><textarea readonly aria-label="${text('Instructions to copy')}"></textarea><button type="button" data-connection-close-copy>${text('Close')}</button>`;
      fallback.hidden=false;const output=fallback.querySelector('textarea');output.value=value;output.focus();output.select();
    }finally {if(current()&&trigger.isConnected)trigger.disabled=false;}
  }

  function local(provider) {
    const runtime=provider.id==='claude-code'?'claude':'codex';
    copies.cli=distribution.install_prompts?.[runtime];copies.skill=distribution.skill_prompts?.[runtime];
    const install=distribution.client?.available&&copies.cli
      ? `<p>${text('Paste one instruction into {app}. Your assistant will install the publishing tool and skill, then give you a command for your private terminal.',{app:provider.name})}</p><div class="connection-step-actions">${button('cli','Copy setup instructions','primary')}</div>`
      : `<p class="connection-notice">${text('Client download is not available. Ask the operator to provide the client release.')}</p>`;
    const advanced=`<details class="connection-release-details"><summary>${text('Manual setup and repair')}</summary><p>${text('The installer reports the full executable path. Use that path if the command is not available in your terminal.')}</p><div class="connection-step-actions">${distribution.client?.available?asset(distribution.client.filename,'Download client'):''}${copies.skill?button('skill','Copy skill instructions'):''}</div>${distribution.client?.available?`<p>${text('Client version')}: ${escape(distribution.client.version)}</p>${code('SHA-256: '+distribution.client.sha256)}`:''}<p class="connection-muted">${text('The separate skill instructions are for repairing or updating an existing installation.')}</p></details>`;
    return `<div class="connection-provider-head"><h2>${text('Use Publish from {app}',{app:provider.name})}</h2><p>${text('Your assistant can publish finished files and sites from this computer.')}</p></div><ol class="connection-setup-steps">${
      step('Install the tool and skill',install)+
      step('Connect with your token',`<p>${text('If setup reports an existing connection is working, you are done. No new token is needed.')}</p><p>${text('Create a device token, then run the connection command your assistant provided in your own terminal.')}</p><div class="connection-step-actions"><button type="button" data-connection-create-token>${text('Create token')}</button></div><p class="connection-muted">${text('Paste the token once into the hidden terminal prompt. Never paste it into an AI chat or put it in a command.')}</p><p>${text('The setup verifies your connection. Continue when it reports that the connection is working.')}</p>`)
    }</ol>${advanced}`;
  }

  function uploadLimits() {
    const size = value => Number.isFinite(value) ? escape(bytes(value)) : '—';
    const files = Number.isFinite(limits?.site_files)
      ? new Intl.NumberFormat(document.documentElement.lang).format(limits.site_files) : '—';
    const rows = [
      ['Single file', size(Math.min(limits?.file_bytes, limits?.upload_payload_bytes))],
      ['Site ZIP', size(limits?.upload_payload_bytes)],
      ['Expanded site', size(limits?.site_expanded_bytes)],
      ['Files per site', files],
    ];
    return `<span class="connection-limits" data-upload-limits><button type="button" aria-expanded="false" aria-controls="connection-upload-limits">${text('Upload limits')}</button><span class="connection-limits-panel" id="connection-upload-limits" hidden>${rows.map(([key,value])=>`<span class="connection-limits-row"><span>${text(key)}</span><strong>${value}</strong></span>`).join('')}<button type="button" data-open-upload-limits>${text('Open upload settings')}</button></span></span>`;
  }

  function bindUploadLimits() {
    const container = root.querySelector('[data-upload-limits]');
    if (!container) return;
    const trigger = container.querySelector('[aria-controls]');
    const panel = container.querySelector('.connection-limits-panel');
    let pinned = false;
    let suppressFocus = false;
    const show = open => {
      panel.hidden = !open;
      trigger.setAttribute('aria-expanded', String(open));
    };
    const close = () => { pinned = false; show(false); };
    container.addEventListener('pointerenter', event => {
      if (event.pointerType === 'mouse') show(true);
    });
    container.addEventListener('pointerleave', () => {
      if (!pinned && !container.contains(document.activeElement)) show(false);
    });
    trigger.addEventListener('focus', () => { if (!suppressFocus) show(true); });
    trigger.addEventListener('click', () => {
      pinned = !pinned;
      show(pinned);
    });
    container.addEventListener('focusout', event => {
      const next = event.relatedTarget;
      if (next && !next.contains(container) && !container.contains(next)) close();
    });
    container.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || panel.hidden) return;
      event.preventDefault();
      suppressFocus = true;
      close();
      trigger.focus();
      suppressFocus = false;
    });
    // The delegated handler checks the current disclosure after each provider render.
    mounted.dismissLimits = event => {
      if (!container.contains(event.target)) close();
    };
  }

  function cloud(provider) {
    copies.mcp=distribution.mcp_url;
    copies['cloud-check']=t('Use my connected Minder Publish to list at most three materials. Show their titles and stable links. Do not publish, edit, delete or upload anything. If authorization fails, explain the error without requesting credentials in chat.');
    copies['file-request']=t('Use Minder Publish to create an upload link for my file. I will choose the file and confirm publication in the browser.');
    const chatgpt=provider.id==='chatgpt';
    const setup=chatgpt
      ? `<p>${text('Open ChatGPT Plugins, choose +, then Add custom MCP server.')}</p><p>${text('Choose OAuth with automatic client registration (DCR). Leave static client ID and client secret empty. Then choose Create as a plugin and install the resulting plugin.')}</p>`
      : `<p>${text('Open Claude Plugins (or Customize in the older layout), then add a custom web connector.')}</p><p>${text('Choose Sign in now. Under OAuth client, choose Register automatically, then sign in to Minder Publish.')}</p>`;
    const skill=chatgpt
      ? `<p>${text('In ChatGPT, go to Plugins → Skills → Create → Upload from your computer and select the ZIP.')}</p>`
      : `<p>${text('In Claude, go to Customize (or Plugins) → Skills → + → Create skill → Upload a skill. Select the ZIP and turn on the skill.')}</p><p class="connection-muted">${text('Enable code execution and file creation if the Skills section is unavailable.')}</p>`;
    const bearer=chatgpt?'':`<details class="connection-bearer"><summary>${text('Advanced: use a device token in Claude')}</summary><p>${text('For a device token, choose No sign in instead of OAuth. Add the Authorization request header in Claude’s connector settings.')}</p><p>${text('Header name')}: <code>Authorization</code></p><p>${text('Header value')}: <code>Bearer YOUR_DEVICE_TOKEN</code></p><p class="connection-muted">${text('Do not paste your actual token into a prompt, skill or this page.')}</p><button type="button" data-connection-create-token>${text('Create token')}</button></details>`;
    return `<div class="connection-provider-head"><h2>${text('Use Publish from {app}',{app:provider.name})}</h2><p>${text('Connect your account on web or desktop. Availability on other devices depends on your account and app.')}</p></div>${connections.enabled?'':`<p class="connection-notice">${text('Remote connections are disabled on this instance. Ask the operator to enable them before connecting.')}</p>`}<ol class="connection-setup-steps">${
      step('Connect Publish',setup+`<div class="connection-server-row">${code(distribution.mcp_url)}${button('mcp','Copy server address')}</div><p class="connection-muted">${text('Publishing requires materials:manage, which allows reading, creating, updating and deleting materials. materials:read allows viewing only. Review the requested permissions before approving; you can revoke the connection below.')}</p>`)+
      step('Choose tool permissions',`<p>${text('Decide whether the host should ask before each tool use or allow this connection to act without repeated prompts. Automatic approval can let the assistant change or delete materials within its granted access.')}</p><p>${chatgpt?text('In ChatGPT, review the Publish app’s permission controls or an approval prompt and choose the available option that matches your preference. Options depend on the account, app and action.'):text('In Claude on web or desktop, open Customize → Connectors → Publish → Permissions and review the tool permissions. Choose the available setting for each tool that matches your preference. Always ask may not be available on every account. In Auto mode, Needs approval may run without asking; check the conversation mode too.')}</p><p class="connection-muted">${text('These host prompts are separate from the access you approved in Publish. A skill adds instructions; it does not grant access or change permissions.')}</p>`)+
      step('Install the publishing skill',`<div class="connection-step-actions">${asset('minder-publish-skill.zip','Download skill')}</div>${skill}<p class="connection-muted">${text('If your account cannot upload skills, continue with the connection alone.')}</p>`)+
      step('Check the connection',`<p>${text('Start a new chat and select the installed Publish plugin or enable its connector. Copy this request to check access without changing any materials.')}</p><div class="connection-step-actions">${button('cloud-check','Copy connection check','primary')}</div>`)+
      step('Publish a file',`<p>${text('Ask your assistant for a Publish upload link. Open it on your phone or computer, sign in, choose the file and confirm.')} ${uploadLimits()}</p><div class="connection-step-actions">${button('file-request','Copy upload request')}</div><p class="connection-muted">${text('The browser upload creates a new material. To replace an existing PDF, image or site, use its cabinet controls or the configured CLI.')}</p>`)
    }</ol><div class="connection-phone"><h3>${text('Use on your phone')}</h3><p>${text('Personal plugin availability and setup controls depend on your account and app. Complete setup on web or desktop, then check the connection and skill on your phone.')}</p></div>${bearer}`;
  }

  function other() {
    copies.mcp=distribution.mcp_url;
    return `<div class="connection-provider-head"><h2>${text('Use another assistant')}</h2><p>${text('Choose the method your assistant supports.')}</p></div><ol class="connection-setup-steps">${
      step('From this computer',`<p>${text('Use the downloadable client for an assistant that can run terminal commands. Install its skill only if that assistant supports Agent Skills.')}</p><div class="connection-step-actions">${distribution.client?.available?asset(distribution.client.filename,'Download client'):''}${asset('minder-publish-skill.zip','Download skill')}</div>`)+
      step('From a connected cloud app',`<p>${text('Add this server in the app’s remote MCP settings and authorize it with OAuth. Follow that app’s installation guide.')}</p><div class="connection-server-row">${code(distribution.mcp_url)}${button('mcp','Copy server address')}</div>`)
    }</ol>`;
  }

  function management(open) {
    const connected=Array.isArray(connections.connections)?connections.connections:[];
    const rows=Array.isArray(tokens)?tokens:tokens.items||[];
    const created=value=>value?` · ${escape(date(value))}`:'';
    return `<details class="connection-management"${open?' open':''}><summary>${text('Manage connected apps and devices')}</summary><div class="connection-management-content"><h3>${text('Connected apps')}</h3>${connected.length?connected.map(item=>`<div class="connection-managed-row"><div><strong>${escape(item.name||item.client_name||'')}</strong><p>${escape(Array.isArray(item.scopes)?item.scopes.join(', '):'')}${created(item.created_at)}</p></div><button type="button" class="danger" data-action="revoke-connection" data-id="${escape(item.id)}">${text('Revoke')}</button></div>`).join(''):`<p class="connection-muted">${text('No connected apps')}</p>`}<h3>${text('Device tokens')}</h3><p class="connection-muted">${text('Use a separate named token for each computer or harness. Revoking one token does not disconnect the others.')}</p><div class="connection-step-actions"><button type="button" data-connection-create-token>${text('Create token')}</button></div>${rows.length?rows.map(item=>`<div class="connection-managed-row"><div><strong>${escape(item.name)}</strong><p>${text(item.scope==='materials:manage'?'Manage materials':'Read materials')}${created(item.created_at)}</p></div><button type="button" class="danger" data-action="revoke-token" data-id="${escape(item.id)}">${text('Revoke')}</button></div>`).join(''):`<p class="connection-muted">${text('No device tokens')}</p>`}</div></details>`;
  }

  function render() {
    if(!current())return;
    const managementOpen=root.querySelector('.connection-management')?.open;
    copySequence++;copySource=undefined;
    root.dataset.publishProvider=selected;copies={};
    const provider=providers.find(item=>item.id===selected);
    root.innerHTML=`<section class="connection-page" data-page="connection"><header class="connection-intro"><h2>${text('Choose your assistant')}</h2><p>${text('Choose where you work. We’ll show what to copy, where to paste it and how to check the connection.')}</p></header><div class="connection-provider-picker" role="group" aria-label="${text('Choose your assistant')}">${providers.map(item=>`<button type="button" data-connection-provider="${item.id}" aria-pressed="${item.id===selected}"><strong>${escape(item.id==='other'?t('Other'):item.name)}</strong><span>${text(item.kind==='local'?'On your computer':item.kind==='cloud'?'Connected app':'Another app')}</span></button>`).join('')}</div><div class="connection-provider-panel">${provider.kind==='local'?local(provider):provider.kind==='cloud'?cloud(provider):other()}<div class="connection-example"><strong>${text('Try asking your assistant')}</strong><p>${text('Publish this finished result and give me a link I can share.')}</p>${button('example','Copy example request')}</div>${copyStatus()}</div>${management(managementOpen)}</section>`;
    mounted.dismissLimits = null;
    if (provider.kind==='cloud') bindUploadLimits();
    copies.example=t('Publish this finished result and give me a link I can share.');
  }

  root.innerHTML=`<section class="connection-page" data-page="connection"><p role="status">${text('Loading…')}</p></section>`;
  root.addEventListener('click',async event=>{
    if(!current())return;
    const trigger=event.target.closest('button');if(!trigger||!root.contains(trigger)||trigger.disabled)return;
    try {
      if(trigger.hasAttribute('data-open-upload-limits')){await onOpenLimits();return;}
      if(trigger.dataset.connectionProvider){
        const provider=providers.find(item=>item.id===trigger.dataset.connectionProvider);
        if(provider && provider.id!==selected){
          const restoreFocus=root.ownerDocument?.activeElement===trigger;
          selected=provider.id;render();
          if(restoreFocus)root.querySelector(`[data-connection-provider="${selected}"]`)?.focus({preventScroll:true});
        }
        return;
      }
      if(trigger.dataset.connectionCopy){const value=copies[trigger.dataset.connectionCopy];if(typeof value==='string'&&value)await copy(value,trigger);return;}
      if(trigger.hasAttribute('data-connection-close-copy')){
        root.querySelector('.connection-manual-copy').hidden=true;
        if(copySource?.isConnected)copySource.focus?.();
        copySource=undefined;
        return;
      }
      if(trigger.hasAttribute('data-connection-create-token'))await onCreateToken();
    }catch(error){if(current())onError(error);}
  },{signal:mounted.controller.signal});
  root.ownerDocument?.addEventListener('pointerdown', event => {
    if (current()) mounted.dismissLimits?.(event);
  }, {signal:mounted.controller.signal});
  try {
    [distribution,connections,tokens,limits]=await Promise.all([api('/distribution'),api('/connections'),api('/tokens'),api('/capabilities').then(value=>value.limits)]);
    if(!current())return;
    if(!distribution||typeof distribution.instance_url!=='string'||typeof distribution.mcp_url!=='string'||!connections||typeof connections.enabled!=='boolean'||!Array.isArray(connections.connections)||!Array.isArray(tokens)&&!Array.isArray(tokens?.items))throw new Error(t('Could not complete the request.'));
    render();
  }catch(error){if(current())onError(error);}
}
