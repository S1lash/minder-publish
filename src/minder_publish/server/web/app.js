import { mountCapacity } from './capacity.js';
import { mountStatistics } from './statistics.js';
import { loadMessages, t, translateStatic } from './i18n.js';
import { mountTooltips } from './tooltips.js';
import { mountConnection } from './connection.js';
import { mountSessionActivity } from './session.js';
import { confirmOwner, cancelOwnerConfirmation } from './reauth.js';
import { safeReturnTo } from './routes.js';
import { copyOwnerSecret } from './secret-copy.js';
import { copyResolvedLink, mountManualCopy } from './link-copy.js';
import { linkEndpoint, mountPublicationLink } from './publication-link.js';
import { mountPasswordEditors, clearPasswordEditors, passwordEditorControl } from './password-editor.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const showManualCopy = mountManualCopy($('#manual-copy-dialog'));
const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const MiB = 1024 ** 2, GiB = 1024 ** 3, DAY = 86400;
const prefs = window.PublishPreferences;
const libraryStates = [['active','Available'],['paused','Paused'],['expired','Expired'],['deleted','Files deleted']];
const defaultLibraryStates = ['active','paused'];
const state = { session:null, settings:null, page:'materials', items:[], total:0, offset:0, limit:50, query:'', kind:'', states:[...defaultLibraryStates], layout:'list', generation:0, dialogEpoch:0, upload:null, listRequest:null, settingsRequest:null };
const publicationLink = mountPublicationLink({api, getSession:()=>state.session, t, escape, onError:showError, errorMessage, showManualCopy});
const paths = {
  materials:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  settings:'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>',
  connection:'<path d="m8 8-4 4 4 4m8-8 4 4-4 4m-3-11-2 22"/>',
  sidebar:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M9 5v14"/><path class="sidebar-direction" d="m16 9.5-2.5 2.5 2.5 2.5"/>',
  owner:'<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  list:'<path d="M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1"/>',
  chevron:'<path d="m14 6-6 6 6 6"/>', close:'<path d="m6 6 12 12M6 18 18 6"/>',
  preview:'<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  previewAction:'<rect x="2.5" y="4.5" width="19" height="15" rx="2"/><path d="m10 9 5 3-5 3z"/>',
  open:'<path d="M14 3h7v7m0-7L10 14M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/>',
  link:'<path d="m10 14 4-4m-6 5-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 3 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/><circle cx="12" cy="15" r="1"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V4a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h4"/>',
  filter:'<path d="M4 6h16M7 12h10M10 18h4"/>',
  more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  file:'<path d="M6 2h8l5 5v15H6zM14 2v6h5M9 12h7m-7 4h7"/>',
  site:'<rect x="2" y="3" width="20" height="18" rx="2"/><path d="M2 8h20M6 5h.1m3 0h.1m-1 7-3 3 3 3m8-6 3 3-3 3"/>'
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.file}</svg>`;
const label = key => escape(t(key));
const accessName = value => ({private:'Only me',link:'Anyone with the link',password:'Password-protected'}[value] || value);
const descriptions = {materials:'Your results, files and complete sites.',settings:'Manage publication defaults, storage and security.',connection:'Connect your assistant to your own instance.'};
const pageNames = {materials:'Materials',settings:'Settings',connection:'Connection'};
const formChange = () => document.dispatchEvent(new Event('publish:formchange'));
function date(value) {
  if (!value) return t('No expiry');
  const timezone = state.settings?.defaults.timezone;
  return new Intl.DateTimeFormat(document.documentElement.lang, {dateStyle:'medium',timeStyle:'short',...(timezone && timezone !== 'browser' ? {timeZone:timezone} : {})}).format(new Date(value));
}
function bytes(value) {
  if (!Number.isFinite(Number(value))) return '—';
  const number = Number(value), unit = number >= GiB ? 'GiB' : number >= MiB ? 'MiB' : 'KiB';
  return `${new Intl.NumberFormat(document.documentElement.lang,{maximumFractionDigits:1}).format(number / (unit === 'GiB' ? GiB : unit === 'MiB' ? MiB : 1024))} ${unit}`;
}
function notify(key) {
  const output = $('#toast'); clearTimeout(notify.timer); output.textContent = t(key); output.hidden = false;
  notify.timer = setTimeout(() => { output.hidden = true; },5000);
}
class RequestError extends Error {
  constructor(problem,status) { super(problem.detail || 'Could not complete the request.'); this.problem=problem;this.status=status; }
}
function errorMessage(error) {
  if (error.name === 'AbortError') return t('Upload cancelled.');
  if (error instanceof RequestError) {
    if (error.status === 401) return t(state.session?.authenticated ? 'Your session expired. Sign in again.' : 'Invalid username or password.');
    if (error.status === 403) return t('Request rejected. Refresh the page and try again.');
    if (error.status === 429) return t('Too many attempts. Try again later.');
    return `${t('Could not complete the request.')}${error.problem.detail ? ` ${error.problem.detail}` : ''}`;
  }
  return error instanceof TypeError ? t('The server is unavailable. Check your connection and try again.') : error.message;
}
function showError(error, target = $('#dialog').open ? $('#dialog-error') : $('#page-error')) {
  const message=errorMessage(error);
  if (error instanceof RequestError && error.status === 401 && state.session?.authenticated) {
    clearOwnerState();setPath('/login',true);renderLogin();target=$('#page-error');
  }
  if (!target) target = $('#page-error');
  target.textContent = message; target.hidden = false;
  if (target.id === 'dialog-error') target.scrollIntoView({block:'nearest'});
}
async function api(path, {method='GET',body,signal,reauth=true} = {}) {
  const session=state.session;
  const headers = {'Accept':'application/json'};
  if (body !== undefined) headers['Content-Type']='application/json';
  if (method !== 'GET' && state.session?.csrf_token) headers['X-CSRF-Token']=state.session.csrf_token;
  const response = await fetch(`/api/v1${path}`, {method,headers,credentials:'same-origin',body:body === undefined ? undefined : JSON.stringify(body),signal});
  if (response.status === 204) return null;
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if(reauth&&response.status===403&&result.code==='reauthentication_required'&&session?.authenticated&&state.session===session){
      await confirmOwner({session,getSession:()=>state.session,t,escape,request:password=>api('/auth/reauth',{method:'POST',body:{password},reauth:false})});
      if(state.session!==session)throw new Error(t('Your session expired. Sign in again.'));
      return api(path,{method,body,signal,reauth:false});
    }
    throw new RequestError(result,response.status);
  }
  if (!Object.hasOwn(result,'data')) throw new Error(t('Could not complete the request.'));
  return result.data;
}
function button(action,key,classes='') { return `<button type="button" class="${classes}" data-action="${action}">${label(key)}</button>`; }
function field(name,key,control,help='') { return `<div class="form-field"><label for="${name}">${label(key)}</label>${control}${help ? `<small>${label(help)}</small>` : ''}</div>`; }
function select(name,choices,value,attributes='') { return `<select id="${name}" name="${name}" ${attributes}>${choices.map(([key,text])=>`<option value="${escape(key)}" ${String(value) === String(key) ? 'selected' : ''}>${label(text)}</option>`).join('')}</select>`; }
function number(name,value,{min=1,max,step=1,required=true}={}) { return `<input id="${name}" name="${name}" type="number" min="${min}" ${max === undefined ? '' : `max="${max}"`} step="${step}" value="${escape(value ?? '')}" ${required ? 'required' : ''}>`; }
function checkbox(name,key,checked) { return `<label class="checkbox-label"><input type="checkbox" name="${name}" id="${name}" ${checked ? 'checked' : ''}><span>${label(key)}</span></label>`; }
function passwordEditor(name,required,help) {
  return field(name,'New publication password',passwordEditorControl({name,required,help,t,escape,copyIcon:icon('copy')}));
}
function clearPublicationPasswordFields() {
  clearPasswordEditors($('#dialog'));
  const input=$('#saved-password-copy'),panel=$('[data-password-fallback]');
  if(input)input.value='';
  if(panel)panel.hidden=true;
}
function dialog(title,body,actions='',wide=false,tabs='') {
  publicationLink.close();clearPublicationPasswordFields();state.dialogEpoch++;
  const root=$('#dialog'); if (root.open) root.close(); delete root.material; delete root.dataset.materialId; root.classList.toggle('wide',wide);root.classList.toggle('material-dialog',!!tabs);
  root.innerHTML=`<header class="dialog-head"><div><h2 id="dialog-title">${escape(title)}</h2></div><button type="button" class="icon-button" data-action="close" aria-label="${label('Close')}" data-tooltip="${label('Close')}">${icon('close')}</button></header>${tabs}<div class="dialog-body"><div id="dialog-error" class="callout error" role="alert" hidden></div>${body}</div>${actions ? `<footer class="dialog-actions">${actions}</footer>` : ''}`;
  root.showModal();mountPasswordEditors({root,getSession:()=>state.session,getDialogEpoch:()=>state.dialogEpoch,t,onCopied:()=>notify('Entered password copied.'),onError:showError});formChange();
}
function safeURL(value) { const url=new URL(value,location.href);if (!['http:','https:'].includes(url.protocol)) throw new Error(t('Could not complete the request.'));return url.href; }
let copyTextSequence=0;
async function copyText(text,source,heading='Copy this link') {
  const attempt=++copyTextSequence;
  const session=state.session, ownerDialog=source.closest('dialog');
  const isCurrent=()=>attempt===copyTextSequence && state.session===session && source.isConnected && (!ownerDialog || ownerDialog.open);
  try {
    await navigator.clipboard.writeText(text);
    if (isCurrent()) notify('Copied.');
  } catch {
    if (isCurrent()) showManualCopy(text, t(heading));
  }
}
function updateMaterialLink(snapshot) {
  const update = material => {
    if (material?.id === snapshot.id) {
      material.stable_url = snapshot.stable_url;
      material.link_name = snapshot.link_name;
    }
  };
  update(state.items.find(material => material.id === snapshot.id));
  const root = $('#dialog');
  if (root.open && root.material?.id === snapshot.id) {
    update(root.material);
    $('[data-material-address]',root).textContent = snapshot.stable_url;
    for (const link of $$('[data-material-link-query]',root)) {
      const current = new URL(safeURL(snapshot.stable_url));
      const [key,value] = link.dataset.materialLinkQuery.split('=');
      current.searchParams.set(key,value);
      link.href = current.href;
    }
  }
  for (const row of $$('.material[data-id]')) {
    if (row.dataset.id === snapshot.id) {
      const link = $('.open-material', row);
      if (link) link.href = safeURL(snapshot.stable_url);
    }
  }
}
async function copyMaterialLink(id, source) {
  const attempt = ++copyTextSequence;
  const session = state.session, parent = source.closest('dialog');
  const isCurrent = () => attempt === copyTextSequence && state.session === session && source.isConnected && (!parent || parent.open);
  try {
    await copyResolvedLink({
      isCurrent,
      load: async () => {
        const value = await api(linkEndpoint(id));
        if (isCurrent()) {
          updateMaterialLink(value);
          const jobOpen = $('#job-open-link');
          if (jobOpen) jobOpen.href = safeURL(value.stable_url);
        }
        return safeURL(value.stable_url);
      },
      onCopied: () => notify('Copied.'),
      fallback: url => showManualCopy(url,t('Copy this link')),
    });
  } catch (error) {
    if (isCurrent()) showError(error);
  }
}
async function copyMaterialPassword(material,target) {
  const session=state.session,epoch=state.dialogEpoch,root=$('#dialog');
  const isCurrent=()=>state.session===session&&!!session?.authenticated&&state.dialogEpoch===epoch&&root.open&&root.dataset.materialId===material.id;
  if(!isCurrent())return;
  target.disabled=true;
  try {
    await copyOwnerSecret({
      isCurrent,
      load:()=>api(`/publications/${encodeURIComponent(material.id)}/password-copy`,{method:'POST'}),
      write:value=>navigator.clipboard.writeText(value),
      onCopied:()=>notify('Password copied.'),
      fallback:value=>{
        const panel=$('[data-password-fallback]',root),input=$('#saved-password-copy',root);
        if(!panel||!input)return;
        panel.hidden=false;input.value=value;input.focus();input.select();
      }
    });
  } catch(error) {if(isCurrent())showError(error);}
  finally {if(isCurrent())target.disabled=false;}
}
function refreshThemeControl() {
  const target=document.documentElement.dataset.theme==='dark'?'light':'dark';
  $('#theme-label').textContent=t('Theme');
  $('#theme-button').setAttribute('aria-label',`${t('Theme')}: ${t('Change theme')} — ${t(target==='dark'?'Dark':'Light')}`);
}
function refreshChrome() {
  const surface=state.session?.authenticated?'owner':(location.pathname==='/login'||/^\/upload\/[a-f0-9]{32}$/.test(location.pathname))?'login':'public';
  document.documentElement.dataset.surface=surface;
  translateStatic();
  $('#language-select').value=document.documentElement.lang;
  $('#language-select').setAttribute('aria-label',t('Language'));
  $('.nav').setAttribute('aria-label',t('Sections'));
  refreshThemeControl();
  const expanded=prefs.read('mp_sidebar','expanded') === 'expanded';$('#sidebar-toggle').setAttribute('aria-expanded',String(expanded));$('#sidebar-toggle').setAttribute('aria-label',t(expanded?'Collapse menu':'Expand menu'));
  const ownerButton=$('#owner-button'),signedIn=!!state.session?.authenticated;
  $('#owner-label').textContent=t(signedIn?'Account':'Sign in');
  ownerButton.setAttribute('aria-label',t(signedIn?'Account':'Owner sign-in'));
  if(signedIn)ownerButton.setAttribute('aria-haspopup','dialog');
  else ownerButton.removeAttribute('aria-haspopup');
  $$('[data-nav]').forEach(element=>{element.setAttribute('aria-label',t(pageNames[element.dataset.nav]));if(element.dataset.nav===state.page)element.setAttribute('aria-current','page');else element.removeAttribute('aria-current');});
  $('#view-title').textContent=t(pageNames[state.page]);$('#view-description').textContent=t(descriptions[state.page]);
  document.title=surface==='public'?'minder publish.':`${t(surface==='login'?'Owner sign-in':pageNames[state.page])} · minder publish.`;
  $('#create-material').hidden=!state.session?.authenticated;
  capacity?.render(state.session?.authenticated ? state.settings : null);
}
function setPath(path,replace=false) {
  if(location.pathname!==path||location.search||location.hash)history[replace?'replaceState':'pushState'](null,'',path);
}
function clearOwnerState() {
  cancelMaterialRequest();
  clearPublicationPasswordFields();cancelOwnerConfirmation();state.dialogEpoch++;
  if(state.upload){state.upload.cancelling=true;state.upload.controller.abort();state.upload=null;}
  state.session={authenticated:false};state.settings=null;state.items=[];state.total=0;state.offset=0;
  state.query='';state.kind='';state.states=[...defaultLibraryStates];state.page='materials';state.generation++;
  publicationLink.close();$('#manual-copy-dialog').close();$('#dialog').close();$('#toast').hidden=true;
}
function renderPublic() {
  refreshChrome();
  $('#view').innerHTML=`<section class="guest-home" data-page="public"><h1>${label('Shared files and sites')}</h1><p>${label('Open the link you received to view a material.')}</p></section>`;
  formChange();
}
function renderLogin() {
  refreshChrome();$('#create-material').hidden=true;
  $('#view').innerHTML=`<section class="login-panel" data-page="login"><a class="guest-back" href="/">${label('Back to the homepage')}</a><h1>${label('Owner sign-in')}</h1>${state.session?.setup_required ? `<p class="callout">${label('The operator must create the owner account on the server first.')}</p>` : `<form id="login-form">${field('username','Username','<input id="username" name="username" autocomplete="username" maxlength="200" required>')}${field('password','Password','<input id="password" name="password" type="password" autocomplete="current-password" required>')}<button class="primary" type="submit">${label('Sign in')}</button></form>`}</section>`;
  $('#login-form')?.addEventListener('submit',async event=>{
    event.preventDefault();const form=event.currentTarget,submit=$('button',form);submit.disabled=true;
    try { await api('/auth/login',{method:'POST',body:{username:form.elements.username.value,password:form.elements.password.value}});form.elements.password.value='';state.session=await api('/session');state.settings=await api('/settings');const destination=safeReturnTo(new URLSearchParams(location.search).get('return_to'))||safeReturnTo(location.pathname)||'/';if(destination.startsWith('/oauth/')||destination.startsWith('/viewer/')||destination.startsWith('/p/')){location.assign(destination);return;}setPath(destination,true);$('#page-error').hidden=true;await navigate('materials'); }
    catch(error){form.elements.password.value='';showError(error);}
    finally {submit.disabled=false;}
  });formChange();
}
async function navigate(page,{settingsRequest=state.settingsRequest}={}) {
  if (!pageNames[page]) return;
  cancelMaterialRequest();clearPublicationPasswordFields();state.generation++;state.dialogEpoch++;state.page=page;$('#page-error').hidden=true;
  if (!state.session?.authenticated) {if(location.pathname==='/login'||/^\/upload\/[a-f0-9]{32}$/.test(location.pathname))renderLogin();else renderPublic();return;}
  const session=state.session;
  if(page!=='materials'||/^\/upload\//.test(location.pathname))await settingsRequest;
  if(state.session!==session||state.page!==page)return;
  if(location.pathname==='/login')setPath('/',true);
  refreshChrome();
  if(/^\/upload\/[a-f0-9]{32}$/.test(location.pathname)){await renderUploadJob(location.pathname.split('/')[2]);return;}
  if (page==='materials') {renderLibrary();await loadMaterials({settingsRequest});if(state.session!==session||state.page!==page)return;const requested=new URLSearchParams(location.search).get('material');if(requested){history.replaceState(null,'','/');await details(requested);}}
  if (page==='settings') renderSettings();
  if (page==='connection') await renderConnection();
}
function statusFilter() {
  return `<details class="status-filter" id="status-filter"><summary aria-labelledby="library-status-label library-status-count">${icon('filter')}<span id="library-status-label" class="status-filter-label">${label('Status')}</span> <span id="library-status-count" class="status-filter-count">${state.states.length}</span><span class="status-filter-chevron" aria-hidden="true"></span></summary><div class="status-filter-panel"><fieldset><legend>${label('Show materials')}</legend>${libraryStates.map(([value,key])=>`<label class="checkbox-label"><input type="checkbox" name="library-state" value="${value}" ${state.states.includes(value)?'checked':''}><span>${label(key)}</span></label>`).join('')}</fieldset><p>${label('Expired and deleted files are hidden by default.')} ${label('Files deleted shows retained records. Permanently deleted materials cannot be searched.')}</p></div></details>`;
}
function renderLibrary() {
  $('#view').innerHTML=`<section data-page="materials">
    <div class="library-toolbar">
      <label class="search">${icon('search')}<input id="search" type="search" aria-label="${label('Search materials')}" placeholder="${label('Title, description or filename')}" value="${escape(state.query)}"></label>
      <div class="library-kinds">${[['','All'],['file','Files'],['site','Sites']].map(([value,key])=>`<button type="button" data-kind="${value}" aria-pressed="${state.kind===value}">${label(key)}</button>`).join('')}</div>
      ${statusFilter()}
      <div class="segmented library-layout">${[['list','List'],['grid','Grid']].map(([value,key])=>`<button type="button" data-layout="${value}" aria-label="${label(key)}" aria-pressed="${state.layout===value}">${icon(value==='grid'?'materials':'list')}</button>`).join('')}</div>
    </div>
    <p id="material-count" class="storage-stat" role="status"></p>
    <div id="materials" class="materials ${state.layout}"></div>
    <div id="pagination" class="pagination"></div>
  </section>`;
  const search=$('#search');
  let timer;
  search.addEventListener('input',event=>{
    state.query=event.target.value;state.offset=0;cancelMaterialRequest();clearTimeout(timer);
    timer=setTimeout(()=>{if(search.isConnected)loadMaterials().catch(showError);},250);
  });
  const filter=$('#status-filter');
  filter.addEventListener('change',()=>{
    state.states=$$('input:checked',filter).map(input=>input.value);state.offset=0;
    $('.status-filter-count',filter).textContent=state.states.length;
    loadMaterials().catch(showError);
  });
  filter.addEventListener('keydown',event=>{if(event.key==='Escape'&&filter.open){event.preventDefault();filter.open=false;$('summary',filter).focus();}});
  filter.addEventListener('focusout',event=>{
    const next=event.relatedTarget;
    // WebKit can focus the containing landmark while activating a label.
    if(next && !next.contains(filter) && !filter.contains(next))filter.open=false;
  });
  formChange();
}
function materialStatus(material) {
  if (material.content_state==='purged') return 'Files deleted';
  if (material.content_state==='purging') return 'Deleting files';
  if (material.content_state==='purge_pending') return 'Deletion pending';
  if (material.library_status==='deleted') return 'Files deleted';
  if (material.library_status==='expired' || (material.expires_at && new Date(material.expires_at).getTime()<=Date.now())) return 'Expired';
  if (material.paused) return 'Paused';
  return 'Available';
}
function materialName(material) {
  for (const value of [material.title,material.filename,material.id]) {
    if (typeof value==='string' && value.trim()) return value.trim();
  }
  return '';
}
function iconAction(material,action,key,glyph) { return `<button type="button" class="icon-button ${action}-material" data-action="${action}" data-id="${escape(material.id)}" aria-label="${label(key)}: ${escape(materialName(material))}" data-tooltip="${label(key)}">${icon(glyph)}</button>`; }
function accessBadge(material) {
  const access=['private','password','link'].includes(material.access)?material.access:'private';
  return `<span class="access-badge access-${access}">${icon({private:'owner',password:'lock',link:'link'}[access])}<span>${label(accessName(access))}</span></span>`;
}
function libraryEmptyState() {
  if(!state.states.length)return `<div class="empty"><h2>${label('Choose a status')}</h2><p>${label('Select at least one status to show materials.')}</p></div>`;
  const filtered=state.query||state.kind||state.states.join(',')!==defaultLibraryStates.join(',');
  if(filtered)return `<div class="empty"><h2>${label('Nothing found')}</h2><p>${label('Try another search or clear the filters.')}</p>${button('clear-filters','Clear filters')}</div>`;
  return `<div class="empty"><h2>${label('No active materials')}</h2><p>${label('Publish a material or include expired and deleted files in the status filter.')}</p>${button('publish','Publish','primary')}</div>`;
}
function renderRows() {
  const root=$('#materials');if(!root)return;root.className=`materials ${state.layout}`;
  root.innerHTML=state.items.length?state.items.map(material=>{
    const present=material.content_state==='present';
    const coverKind=material.kind==='site'?'site':'file';
    const cover=present&&material.thumbnail_state==='ready'?`<img src="/api/v1/publications/${encodeURIComponent(material.id)}/thumbnail" alt="" loading="lazy" data-thumbnail="${coverKind}">`:icon(coverKind);
    const guests=material.guest_open_count;
    const owner=material.owner_open_count;
    const total=openingCount(guests+owner);
    const breakdown=`${t('Guests')} ${openingCount(guests)} · ${t('You')} ${openingCount(owner)}`;
    const name=escape(materialName(material));
    const id=escape(material.id);
    return `<article class="material" data-id="${id}">
      <button type="button" data-action="details" data-id="${id}" aria-label="${escape(t('Open details: {title}',{title:materialName(material)}))}" class="material-cover">${cover}</button>
      <div class="material-content">
        <button type="button" class="material-title" data-action="details" data-id="${id}" ${material.summary?'data-tooltip-overflow=".material-summary"':''}><span>${name}</span></button>
        <p class="material-summary" ${material.summary?'data-tooltip-overflow':''}>${escape(material.summary||'')}</p>
        <div class="material-file-row">
          <span class="material-file">${escape(material.filename)}</span>
          <button type="button" class="material-statistics" aria-label="${label('Opening statistics')} · ${total}" data-tooltip="${escape(breakdown)}" data-tooltip-toggle>${icon('preview')}<strong>${total}</strong></button>
        </div>
      </div>
      <div class="material-meta">
        <span class="material-access">${accessBadge(material)}<small class="material-status">${label(materialStatus(material))}</small></span>
        <span class="material-expiry">${escape(material.expires_at?t('Until {date}',{date:date(material.expires_at)}):t('No expiry'))}</span>
        <span class="material-size">${present?bytes(material.size_bytes):'—'}</span>
      </div>
      <div class="material-actions">
        ${present?`${iconAction(material,'preview','Preview','previewAction')}<a class="icon-button open-material" href="${escape(safeURL(material.stable_url))}" target="_blank" rel="noopener noreferrer" aria-label="${label('Open in a new tab')}: ${name}" data-tooltip="${label('Open in a new tab')}">${icon('open')}</a>`:''}
        ${iconAction(material,'copy','Copy link','link')}${iconAction(material,'details','Details','more')}
      </div>
    </article>`;
  }).join(''):libraryEmptyState();
  $('#material-count').textContent=t('{count} of {total}',{count:Math.min(state.offset+state.items.length,state.total),total:state.total});
  $('#pagination').innerHTML=state.total>state.limit?`<button type="button" data-action="previous" ${state.offset===0?'disabled':''}>${label('Previous')}</button><span>${state.offset+1}–${state.offset+state.items.length} / ${state.total}</span><button type="button" data-action="next" ${state.offset+state.limit>=state.total?'disabled':''}>${label('Next')}</button>`:'';
  $$('[data-layout]').forEach(element=>element.setAttribute('aria-pressed',String(element.dataset.layout===state.layout)));
}
function cancelMaterialRequest() {
  state.listRequest?.abort();state.listRequest=null;state.generation++;
}
async function loadMaterials({settingsRequest=state.settingsRequest}={}) {
  cancelMaterialRequest();
  const generation=state.generation;
  if(state.page!=='materials'||!state.session?.authenticated)return;
  if(!state.states.length){await settingsRequest;if(generation!==state.generation||state.page!=='materials'||!state.session?.authenticated)return;state.items=[];state.total=0;renderRows();return;}
  const controller=new AbortController();state.listRequest=controller;
  const query=new URLSearchParams({q:state.query,kind:state.kind,states:state.states.join(','),offset:String(state.offset),limit:String(state.limit)});
  try{
    const [result]=await Promise.all([api(`/publications?${query}`,{signal:controller.signal}),settingsRequest]);
    if (generation!==state.generation || state.page!=='materials' || !state.session?.authenticated) return;
    if (state.offset && state.offset >= result.total) {
      state.offset=Math.max(0,Math.floor((result.total-1)/state.limit)*state.limit);
      if (result.total) return loadMaterials({settingsRequest});
    }
    state.items=result.items;state.total=result.total;renderRows();
  }catch(error){if(error.name!=='AbortError'&&generation===state.generation)throw error;}
  finally{if(state.listRequest===controller)state.listRequest=null;}
}
function policyFields(value,prefix='') {
  const retention=state.settings.ceilings.retention_days;
  const entry=(name,key,control,help='')=>field(`${prefix}${name}`,key,control,help);
  const makeSelect=(name,choices,selected,attributes='')=>select(`${prefix}${name}`,choices,selected,attributes).replace(`name="${prefix}${name}"`,`name="${name}"`);
  const makeNumber=(name,initial,max=retention)=>number(`${prefix}${name}`,initial,{min:0,max,step:'any'}).replace(`name="${prefix}${name}"`,`name="${name}"`);
  return `<div class="policy-fields">${entry('purge_mode','Delete files after access expires',makeSelect('purge_mode',[['immediate','Immediately'],['after','After a recovery period'],...(state.settings.operations.allow_never_purge?[['never','Never automatically']]:[])],value.purge_mode))}<div data-policy-after>${entry('purge_after','Recovery period',`<div class="inline-fields">${makeNumber('purge_after',value.purge_after_seconds/(value.purge_after_seconds<DAY?3600:DAY),retention*24)}${makeSelect('purge_unit',[['days','Days'],['hours','Hours']],value.purge_after_seconds<DAY?'hours':'days',`aria-label="${escape(t('Time unit'))}"`)}</div>`)}</div>${entry('metadata_policy','Library record after file deletion',makeSelect('metadata_policy',[['keep','Keep until manually deleted'],['with_content','Delete with files'],['after_purge','Delete after a set period']],value.metadata_policy))}<div data-policy-metadata>${entry('metadata_days','Record retention, days',makeNumber('metadata_days',value.metadata_after_purge_seconds/DAY))}</div><p><small>${label('Access ends on time. File deletion is a separate background step. Record retention starts after actual file deletion.')}</small></p></div>`;
}
function readPolicy(form) {
  const values=form.elements;
  const policy={purge_mode:values.purge_mode.value,metadata_policy:values.metadata_policy.value};
  if(policy.purge_mode==='after')policy.purge_after_seconds=nonNegativeInteger(Math.round(Number(values.purge_after.value)*(values.purge_unit.value==='hours'?3600:DAY)));
  if(policy.metadata_policy==='after_purge')policy.metadata_after_purge_seconds=nonNegativeInteger(Math.round(Number(values.metadata_days.value)*DAY));
  return policy;
}
function nonNegativeInteger(value) { const number=Number(value);if(value===''||!Number.isSafeInteger(number)||number<0)throw new Error(t('Please enter zero or a positive whole number.'));return number; }
function positive(value) { const number=Number(value);if(!Number.isFinite(number)||number<=0)throw new Error(t('Please enter a valid positive number.'));return number; }
function syncForm(form) {
  if(!form)return;
  const controls=form.elements;
  const show=(selector,visible)=>{const element=$(selector,form);if(!element)return;element.hidden=!visible;$$('input,select',element).forEach(input=>{input.disabled=!visible;});};
  show('[data-policy-after]',controls.purge_mode?.value==='after');
  show('[data-policy-metadata]',controls.metadata_policy?.value==='after_purge');
  show('[data-password]',controls.access?.value==='password');
  show('[data-expiry-days]',controls.expiry?.value==='days');
  show('[data-site-options]',controls.kind?.value==='site');
  formChange();
}
function expiryFields(mode='default',prefix='') {
  const choices=mode==='keep'?[['keep','Keep current expiry'],['never','No expiry'],['days','After a number of days']]:[['default','Use default'],['never','No expiry'],['days','After a number of days']];
  return `${field(`${prefix}expiry`,'Expiry',select(`${prefix}expiry`,choices,mode).replace(`name="${prefix}expiry"`,'name="expiry"'))}<div data-expiry-days>${field(`${prefix}expiry_days`,'Days',number(`${prefix}expiry_days`,7,{max:state.settings.ceilings.retention_days,step:'any'}).replace(`name="${prefix}expiry_days"`,'name="expiry_days"'))}</div>`;
}
function readExpiry(form) {
  const mode=form.elements.expiry.value;
  if(mode==='keep')return undefined;
  if(form.dataset.intentExpiry){const intent=JSON.parse(form.dataset.intentExpiry);return intent;}
  if(mode==='days')return {mode:'at',value:new Date(Date.now()+positive(form.elements.expiry_days.value)*DAY*1000).toISOString()};
  return {mode};
}
async function details(id) {
  const session=state.session,epoch=++state.dialogEpoch;
  const material=await api(`/publications/${encodeURIComponent(id)}`);
  if(state.session!==session || !state.session?.authenticated||state.dialogEpoch!==epoch)return;
  const files=Array.isArray(material.files)?material.files:[];
  const passwordCopy=material.access==='password'?`<div class="password-copy-control"><button type="button" data-action="copy-password" ${material.password_copy_available?'':'disabled'}>${icon('lock')}<span>${label('Copy saved password')}</span></button><small>${label(material.password_copy_available?'Copies the saved password. Save changes before copying a new password.':'This password has no saved copy. Set and save a new password to enable copying.')}</small><div data-password-fallback hidden><label for="saved-password-copy">${label('Copy this password')}</label><textarea id="saved-password-copy" readonly class="secret" autocomplete="off" spellcheck="false"></textarea><button type="button" data-action="hide-password">${label('Hide password')}</button></div></div>`:'';
  const url=new URL(safeURL(material.stable_url));
  const viewURL=(mode,value)=>{
    const target=new URL(url);
    target.searchParams.set(mode,value);
    return escape(target.href);
  };
  const present=material.content_state==='present';
  const sections=[
    ['overview','Overview'],
    ['access','Access and cleanup'],
    ['files','Files'],
    ['statistics','Statistics'],
  ];
  const counts=[
    ['guest','Guests',material.guest_open_count],
    ['owner','You',material.owner_open_count],
  ];
  const tabs=sections.map(([key,name],index)=>`<button type="button" role="tab" id="material-tab-${key}" data-material-tab="${key}" aria-controls="material-pane-${key}" aria-selected="${index===0}" tabindex="${index===0?0:-1}">${label(name)}</button>`).join('');
  const overview=`
    <div class="material-glance">
      <div class="material-glance-field"><small>${label('Access')}</small>${accessBadge(material)}</div>
      <div class="material-glance-field"><small>${label('Status')}</small><strong data-material-status>${label(materialStatus(material))}</strong></div>
      <div class="material-glance-field"><small>${label('Expiry')}</small><strong>${escape(date(material.expires_at))}</strong></div>
    </div>
    <section class="material-opening-summary" aria-labelledby="material-opening-heading">
      <div class="material-opening-heading"><h3 id="material-opening-heading">${label('Opening statistics')}</h3><p class="subtext">${label('Total since collection started')}</p></div>
      <div class="material-opening-counts">${counts.map(([audience,name,value])=>`<div class="material-opening-count"><span>${label(name)}</span><strong data-opening-count="${audience}">${openingCount(value)}</strong></div>`).join('')}</div>
      <button type="button" data-open-material-statistics>${label('View opening history')}</button>
    </section>
    <p class="subtext publication-link-address" data-material-address>${escape(url.href)}</p>
    <div class="material-operation-links material-quick-actions">
      ${button('copy','Copy link')}
      ${button('edit-link','Edit link')}
      ${present?`<a class="button" data-material-link-query="preview=tools" href="${viewURL('preview','tools')}" target="_blank" rel="noopener noreferrer">${label('Versions and owner tools')}</a><a class="button" data-material-link-query="guest=1" href="${viewURL('guest','1')}" target="_blank" rel="noopener noreferrer">${label('Check guest view')}</a>`:''}
    </div>
    <div class="material-edit-info">
      ${field('material-title','Title',`<input id="material-title" name="title" value="${escape(material.title)}" maxlength="${state.settings.limits.title_characters}" required>`)}
      ${field('material-summary','Description',`<textarea id="material-summary" name="summary" maxlength="${state.settings.limits.summary_characters}">${escape(material.summary)}</textarea>`,'Optional')}
    </div>`;
  const access=`
    ${field('access','Access',select('access',[['private','Only me'],['link','Anyone with the link'],['password','Password-protected']],material.access))}
    <div data-password>
      ${passwordEditor('new-password',!material.has_password,'Leave blank to keep the existing password.')}
      ${passwordCopy}
      ${field('material-hint','Password hint (public)',`<input id="material-hint" name="password_hint" maxlength="${state.settings.limits.password_hint_characters}" value="${escape(material.password_hint)}">`,'Shown before the password is entered. Do not include passwords or secrets.')}
    </div>
    ${expiryFields('keep','material-')}${policyFields(material,'material-')}
    <section class="material-operations">
      <h3>${label('Manage access and deletion')}</h3>
      <p class="subtext">${label('Pausing is reversible. Expiring closes access and follows the saved cleanup policy. Deleting removes the material and its files.')}</p>
      <div class="material-operation-links">
        ${present?`${button('pause',material.paused?'Resume access':'Pause access')}${material.expires_at?button('extend','Extend access'):''}${button('expire','Expire now')}`:''}
        ${button('delete','Delete material','danger')}
      </div>
    </section>`;
  const report=material.dependency_report;
  const fileDetails=`
    <div class="material-glance">
      <div class="material-glance-field"><small>${label('Filename')}</small><strong>${escape(material.filename||'—')}</strong></div>
      <div class="material-glance-field"><small>${label('Created')}</small><strong>${escape(date(material.created_at))}</strong></div>
      <div class="material-glance-field"><small>${label('Size')}</small><strong>${present?bytes(material.size_bytes):'—'}</strong></div>
      <div class="material-glance-field"><small>${label('Revision')}</small><strong>${escape(material.current_revision||'—')}</strong></div>
    </div>
    ${present?'':`<p class="callout warning">${label('The file is no longer stored. Upload a new revision to restore content.')}</p>`}
    <div class="material-operation-links material-quick-actions">
      ${present?`<a class="button" href="/api/v1/publications/${encodeURIComponent(material.id)}/download" download>${label(material.kind==='site'?'Download site ZIP':'Download original')}</a>`:''}
      ${button('replace','Replace files')}
    </div>
    ${report&&(material.kind==='site'||report.scanned_files||report.truncated||report.static_scan_complete===false)?dependencyReport(report):''}
    ${files.length?`<details class="material-manifest"><summary>${label('Files in this revision')} (${files.length})</summary><div class="manifest">${files.map(file=>escape(typeof file==='string'?file:file.path)).join('<br>')}</div></details>`:''}`;
  const panes=[overview,access,fileDetails,`<section id="material-statistics-panel" class="viewer-section"><p class="subtext" role="status">${label('Loading…')}</p></section>`];
  dialog(materialName(material),`
    <form id="material-form">
      ${sections.map(([key],index)=>`<section id="material-pane-${key}" role="tabpanel" tabindex="0" aria-labelledby="material-tab-${key}" ${index?'hidden':''}>${panes[index]}</section>`).join('')}
    </form>`,
    `${button('close','Close')}<button type="submit" form="material-form" class="primary">${label('Save')}</button>`,true,
    `<div class="material-tabs" role="tablist" aria-label="${label('Material details')}">${tabs}</div>`);
  const root=$('#dialog');root.dataset.materialId=material.id;root.material=material;
  const form=$('#material-form');
  const statistics=mountStatistics({
    root:$('#material-statistics-panel'),id:material.id,api,t,escape,date,lazy:true,
    isCurrent:()=>state.session===session&&root.open&&$('#material-form')===form,
    onError:error=>{
      const loading=$('#material-statistics-panel [role=status]',root);
      if(loading)loading.hidden=true;
      showError(error);
    },
    onUpdate:value=>{
      $('[data-opening-count="guest"]',form).textContent=openingCount(value.total);
      $('[data-opening-count="owner"]',form).textContent=openingCount(value.owner?.total);
    },
  });
  mountMaterialTabs(root,form,$('[form=material-form]',root),()=>statistics.then(panel=>panel.refresh()));
  syncForm(form);form.addEventListener('change',()=>syncForm(form));
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    const submit=$('[form=material-form]',$('#dialog'));submit.disabled=true;
    try {
      const values=form.elements, expiry=readExpiry(form);
      if(material.access==='private'&&values.access.value!=='private'&&!expiry)throw new Error(t('Choose an explicit expiry when sharing a private material.'));
      const patch={title:values.title.value,summary:values.summary.value,access:values.access.value,...readPolicy(form)};
      if(expiry)patch.expiry=expiry;
      if(values.access.value==='password'){patch.password_hint=values.password_hint.value;if(values.password.value)patch.password=values.password.value;}
      await api(`/publications/${encodeURIComponent(material.id)}`,{method:'PATCH',body:patch});values.password.value='';if(state.session!==session||!root.open||$('#material-form')!==form)return;root.close();notify('Saved.');await loadMaterials();
    } catch(error){if(state.session===session&&$('#material-form')===form)showError(error);}finally{submit.disabled=false;}
  });
}
function openingCount(value) {
  return Number.isFinite(value)?new Intl.NumberFormat(document.documentElement.lang).format(value):'—';
}
function mountMaterialTabs(root,form,save,openStatistics) {
  const tabs=$$('[data-material-tab]',root);
  const body=$('.dialog-body',root);
  const choose=(tab,focus=false)=>{
    for(const item of tabs){
      const active=item===tab;
      item.setAttribute('aria-selected',String(active));
      item.tabIndex=active?0:-1;
      $(`#${item.getAttribute('aria-controls')}`,form).hidden=!active;
    }
    const key=tab.dataset.materialTab;
    body.scrollTop=0;
    save.hidden=key!=='overview'&&key!=='access';
    if(focus)tab.focus();
    if(key==='statistics')void openStatistics();
  };
  for(const [index,tab] of tabs.entries()){
    tab.addEventListener('click',()=>choose(tab));
    tab.addEventListener('keydown',event=>{
      const last=tabs.length-1;
      const position={
        ArrowLeft:index===0?last:index-1,
        ArrowRight:index===last?0:index+1,
        Home:0,End:last,
      }[event.key];
      if(position===undefined)return;
      event.preventDefault();
      choose(tabs[position],true);
    });
  }
  $('[data-open-material-statistics]',form).addEventListener('click',()=>choose(tabs.find(tab=>tab.dataset.materialTab==='statistics'),true));
  form.addEventListener('invalid',event=>{
    if(event.target!==$(':invalid',form))return;
    const pane=event.target.closest('[role=tabpanel]');
    if(pane?.hidden)choose(tabs.find(tab=>tab.id===pane.getAttribute('aria-labelledby')));
  },true);
}
async function preview(id) {
  const session=state.session,epoch=++state.dialogEpoch;
  const material=state.items.find(item=>item.id===id)||await api(`/publications/${encodeURIComponent(id)}`);
  if(state.session!==session || !state.session?.authenticated||state.dialogEpoch!==epoch)return;
  const browserType=/^(?:text\/(?:html|plain|css|javascript)|application\/(?:json|pdf)|image\/[^;]+|audio\/[^;]+|video\/[^;]+)(?:;|$)/i.test(material.content_type||'');
  if(material.kind==='file'&&(material.preview==='download'||!browserType)){
    dialog(materialName(material),`<p>${label(material.preview==='download'?'This material is configured to download.':'This file type does not have a browser preview.')}</p><p><small>${escape(material.filename)} · ${bytes(material.size_bytes)}</small></p>`,`${button('close','Close')}<a class="primary download-button" href="/api/v1/publications/${encodeURIComponent(id)}/download" download>${label('Download original')}</a>`);
    return;
  }
  const url=new URL(safeURL(material.stable_url));url.searchParams.set('preview','1');
  dialog(materialName(material),`<iframe class="preview-frame" title="${label('Preview')}: ${escape(materialName(material))}" sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" src="${escape(url.href)}"></iframe>`,`${button('close','Close')}<a class="secondary" href="${escape(safeURL(material.stable_url))}" target="_blank" rel="noopener noreferrer">${label('Open in a new tab')}</a><a href="/api/v1/publications/${encodeURIComponent(id)}/download" download>${label(material.kind==='site'?'Download site ZIP':'Download original')}</a>`,true);
}
function uploadDialog(replacement=null,job=null) {
  const defaults=state.settings.defaults;
  const name= replacement ? 'Replace files' : 'Publish';
  const siteOptions=`<div data-site-options>${field('entry_path','Entry path',`<input id="entry_path" name="entry_path" value="${escape(defaults.site_entry)}" required>`)}${field('site_routing','Routing',select('site_routing',[['strict','Existing files only'],['spa','SPA page fallback']],defaults.site_routing),'Missing scripts, styles and media still return 404.')}${field('external_resources','External resources',select('external_resources',[['allow','Allow HTTPS and APIs'],['self_only','Build resources only']],defaults.external_resources),'External fonts, CDNs and APIs may stop working in the local-only profile.')}<p class="subtext">${label('Add publish-cover.png to the ZIP root to show a cover in the library.')}</p></div>`;
  dialog(t(name),`<form id="upload-form">${replacement?`<p>${escape(materialName(replacement))} · ${escape(replacement.filename)}</p><input name="kind" type="hidden" value="${escape(replacement.kind)}">`:field('kind','Publication type',select('kind',[['file','File'],['site','Static site ZIP']],'file'),'A ZIP is an ordinary file unless site mode is selected explicitly.')}${field('upload-file','Choose file',`<input id="upload-file" name="file" type="file" required ${replacement?.kind==='site'?'accept=".zip,application/zip"':''}>`)}${replacement&&replacement.content_state==='purged'?expiryFields('default','restore-'):''}${replacement?'':`${field('upload-title','Title',`<input id="upload-title" name="title" maxlength="${state.settings.limits.title_characters}" required>`)}${field('upload-summary','Description',`<textarea id="upload-summary" name="summary" maxlength="${state.settings.limits.summary_characters}"></textarea>`,'Optional')}${field('access','Access',select('access',[['private','Only me'],['link','Anyone with the link'],['password','Password-protected']],defaults.access))}<div data-password>${passwordEditor('upload-password',true,'Publication passwords are separate from your owner password.')}${field('upload-hint','Password hint (public)',`<input id="upload-hint" name="password_hint" maxlength="${state.settings.limits.password_hint_characters}">`,'Shown before the password is entered. Do not include passwords or secrets.')}</div>${expiryFields()}${siteOptions}<details><summary>${label('Link options')}</summary>${field('custom-link-name','Custom link name',`<input id="custom-link-name" name="key" type="text" minlength="2" maxlength="64" pattern="[a-z0-9](?:[a-z0-9]|-){1,63}" placeholder="test1" autocomplete="off" autocapitalize="off" spellcheck="false">`,'Optional: 2–64 lowercase letters, digits or dashes; start with a letter or digit. You can edit it after publishing. Short names are not secrets.')}</details><details><summary>${label('Access and cleanup')}</summary>${policyFields(defaults,'upload-')}</details>`}${replacement?field('change-summary','What changed?',`<input id="change-summary" name="change_summary" maxlength="${state.settings.operations.change_summary_characters}">`,'Optional short note for this version.'):''}<div id="upload-progress" hidden><progress class="upload-progress" max="100" value="0"></progress><p class="upload-status" role="status"></p></div><button class="primary" type="submit">${label(name)}</button></form>`,button('close','Cancel'),true);
  const form=$('#upload-form');
  if(job){const intent=job.intent;for(const key of ['kind','title','summary','access','entry_path'])if(intent[key]!==undefined&&form.elements[key])form.elements[key].value=intent[key];form.elements.kind.disabled=true;if(intent.expiry){form.dataset.intentExpiry=JSON.stringify(intent.expiry);form.elements.expiry.value=intent.expiry.mode==='at'?'days':intent.expiry.mode;if(intent.expiry.mode==='at')form.elements.expiry_days.value=Math.max(.001,(new Date(intent.expiry.value).getTime()-Date.now())/(DAY*1000));}form.elements.file.accept=intent.kind==='site'?'.zip,application/zip':'';}
  $('[type=submit]',form).textContent=t('Upload and review');syncForm(form);form.addEventListener('change',event=>{
    if(event?.target?.name==='expiry'||event?.target?.name==='expiry_days')delete form.dataset.intentExpiry;
    if(!replacement){const file=form.elements.file.files[0];if(file&&!form.elements.title.value)form.elements.title.value=file.name;form.elements.file.accept=form.elements.kind.value==='site'?'.zip,application/zip':'';}
    syncForm(form);
  });
  form.addEventListener('submit',async event=>{event.preventDefault();if(!form.reportValidity())return;await upload(form,replacement,job);});
}
async function renderUploadJob(id) {
  const session=state.session;
  const job=await api(`/upload-jobs/${encodeURIComponent(id)}`);
  if(state.session!==session||!session?.authenticated||location.pathname!==`/upload/${id}`)return;
  $('#create-material').hidden=true;$('#view-title').textContent=t('Upload requested by your assistant');$('#view-description').textContent=t('Choose a file here. Your assistant will receive the published link.');
  const complete=job.state==='published';
  const status={pending:'Ready to choose a file',uploading:'An upload is already in progress in this session.',published:'Published. Return to your assistant to continue.',expired:'This upload request expired. Ask your assistant for a new one.',cancelled:'Upload cancelled.',failed:'The upload failed. Ask your assistant for a new request.',revoked:'The requesting application no longer has access.'}[job.state]||'Could not complete the request.';
  $('#view').innerHTML=`<section class="upload-job-panel"><h2>${escape(job.intent.title||t('Choose file'))}</h2>${job.intent.summary?`<p>${escape(job.intent.summary)}</p>`:''}<p class="callout">${label(status)}</p><p>${label(job.intent.kind==='site'?'Static site ZIP':'File')} · ${label(accessName(job.intent.access||state.settings.defaults.access))}</p>${job.state==='pending'?`<button type="button" id="job-choose-file" class="primary">${label('Choose file')}</button>`:''}${complete?`<a id="job-open-link" class="button" href="${escape(safeURL(job.stable_url))}" target="_blank" rel="noopener noreferrer">${label('Open in a new tab')}</a> <button type="button" id="job-copy-link">${label('Copy link')}</button>`:''}<p><a href="/">${label('Materials')}</a></p></section>`;
  $('#job-choose-file')?.addEventListener('click',()=>uploadDialog(null,job));
  $('#job-copy-link')?.addEventListener('click',event=>copyMaterialLink(job.publication_id,event.currentTarget));
  formChange();
}
function sendFile(path,file,current) {
  return new Promise((resolve,reject)=>{
    const request=new XMLHttpRequest();current.request=request;
    request.open('PUT',`/api/v1${path}`);request.withCredentials=true;request.setRequestHeader('Content-Type','application/octet-stream');request.setRequestHeader('Accept','application/json');request.setRequestHeader('X-CSRF-Token',state.session.csrf_token);
    request.upload.onprogress=event=>{const progress=$('.upload-progress');if(progress&&event.lengthComputable)progress.value=event.loaded/event.total*100;};
    request.onload=()=>{let result;try{result=JSON.parse(request.responseText);}catch{result={};}if(request.status>=200&&request.status<300&&Object.hasOwn(result,'data'))resolve(result.data);else reject(new RequestError(result,request.status));};
    request.onerror=()=>reject(new TypeError('Network request failed'));
    request.onabort=()=>reject(new DOMException('Upload aborted','AbortError'));
    if(current.controller.signal.aborted){reject(new DOMException('Upload aborted','AbortError'));return;}
    current.controller.signal.addEventListener('abort',()=>request.abort(),{once:true});request.send(file);
  });
}
function dependencyReport(report) {
  const origins=Array.isArray(report.external_origins)?report.external_origins:[];
  const missing=Array.isArray(report.missing_assets)?report.missing_assets:[];
  const list=items=>`<ul>${items.map(item=>`<li><code>${escape(item)}</code></li>`).join('')}</ul>`;
  const incomplete=report.static_scan_complete===false||Boolean(report.truncated);
  const checked=Number(report.scanned_files)>0;
  const messages=[
    incomplete?'Some resources were not checked. Test the published page before sharing.':null,
    missing.length?'Some local files are missing. Include them in the upload, then try again.':null,
    origins.length?'This page uses resources from other sites. Check that they load for your audience.':null,
  ].filter(Boolean);
  return `<section class="dependency-report"><h3>${label('Page resource check')}</h3>
    ${messages.length?`<ul class="dependency-findings">${messages.map(message=>`<li>${label(message)}</li>`).join('')}</ul>`:''}
    <details><summary>${label(messages.length?'View checked resources':checked?'No issues found in the checked resources':'No HTML or CSS was checked')}</summary>
      <p><small>${label('Static HTML/CSS analysis only. JavaScript, runtime requests and browser behavior are not evaluated.')}</small></p>
      <p class="storage-stat">${escape(t('Files checked: {count} · {size}',{count:report.scanned_files??0,size:bytes(report.scanned_bytes??0)}))}</p>
      ${origins.length?`<h4>${label('Detected external origins')}</h4>${list(origins)}`:''}
      ${missing.length?`<h4>${label('Missing local assets')}</h4>${list(missing)}`:''}
    </details>
  </section>`;
}
function preparedReview(current,inspection) {
  const files=Array.isArray(inspection.files)?inspection.files:[];
  const total=files.reduce((sum,file)=>sum+(Number(file.size_bytes)||0),0);
  current.phase='review';
  dialog(t('Review publication'),`<p>${escape(current.filename)}</p><p class="callout">${label('Uploaded files are not live until you confirm. Cancel removes this pending upload.')}</p><div class="stat-strip"><div><strong>${files.length}</strong><small>${label('Files')}</small></div><div><strong>${bytes(total)}</strong><small>${label('Prepared size')}</small></div></div>${inspection.expires_at?`<p><small>${escape(t('Review before {date}',{date:date(inspection.expires_at)}))}</small></p>`:''}<details><summary>${label('Included files')} (${files.length})</summary><div class="manifest">${files.map(file=>`${escape(file.path)} · ${bytes(file.size_bytes)}`).join('<br>')}</div></details>${inspection.dependency_report&&(current.kind==='site'||inspection.dependency_report.scanned_files||inspection.dependency_report.truncated)?dependencyReport(inspection.dependency_report):`<p>${label('The file is ready to publish.')}</p>`}`,`${button('close','Cancel')}${button('commit-upload','Publish now','primary')}`,true);
}
async function completePrepared(current,session) {
  if(state.upload!==current||state.session!==session||current.cancelling)return;
  capacity?.invalidate();
  current.id=null;state.upload=null;$('#dialog').close();state.offset=0;notify('Published.');
  if(current.jobId)await renderUploadJob(current.jobId);else await navigate('materials');
}
async function checkPrepared() {
  const current=state.upload,session=state.session;if(!current||current.phase!=='unknown')return;
  const submit=$('[data-action="check-upload"]');submit.disabled=true;
  try {
    const result=await api(`/uploads/${encodeURIComponent(current.id)}`);
    if(state.upload!==current||state.session!==session||current.cancelling)return;
    if(result.state==='committed'){await completePrepared(current,session);return;}
    if(result.state==='prepared'){
      current.phase='review';submit.dataset.action='commit-upload';submit.textContent=t('Publish now');
      showError(new Error(t('This upload is prepared. You can publish this same upload.')));
    }else showError(new Error(t('The publication is not confirmed. Check again shortly; do not create a duplicate.')));
  }catch(error){if(state.upload===current&&state.session===session&&!current.cancelling)showError(error);}
  finally{submit.disabled=false;}
}
async function commitPrepared() {
  const current=state.upload,session=state.session;if(!current||current.phase!=='review')return;
  current.phase='committing';
  const submit=$('[data-action="commit-upload"]');submit.disabled=true;submit.textContent=t('Publishing…');$('#dialog-error').hidden=true;
  try {
    await api(`/uploads/${encodeURIComponent(current.id)}/commit`,{method:'POST',body:{},signal:current.controller.signal});
    if(state.upload!==current||state.session!==session||current.cancelling)return;
    await completePrepared(current,session);
  }catch(error){
    if(current.cancelling||state.upload!==current||state.session!==session)return;
    if(!(error instanceof RequestError)||error.status>=500){
      current.phase='unknown';submit.dataset.action='check-upload';submit.disabled=false;submit.textContent=t('Check publication status');
      showError(new Error(t('The publication outcome is unknown. Check this upload before creating another material.')));
      await checkPrepared();
    }else{current.phase='review';submit.disabled=false;submit.textContent=t('Publish now');showError(error);}
  }
}
async function cancelUpload() {
  const current=state.upload,session=state.session;if(!current||current.cancelling)return;
  current.cancelling=true;current.controller.abort();
  $$('button',$('#dialog')).forEach(button=>{button.disabled=true;});
  let result,failure;
  try {if(current.jobId)result=await api(`/upload-jobs/${encodeURIComponent(current.jobId)}`,{method:'DELETE'});else if(current.id)result=await api(`/uploads/${encodeURIComponent(current.id)}`,{method:'DELETE'});}
  catch(error){failure=error;}
  finally {
    if(state.upload!==current||state.session!==session)return;
    state.upload=null;
    $('#dialog').close();
    notify(result?.state==='committed'?'Published.':result?.state==='aborted'?'Upload cancelled.':'Upload stopped. Any pending server cleanup may finish in the background.');
    if(failure)showError(failure,$('#page-error'));
    if(state.session?.authenticated&&current.jobId)await renderUploadJob(current.jobId).catch(showError);else if(state.session?.authenticated&&state.page==='materials')await loadMaterials().catch(showError);
  }
}
async function upload(form,replacement,job=null) {
  if(state.upload)return;
  const session=state.session;
  const file=form.elements.file.files[0];if(!file){showError(new Error(t('Choose a file first.')));return;}
  const kind=form.elements.kind.value;
  if(kind==='site'&&!/\.zip$/i.test(file.name)){showError(new Error(t('Select a static-site ZIP archive.')));return;}
  const limit=kind==='file'?Math.min(state.settings.limits.file_bytes,state.settings.limits.upload_payload_bytes):state.settings.limits.upload_payload_bytes;
  if(file.size>limit){showError(new Error(t('The file exceeds the server upload limit.')));return;}
  const current={id:null,controller:new AbortController(),request:null,phase:'uploading',filename:file.name,kind,cancelling:false,jobId:job?.id};state.upload=current;
  const submit=$('[type=submit]',form);submit.disabled=true;$('#upload-progress').hidden=false;$('.upload-status').textContent=t('Uploading…');$('#dialog-error').hidden=true;
  try {
    const plan={kind,filename:file.name,size_bytes:file.size,supports_link_names:true};
    if(replacement){if(form.elements.change_summary?.value.trim())plan.change_summary=form.elements.change_summary.value.trim();plan.publication_id=replacement.id;plan.expected_revision=replacement.current_revision;if(replacement.content_state==='purged')plan.expiry=readExpiry(form);}
    else {
      Object.assign(plan,{title:form.elements.title.value,summary:form.elements.summary.value,access:form.elements.access.value,expiry:readExpiry(form),...readPolicy(form)});
      const key=form.elements.key?.value.trim();if(key)plan.key=key;
      if(plan.access==='password'){plan.password=form.elements.password.value;plan.password_hint=form.elements.password_hint.value;}
      if(kind==='site')Object.assign(plan,{entry_path:form.elements.entry_path.value,site_routing:form.elements.site_routing.value,external_resources:form.elements.external_resources.value});
    }
    const reservation=await api(job?`/upload-jobs/${encodeURIComponent(job.id)}/start`:'/uploads',{method:'POST',body:plan,signal:current.controller.signal});current.id=reservation.id;
    if(state.upload!==current||state.session!==session||current.cancelling)return;
    if(form.elements.password){form.elements.password.value='';form.elements.password.dispatchEvent(new Event('input',{bubbles:true}));}
    await sendFile(`/uploads/${encodeURIComponent(current.id)}/content`,file,current);
    if(state.upload!==current||state.session!==session||current.cancelling)return;
    $('.upload-status').textContent=t('Preparing publication…');$('.upload-progress').removeAttribute('value');
    current.phase='inspecting';
    const inspection=await api(`/uploads/${encodeURIComponent(current.id)}/inspect`,{method:'POST',body:{},signal:current.controller.signal});
    if(current.cancelling||state.upload!==current||state.session!==session)return;
    preparedReview(current,inspection);
  }catch(error){if(current.cancelling||state.upload!==current||state.session!==session)return;if(current.id)await api(`/uploads/${encodeURIComponent(current.id)}`,{method:'DELETE'}).catch(()=>{});showError(error);}
  finally {if(form.elements.password){form.elements.password.value='';form.elements.password.dispatchEvent(new Event('input',{bubbles:true}));}if(current.phase!=='review'&&!current.cancelling&&state.upload===current)state.upload=null;submit.disabled=false;}
}
function settingsRow(name,key,control,help='') {return `<div class="form-row"><label for="${name}">${label(key)}</label><div class="field">${control}${help?`<small>${label(help)}</small>`:''}</div></div>`;}
function settingsGroup(key,body,id) {return `<section class="settings-group"${id?` id="${id}" tabindex="-1"`:''}><div class="section-heading"><h2>${label(key)}</h2></div>${body}</section>`;}
function renderSettings() {
  const session=state.session;
  const {defaults:d,limits:l,ceilings:c,storage:s}=state.settings;
  const timezones=[['browser','Browser time zone'],['UTC','UTC']];if(!['browser','UTC'].includes(d.timezone))timezones.push([d.timezone,d.timezone]);
  const expiryControl=(name,key,seconds,{help='',required=true}={})=>settingsRow(name,key,number(
    name,seconds===null?'':seconds/DAY,{min:1/DAY,max:c.retention_days,step:'any',required}
  ),help);
  const publishing=`<p class="subtext">${label('New materials use these defaults when your agent, CLI or API omits a setting. An explicit choice applies to that material within the server’s limits. Existing materials keep their saved policy.')}</p>`+settingsRow('access','Access',select('access',[['private','Only me'],['link','Anyone with the link'],['password','Password-protected']],d.access))+
    expiryControl('private_ttl_days','Private expiry, days',d.private_ttl_seconds,{help:'Leave blank for no expiry.',required:false})+
    expiryControl('external_ttl_days','External link expiry, days',d.external_ttl_seconds);
  const keyBounds=state.settings.operations.publication_key_bounds;
  const keyLengthControl=(name,key)=>settingsRow(name,key,number(name,d[name],{min:keyBounds.minimum,max:keyBounds.maximum})+`<small>${escape(t('Allowed: {minimum}–{maximum} characters.',keyBounds))}</small>`);
  const linkNames=keyLengthControl('publication_key_min_characters','Minimum random name length')+
    keyLengthControl('publication_key_max_characters','Maximum random name length')+
    `<p class="subtext">${label('Automatic names start at the minimum length and grow on collisions, up to the maximum.')}</p><p><small>${label('Shorter names are easier to guess. Use password protection or Only me for confidential materials.')} ${label('Existing links and custom link names stay unchanged.')}</small></p>`;
  const historyControl=(name,key,help='')=>settingsRow(name,key,number(name,d[name],{min:0,max:c[name]}),help);
  const historySettings=historyControl('history_previous_versions','Previous versions to keep','Older versions count towards your storage quota.')+
    historyControl('history_retention_days','Keep previous versions, days')+
    historyControl('history_budget_percent','History share of storage budget, %','The earliest count, time or storage limit applies. Current files are never removed by history cleanup.')+
    `<p class="subtext">${label('Set any history limit to zero to stop retaining previous files. Lowering limits removes older files during cleanup; they cannot be recovered.')}</p>`;
  const viewing=`<div class="form-row">${checkbox('owner_auto_open','Open my protected materials without separate passwords',d.owner_auto_open)}</div>`+
    settingsRow('preview','File opening',select('preview',[['auto','Preview when supported'],['download','Download first']],d.preview))+
    `<div class="form-row">${checkbox('guest_counts','Count guest views',d.guest_counts)}</div><div class="form-row">${checkbox('owner_counts','Count my openings',d.owner_counts)}</div><div class="form-row">${checkbox('indexing','Allow search indexing of public links',d.indexing)}<small>${label('Private and password-protected materials always use noindex.')}</small></div>`+
    settingsRow('timezone','Date time zone',select('timezone',timezones,d.timezone));
  const site=settingsRow('site_entry','Entry path',`<input id="site_entry" name="site_entry" value="${escape(d.site_entry)}" required>`)+
    settingsRow('site_routing','Routing',select('site_routing',[['strict','Existing files only'],['spa','SPA page fallback']],d.site_routing),'Missing scripts, styles and media still return 404.')+
    settingsRow('external_resources','External resources',select('external_resources',[['allow','Allow HTTPS and APIs'],['self_only','Build resources only']],d.external_resources),'External fonts, CDNs and APIs may stop working in the local-only profile.');
  const maximumHint=max=>`<small>${escape(t('Maximum: {value}',{value:new Intl.NumberFormat(document.documentElement.lang,{maximumFractionDigits:2}).format(max)}))}</small>`;
  const limitControl=(name,key,value,max)=>settingsRow(name,key,number(name,value,{max})+maximumHint(max));
  const quotaMaximum=Math.min(c.content_quota_bytes,s.maximum_material_bytes??c.content_quota_bytes)/GiB;
  const operations=state.settings.operations??{};
  const uploadLimits=limitControl('file_mib','Single file limit, MiB',l.file_bytes/MiB,c.file_bytes/MiB)+
    limitControl('payload_mib','Upload request limit, MiB',l.upload_payload_bytes/MiB,c.upload_payload_bytes/MiB)+
    limitControl('site_mib','Expanded site, MiB',l.site_expanded_bytes/MiB,c.site_expanded_bytes/MiB)+
    limitControl('site_files','Files per site',l.site_files,c.site_files)+
    limitControl('concurrent_uploads','Concurrent uploads',l.concurrent_uploads,c.concurrent_uploads);
  const storageLimits=settingsRow('publication_count','Materials limit',number('publication_count',l.publication_count,{max:c.publication_count})+maximumHint(c.publication_count),'Retained records count even after their files are removed.')+
    settingsRow('quota_gib','Storage quota, GiB',number('quota_gib',l.content_quota_bytes/GiB,{min:0.01,max:quotaMaximum,step:'any'})+maximumHint(quotaMaximum))+
    `<p class="storage-stat">${label('Storage in use')}: ${bytes(s.used_bytes)} · ${label('Version history')}: ${bytes(s.history_bytes??0)} · ${label('Reserved')}: ${bytes(s.reserved_bytes)}</p><p><small>${label('Lowering a limit never deletes existing files. Deployment ceilings and free disk checks still apply.')}</small></p>`;
  const rateFields=[
    ['publication_open_attempts_per_minute','Link requests per IP per minute'],
    ['publication_password_attempts','Password attempts per IP and material'],
    ['publication_password_window_minutes','Password attempt window, minutes'],
  ];
  const rateBounds=operations.publication_rate_bounds;
  const protection=rateBounds?rateFields.map(([name,key])=>{
    const {minimum,maximum}=rateBounds[name];
    return settingsRow(name,key,number(name,d[name],{min:minimum,max:maximum})+maximumHint(maximum));
  }).join('')+`<p class="subtext">${label('Attempts are counted in fixed windows. At the limit, new requests wait until the window ends. Owner sign-in has separate protection.')}</p>`:'';
  const settingsSections=[
    ['publication-defaults','Publication defaults'],['link-names','Link names'],
    ['access-cleanup','Access and cleanup'],['owner-viewing','Owner viewing'],
    ['version-history','Version history'],['site-settings','Static site ZIP'],
    ['upload-limits','Upload limits'],['storage-limits','Limits and storage'],
    ...(rateBounds?[['request-protection','Request protection']]:[]),
  ];
  const sectionButtons=settingsSections.map(([id,key])=>`<button type="button" data-settings-section="${id}">${label(key)}</button>`).join('');
  const settingsNavigation=`<nav class="settings-navigation" aria-label="${label('Settings')}"><div class="settings-navigation-desktop">${sectionButtons}</div><details class="settings-navigation-mobile"><summary>${label('Sections')}</summary><div>${sectionButtons}</div></details></nav>`;
  const example=`<aside class="settings-summary" aria-labelledby="settings-example-title"><h3 id="settings-example-title">${label('Example publication')}</h3><p class="subtext">${label('After saving these defaults, this is what a new material will use unless its own settings are supplied.')}</p><div id="lifecycle" class="lifecycle"></div></aside>`;
  $('#view').innerHTML=`<form id="settings-form">${settingsNavigation}<div class="settings-layout"><div class="settings-sections">${settingsGroup('Publication defaults',publishing,'publication-defaults')}${settingsGroup('Link names',linkNames,'link-names')}${settingsGroup('Access and cleanup',policyFields(d,'settings-'),'access-cleanup')}${settingsGroup('Owner viewing',viewing,'owner-viewing')}${settingsGroup('Version history',historySettings,'version-history')}${settingsGroup('Static site ZIP',site,'site-settings')}${settingsGroup('Upload limits',`<p class="subtext">${label('These file limits apply to uploads from the cabinet, browser requests, CLI and API.')}</p>`+uploadLimits+(Number.isFinite(operations.mcp_inline_bytes)?`<p class="subtext">${escape(t('Text and HTML sent directly through MCP: {size} per request.',{size:bytes(operations.mcp_inline_bytes)}))}</p>`:''),'upload-limits')}${settingsGroup('Limits and storage',storageLimits,'storage-limits')}${rateBounds?settingsGroup('Request protection',protection,'request-protection'):''}</div>${example}</div><div class="setting-actions"><button type="submit" class="primary">${label('Save settings')}</button></div></form>`;
  const form=$('#settings-form');
  form.addEventListener('click',event=>{
    const id=event.target.closest('[data-settings-section]')?.dataset.settingsSection;
    if(!settingsSections.some(([section])=>section===id))return;
    const section=$('#'+id);
    const mobileNavigation=event.target.closest('.settings-navigation-mobile');
    if(mobileNavigation)mobileNavigation.open=false;
    section?.focus({preventScroll:true});
    section?.scrollIntoView({block:'start'});
  });
  const validateLinkNames=()=>{
    const input=form.elements;
    const minimum=Number(input.publication_key_min_characters.value),maximum=Number(input.publication_key_max_characters.value);
    input.publication_key_max_characters.setCustomValidity(minimum>maximum?t('Maximum length must be at least the minimum length.'):'');
  };
  const simulate=()=>{
    validateLinkNames();syncForm(form);const input=form.elements;
    const expiry=input.access.value==='private'?input.private_ttl_days:input.external_ttl_days;
    const end=expiry.value===''?null:new Date(Date.now()+Number(expiry.value)*DAY*1000);
    const after=input.purge_mode.value==='after';
    const purge=end&&input.purge_mode.value!=='never'?new Date(end.getTime()+(after?Number(input.purge_after.value)*(input.purge_unit.value==='hours'?3600:DAY)*1000:0)):null;
    const record=!purge?'While files are kept':{keep:'Keep until manually deleted',with_content:'Delete with files',after_purge:'Delete after a set period'}[input.metadata_policy.value];
    const recordText=purge&&input.metadata_policy.value==='after_purge'&&input.metadata_days.validity?.valid!==false
      ?t('{days} days after file deletion',{days:new Intl.NumberFormat(document.documentElement.lang).format(Number(input.metadata_days.value))})
      :t(record);
    const invalid=expiry.validity?.valid===false||(end&&after&&input.purge_after.validity?.valid===false);
    const step=(heading,content)=>`<div class="life-step"><strong>${label(heading)}</strong><small>${escape(content)}</small></div>`;
    $('#lifecycle').innerHTML=invalid?`<p class="subtext">${label('Enter valid durations to see the example.')}</p>`:[
      step('Publish',t(accessName(input.access.value))),
      step('Expiry',end?date(end):t('No expiry')),
      step('Delete files after access expires',purge?date(purge):t('Never automatically')),
      step('Library record after file deletion',recordText),
    ].join('<div class="life-line" aria-hidden="true"></div>');
  };
  form.addEventListener('input',simulate);form.addEventListener('change',simulate);simulate();
  form.addEventListener('submit',async event=>{
    event.preventDefault();validateLinkNames();if(!form.reportValidity())return;
    const submit=$('[type=submit]',form), restoreFocus=document.activeElement===submit;
    submit.disabled=true;
    try {
      const input=form.elements;
      const patch={defaults:{...Object.fromEntries(rateBounds?rateFields.map(([name])=>[name,positive(input[name].value)]):[]),history_previous_versions:nonNegativeInteger(input.history_previous_versions.value),history_retention_days:nonNegativeInteger(input.history_retention_days.value),history_budget_percent:nonNegativeInteger(input.history_budget_percent.value),publication_key_min_characters:positive(input.publication_key_min_characters.value),publication_key_max_characters:positive(input.publication_key_max_characters.value),access:input.access.value,private_ttl_seconds:input.private_ttl_days.value===''?null:Math.round(positive(input.private_ttl_days.value)*DAY),external_ttl_seconds:Math.round(positive(input.external_ttl_days.value)*DAY),...readPolicy(form),owner_auto_open:input.owner_auto_open.checked,preview:input.preview.value,guest_counts:input.guest_counts.checked,owner_counts:input.owner_counts.checked,indexing:input.indexing.checked,timezone:input.timezone.value,site_entry:input.site_entry.value,site_routing:input.site_routing.value,external_resources:input.external_resources.value},limits:{file_bytes:Math.round(positive(input.file_mib.value)*MiB),upload_payload_bytes:Math.round(positive(input.payload_mib.value)*MiB),site_expanded_bytes:Math.round(positive(input.site_mib.value)*MiB),site_files:positive(input.site_files.value),publication_count:positive(input.publication_count.value),content_quota_bytes:Math.round(positive(input.quota_gib.value)*GiB),concurrent_uploads:positive(input.concurrent_uploads.value)}};
      const saved=await api('/settings',{method:'PATCH',body:patch});
      if(state.session!==session||!state.session?.authenticated)return;
      state.settings=saved;capacity?.render(saved);notify('Saved.');
      if(state.page==='settings'){
        renderSettings();
        if(restoreFocus && document.activeElement===document.body)$('[type=submit]',$('#settings-form'))?.focus({preventScroll:true});
      }
    }catch(error){showError(error);}finally{submit.disabled=false;}
  });
}
async function renderConnection() {
  const session=state.session;
  const root=$('#view');
  await mountConnection({root,api,t,escape,date,bytes,onCreateToken:createTokenDialog,onError:showError,
    onOpenLimits:async()=>{
      await navigate('settings');
      if(state.page!=='settings'||state.session!==session)return;
      const section=$('#upload-limits');
      section?.focus({preventScroll:true});
      section?.scrollIntoView({block:'start'});
    },
    isCurrent:()=>state.page==='connection'&&state.session===session&&!!session?.authenticated});
  formChange();
}
function openAccountDialog() {
  const session=state.session;
  const identity=`<dl class="account-identity" tabindex="-1" autofocus><dt>${label('Username')}</dt><dd>${escape(session.username)}</dd></dl>`;
  dialog(t('Account'),identity,`<button id="sign-out" type="button" data-session-no-refresh>${label('Sign out')}</button>`);
  $('#sign-out').addEventListener('click',async event=>{
    const trigger=event.currentTarget;
    trigger.disabled=true;
    try {
      await api('/auth/logout',{method:'POST',body:{}});
      if(state.session!==session)return;
      clearOwnerState();setPath('/',true);$('#page-error').hidden=true;renderPublic();
    } catch(error) {
      if(state.session===session)showError(error);
    } finally {
      trigger.disabled=false;
    }
  });
}
function createTokenDialog() {
  const session=state.session;
  dialog(t('Create token'),`<form id="token-form">${field('token-name','Name','<input id="token-name" name="name" maxlength="120" required>')}${field('scope','Scope',select('scope',[['materials:manage','Manage materials'],['materials:read','Read materials']],'materials:manage'))}<button class="primary" type="submit">${label('Create token')}</button></form>`,button('close','Cancel'));
  const epoch=state.dialogEpoch;
  $('#token-form').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.currentTarget;if(!form.reportValidity())return;const submit=$('[type=submit]',form);submit.disabled=true;
    try {
      const result=await api('/tokens',{method:'POST',body:{name:form.elements.name.value,scope:form.elements.scope.value}});
      if(state.session!==session||!state.session?.authenticated||state.dialogEpoch!==epoch)return;
      if(typeof result.token!=='string')throw new Error(t('Could not complete the request.'));
      dialog(t('Token created — save it now'),`<p>${label('This secret is shown once. Store it in your tool’s credential configuration, never in a skill or publication.')}</p><p>${label('Paste this token only into your tool’s private connection prompt or credential settings, never into a chat.')}</p><textarea id="token-secret" class="secret" readonly autocomplete="off">${escape(result.token)}</textarea>`,`${button('copy-token','Copy token')}${button('close','Close')}`);
      await renderConnection();
    }catch(error){if(state.session===session&&state.session?.authenticated&&state.dialogEpoch===epoch)showError(error);}finally{submit.disabled=false;}
  });
}
async function confirmAction(title,key,action) {
  const session=state.session;
  dialog(t(title),`<p>${label(key)}</p>`,`${button('close','Cancel')}<button id="confirm-action" type="button" class="danger">${label(title)}</button>`);
  $('#confirm-action').addEventListener('click',async event=>{const trigger=event.currentTarget;trigger.disabled=true;try{await action();if(state.session!==session||$('#confirm-action')!==trigger)return;$('#dialog').close();notify('Saved.');if(state.page==='materials')await loadMaterials();else if(state.page==='connection')await renderConnection();}catch(error){if(state.session===session&&$('#confirm-action')===trigger)showError(error);trigger.disabled=false;}});
}
function extendDialog(material) {
  const session=state.session;
  dialog(t('Extend access'),`<form id="extend-form">${field('extend-days','Days',number('extend-days',7,{max:state.settings.ceilings.retention_days,step:'any'}))}<button type="submit" class="primary">${label('Extend access')}</button></form>`,button('close','Cancel'));
  $('#extend-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;if(!form.reportValidity())return;try{await api(`/publications/${encodeURIComponent(material.id)}/extend`,{method:'POST',body:{seconds:Math.round(positive(form.elements['extend-days'].value)*DAY)}});if(state.session!==session||$('#extend-form')!==form)return;$('#dialog').close();notify('Saved.');await loadMaterials();}catch(error){if(state.session===session&&$('#extend-form')===form)showError(error);}});
}
document.addEventListener('error',event=>{
  const image=event.target;
  if(image.matches?.('[data-thumbnail]')){
    const fallback=icon(image.dataset.thumbnail==='site'?'site':'file');
    image.replaceWith(document.createRange().createContextualFragment(fallback));
  }
},true);
document.addEventListener('pointerdown',event=>{const filter=$('#status-filter');if(filter?.open&&!filter.contains(event.target))filter.open=false;});
document.addEventListener('click',async event=>{
  const target=event.target.closest('button');if(!target||target.disabled)return;
  try {
    if(target.dataset.nav){setPath('/');await navigate(target.dataset.nav);return;}
    if(target.dataset.layout){state.layout=target.dataset.layout;renderRows();return;}
    if(Object.hasOwn(target.dataset,'kind')){state.kind=target.dataset.kind;state.offset=0;$$('[data-kind]').forEach(item=>item.setAttribute('aria-pressed',String(item.dataset.kind===state.kind)));await loadMaterials();return;}
    const action=target.dataset.action;if(!action)return;
    if(action==='close'){clearPublicationPasswordFields();state.dialogEpoch++;if(state.upload)await cancelUpload();else $('#dialog').close();return;}
    if(action==='commit-upload'){await commitPrepared();return;}
    if(action==='check-upload'){await checkPrepared();return;}
    if(action==='publish'){uploadDialog();return;}
    if(action==='clear-filters'){state.query='';state.kind='';state.states=[...defaultLibraryStates];state.offset=0;renderLibrary();await loadMaterials();return;}
    if(action==='previous'||action==='next'){
      const pager=$('#pagination'), restoreFocus=document.activeElement===target;
      state.offset=Math.max(0,state.offset+(action==='next'?state.limit:-state.limit));
      await loadMaterials();
      if(restoreFocus && pager.isConnected && !target.isConnected && document.activeElement===document.body){
        const current=$(`[data-action="${action}"]:not(:disabled)`,pager) || $('button:not(:disabled)',pager) || $('#search');
        current?.focus({preventScroll:true});
      }
      return;
    }
    if(action==='create-token'){createTokenDialog();return;}
    if(action==='copy-token'){await copyText($('#token-secret').value,target,'Copy token');return;}
    if(action==='revoke-connection'){await confirmAction('Revoke','Revoke this application connection?',()=>api(`/connections/${encodeURIComponent(target.dataset.id)}`,{method:'DELETE'}));return;}
    if(action==='revoke-token'){await confirmAction('Revoke','Revoke this device token?',()=>api(`/tokens/${encodeURIComponent(target.dataset.id)}`,{method:'DELETE'}));return;}
    const material=target.dataset.id?state.items.find(item=>item.id===target.dataset.id):$('#dialog').material;
    if(!material)return;
    if(action==='details'){await details(material.id);return;}
    if(action==='preview'){await preview(material.id);return;}
    if(action==='copy'){await copyMaterialLink(material.id,target);return;}
    if(action==='edit-link'){copyTextSequence++;publicationLink.open(material.id,target,updateMaterialLink);return;}
    if(action==='copy-password'){await copyMaterialPassword(material,target);return;}
    if(action==='hide-password'){
      const input=$('#saved-password-copy');
      if(input)input.value='';
      $('[data-password-fallback]').hidden=true;
      $('[data-action="copy-password"]')?.focus();
      return;
    }
    if(action==='replace'){uploadDialog(material);return;}
    if(action==='extend'){extendDialog(material);return;}
    if(action==='pause'){
      if(target.getAttribute('aria-disabled')==='true')return;
      const root=$('#dialog'), session=state.session;
      target.setAttribute('aria-disabled','true');
      try {
        const updated=await api(`/publications/${encodeURIComponent(material.id)}/pause`,{method:'POST',body:{paused:!material.paused}});
        if(state.session!==session)return;
        const status=materialStatus(updated);
        if(root.open && root.contains(target)){
          root.material=updated;
          target.textContent=t(updated.paused?'Resume access':'Pause access');
          $('[data-material-status]',root).textContent=t(status);
        }
        notify(status);
        if(state.page==='materials')await loadMaterials();
      }finally{target.removeAttribute('aria-disabled');}
      return;
    }
    if(action==='expire'){await confirmAction('Expire now','Access closed. The saved cleanup policy still applies.',()=>api(`/publications/${encodeURIComponent(material.id)}/expire`,{method:'POST',body:{}}));return;}
    if(action==='delete'){await confirmAction('Delete','Delete this material and its files? Access closes immediately. File cleanup may finish in the background.',async()=>{await api(`/publications/${encodeURIComponent(material.id)}`,{method:'DELETE'});capacity?.invalidate();});}
  }catch(error){showError(error);}
});
$('#dialog').addEventListener('cancel',event=>{clearPublicationPasswordFields();if(state.upload){event.preventDefault();cancelUpload();}});
$('#dialog').addEventListener('close',()=>{
  const root=$('#dialog');
  if(root.open)return;
  publicationLink.close();
  const restoreAccountFocus=!!state.session?.authenticated&&!!root.querySelector('.account-identity');
  state.dialogEpoch++;root.replaceChildren();delete root.material;delete root.dataset.materialId;
  if(restoreAccountFocus)$('#owner-button').focus({preventScroll:true});
});
$('#create-material').addEventListener('click',async()=>{try{await state.settingsRequest;if(state.session?.authenticated)uploadDialog();}catch(error){showError(error);}});
$('#owner-button').addEventListener('click',()=>{
  if(state.session?.authenticated){openAccountDialog();return;}
  setPath('/login');$('#page-error').hidden=true;renderLogin();
});
window.addEventListener('popstate',()=>{navigate(state.page).catch(showError);});
$('#theme-button').addEventListener('click',()=>{
  prefs.write('mp_theme',document.documentElement.dataset.theme==='dark'?'light':'dark');
  prefs.apply();refreshThemeControl();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(prefs.read('mp_theme','system')==='system')refreshThemeControl();});
$('#sidebar-toggle').addEventListener('click',()=>{prefs.write('mp_sidebar',prefs.read('mp_sidebar','expanded')==='expanded'?'collapsed':'expanded');prefs.apply();refreshChrome();});
$('#language-select').addEventListener('change',async event=>{
  prefs.write('mp_lang',event.target.value);prefs.apply();$('#dialog').close();$('#toast').hidden=true;
  document.dispatchEvent(new CustomEvent('publish:languagechange',{detail:{language:document.documentElement.lang}}));
  try {await navigate(state.page);}catch(error){showError(error);}
});
async function start() {
  try {
    [,state.session]=await Promise.all([loadMessages(),api('/session')]);$$('[data-icon]').forEach(element=>{element.innerHTML=icon(element.dataset.icon);});$('#instance-name').textContent=location.host;
    refreshChrome();mountTooltips();mountSessionActivity({getSession:()=>state.session,refresh:()=>api('/auth/refresh',{method:'POST',body:{}}),onError:showError});
    if(state.session.authenticated){
      const session=state.session;
      const settingsRequest=api('/settings').then(value=>{if(state.session===session){state.settings=value;capacity?.render(value);}});state.settingsRequest=settingsRequest;
      settingsRequest.finally(()=>{if(state.settingsRequest===settingsRequest)state.settingsRequest=null;}).catch(()=>{});
      if(state.page==='materials'&&!/^\/upload\//.test(location.pathname))await navigate(state.page,{settingsRequest});
      else{await settingsRequest;await navigate(state.page);}
    }else await navigate(state.page);
  }catch(error){showError(error);}
}
const capacityRoot = $('#capacity');
const capacity = capacityRoot && mountCapacity({
  root: capacityRoot,
  t,
  bytes,
  getSettings: () => state.session?.authenticated ? state.settings : null,
  refreshSettings: async (isCurrent) => {
    const session = state.session;
    const previous = state.settings;
    const settings = await api('/settings');
    if (isCurrent() && state.session === session && state.settings === previous) state.settings = settings;
  }
});

start();
