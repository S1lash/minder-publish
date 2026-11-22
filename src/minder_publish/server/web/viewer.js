import {loadMessages,t,translateStatic} from './i18n.js';
import {mountTooltips} from './tooltips.js';
import {mountSessionActivity} from './session.js';
import {confirmOwner,cancelOwnerConfirmation} from './reauth.js';
import {mountVersionSharing} from './viewer-sharing.js';
import {mountViewerActions} from './viewer-actions.js';
import {mountStatistics} from './statistics.js';
import {copyResolvedLink,mountManualCopy} from './link-copy.js';

import {linkEndpoint,mountPublicationLink} from './publication-link.js';

const $ = selector=>document.querySelector(selector);
const escape = value=>String(value??'').replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const prefs=window.PublishPreferences;
const identifier=document.body.dataset.publicationId||decodeURIComponent(location.pathname.split('/')[2]||'');
let initialRevision=document.body.dataset.revisionId||null;
const endpoint=`/publications/${encodeURIComponent(identifier)}`;
const drawer=$('#viewer-drawer');
const toast=$('#viewer-toast');
const manualCopy = $('#manual-copy-dialog');
const showManualCopy = mountManualCopy(manualCopy);
let toastTimer,restoreBusy=false,copyLinkAttempt=0;
let session,material,historyData,selected,settings,actions,sharing,statistics,epoch=0;
const date=value=>value?new Intl.DateTimeFormat(document.documentElement.lang,{dateStyle:'medium',timeStyle:'short',...(settings?.defaults?.timezone&&settings.defaults.timezone!=='browser'?{timeZone:settings.defaults.timezone}:{})}).format(new Date(value)):t('No expiry');
const bytes=value=>`${new Intl.NumberFormat(document.documentElement.lang,{maximumFractionDigits:1}).format(Number(value)/1024)} KiB`;
const current=()=>!!session?.authenticated;
async function api(path,{method='GET',body,reauth=true}={}) {
  const activeSession=session;
  const response=await fetch(`/api/v1${path}`,{method,credentials:'same-origin',headers:{Accept:'application/json',...(body!==undefined?{'Content-Type':'application/json'}:{}),...(method!=='GET'?{'X-CSRF-Token':session?.csrf_token||''}:{})},body:body===undefined?undefined:JSON.stringify(body)});
  const result=await response.json().catch(()=>({}));
  if(!response.ok){if(reauth&&response.status===403&&result.code==='reauthentication_required'&&activeSession?.authenticated&&session===activeSession){await confirmOwner({session:activeSession,getSession:()=>session,t,escape,request:password=>api('/auth/reauth',{method:'POST',body:{password},reauth:false})});if(session!==activeSession)throw new Error(t('Your session expired. Sign in again.'));return api(path,{method,body,reauth:false});}const error=new Error(response.status===401?t('Your session expired. Sign in again.'):result.detail||t('Could not complete the request.'));error.status=response.status;error.code=result.code;throw error;}
  return result.data;
}
function showError(error) {
  const output=$('#viewer-error');output.replaceChildren(document.createTextNode(error.message||t('Could not complete the request.')));output.hidden=false;
  if(error.status===401){publicationLink.close();sharing?.close();actions?.close();epoch++;session=null;cancelOwnerConfirmation();manualCopy.close();$('#viewer-confirm').close();$('#viewer-content').replaceChildren();drawer.close();$('#viewer-tools').disabled=true;const link=document.createElement('a');link.href='/login?return_to='+encodeURIComponent(location.pathname+location.search);link.textContent=t('Sign in');output.append(link);}
}
function clearPassword(){actions?.clearSecrets();}
function notify(key){clearTimeout(toastTimer);([...document.querySelectorAll('dialog[open]')].at(-1)||document.body).append(toast);toast.textContent=t(key);toast.hidden=false;toastTimer=setTimeout(()=>toast.hidden=true,4000);}
function closeDrawer(){copyLinkAttempt++;publicationLink.close();sharing?.close();actions?.close();manualCopy.close();drawer.close();$('#viewer-tools').setAttribute('aria-expanded','false');$('#viewer-tools').focus();}
function explicitRevision(){return new URLSearchParams(location.search).get('revision')||initialRevision;}
function originalURL(revision=selected) {
  if(revision&&(explicitRevision()||revision.revision_id!==material.current_revision))return `/api/v1${endpoint}/revisions/${encodeURIComponent(revision.revision_id)}/open${location.hash}`;
  const url=new URL(material.stable_url,location.origin);url.searchParams.set('preview','1');
  const incoming=new URLSearchParams(location.search);
  for(const [key,value] of incoming)if(!['revision','preview','guest'].includes(key))url.searchParams.append(key,value);
  url.hash=location.hash;
  return url.href;
}
function renderContent(){
  const root=$('#viewer-content');
  const browserType=/^(?:text\/(?:html|plain|css|javascript)|application\/(?:json|pdf)|image\/[^;]+|audio\/[^;]+|video\/[^;]+)(?:;|$)/i.test(selected?.content_type||'');
  if(!selected?.available||material.content_state!=='present'||selected?.kind==='file'&&(material.preview==='download'||!browserType)){
    root.replaceChildren();
    const fallback=document.createElement('section');fallback.className='viewer-file-fallback';fallback.innerHTML=`<h2>${escape(material.title||selected?.filename)}</h2><p>${escape(t(!selected?.available?'The files for this version are no longer stored.':'This file type does not have a browser preview.'))}</p>${selected?.available?`<a class="button" href="${escape(originalURL())}" target="_blank" rel="noopener noreferrer">${escape(t('Open original'))}</a>`:''}`;root.append(fallback);return;
  }
  const source=originalURL();
  const contentKey=JSON.stringify([source,selected.revision_id,material.access_version,material.access,material.paused,material.expires_at,material.content_state,material.preview,material.external_resources,material.site_routing,settings.defaults.owner_auto_open]);
  const existing=root.querySelector('iframe');
  if(existing?.dataset.contentKey===contentKey){existing.title=material.title||selected.filename||t('Preview');return;}
  root.replaceChildren();
  const frame=document.createElement('iframe');frame.dataset.contentKey=contentKey;frame.title=material.title||selected?.filename||t('Preview');frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox');frame.referrerPolicy='no-referrer';frame.src=source;root.append(frame);
}
function render(){
  translateStatic();document.title=`${material.title||material.filename} · minder publish.`;
  $('#viewer-title').textContent=material.title||material.filename;$('#viewer-drawer-title').textContent=material.title||material.filename;
  $('#viewer-open-tools').checked=prefs.read('mp_owner_view','tools')==='tools';
  $('#viewer-language').value=document.documentElement.lang;$('#viewer-theme').value=prefs.read('mp_theme','system');
  $('#viewer-language').setAttribute('aria-label',t('Language'));$('#viewer-theme').setAttribute('aria-label',t('Interface theme'));
  $('#viewer-version').textContent=selected?.sequence?`v${selected.sequence}`:'';
  const accessLabel=t({private:'Only me',link:'Anyone with the link · No password',password:'Password-protected'}[material.access]);
  const accessIcon=material.access==='password'?'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>':material.access==='private'?'<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6Z"/>':'<path d="m10 13 4-4M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>';
  $('#viewer-access').innerHTML=`<span class="viewer-access-badge access-${escape(material.access)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">${accessIcon}</svg>${escape(accessLabel)}</span>${material.paused?`<span class="pill warning">${escape(t('Paused'))}</span>`:''}${material.expires_at&&new Date(material.expires_at)<=new Date()?`<span class="pill warning">${escape(t('Expired'))}</span>`:''}`;
  $('#viewer-copy-address').textContent=material.stable_url;
  actions.render();
  $('#viewer-version-count').textContent=t('{available} available of {total}',{available:historyData.available,total:historyData.total});
  $('#viewer-versions').innerHTML=historyData.items.map(item=>{
    const title=`${item.sequence?`v${item.sequence}`:t('Imported version')} · ${date(item.created_at)}`;
    const details=`${bytes(item.size_bytes)}${item.retain_until?` · ${t('Stored until {date}',{date:date(item.retain_until)})}`:''}`;
    const content=`<span class="viewer-revision-line"><span>${escape(title)}</span><span class="viewer-revision-badges">${item.current?`<small>${escape(t('Current'))}</small>`:''}${item.shared?`<small>${escape(t('Shared version'))}</small>`:''}</span></span><small>${escape(item.available?details:t('Files removed by retention policy'))}</small>${item.change_summary?`<small>${escape(item.change_summary)}</small>`:''}`;
    return item.available?`<button type="button" data-revision="${escape(item.revision_id)}" aria-pressed="${selected?.revision_id===item.revision_id}">${content}</button>`:`<div class="viewer-revision-unavailable">${content}</div>`;
  }).join('');
  sharing.render();
  const historical=selected?.revision_id!==material.current_revision;
  const exactVersion=!!explicitRevision()||historical;
  $('#viewer-history-notice').hidden=!exactVersion;$('#viewer-restore').hidden=!historical||!selected?.available;
  $('#viewer-selection-notice').textContent=t('Viewing version {version}. The material link opens the current version.',{version:selected?.sequence||''});
  $('#viewer-clean').href=originalURL();
  const guestVersion=exactVersion&&selected?.shared;
  $('#viewer-guest').href=(guestVersion?selected.version_url:material.stable_url)+'?guest=1';
  const guestLabel=$('#viewer-guest [data-i18n]');
  guestLabel.dataset.i18n=exactVersion&&!guestVersion?'Check current material access':'Check guest access';
  guestLabel.textContent=t(guestLabel.dataset.i18n);
  $('#viewer-download').href=exactVersion?`/api/v1${endpoint}/revisions/${encodeURIComponent(selected.revision_id)}/download`:`/api/v1${endpoint}/download`;
  $('#viewer-download').textContent=historical?t('Download version {version}',{version:selected.sequence||''}):t(selected?.kind==='site'?'Download site ZIP':'Download original');
  $('#viewer-download').hidden=!selected?.available;
  $('#viewer-library').href='/?material='+encodeURIComponent(identifier);
  document.dispatchEvent(new Event('publish:formchange'));
}
function setRevisionURL(id){
  const url=new URL(location.href);url.pathname=new URL(material.stable_url,location.origin).pathname;
  if(!id)url.searchParams.delete('revision');else url.searchParams.set('revision',id);
  initialRevision=null;history.replaceState(null,'',url);
}
function selectRevision(id,{latest=false}={}){
  const revision=historyData.items.find(item=>item.revision_id===id&&item.available);if(!revision)return;
  selected=revision;setRevisionURL(latest?undefined:id);
  render();renderContent();closeDrawer();
}
async function refresh({loadSettings=false}={}){
  const request=++epoch;
  const values=await Promise.all([api(endpoint),api(endpoint+'/revisions'),...(loadSettings?[api('/settings')]:[])]);if(request!==epoch||!current())return;
  [material,historyData]=values;if(loadSettings)settings=values[2];
  const requested=new URLSearchParams(location.search).get('revision')||initialRevision;
  selected=requested?historyData.items.find(item=>item.revision_id===requested):historyData.items.find(item=>item.revision_id===material.current_revision)||historyData.items[0];
  if(!selected)throw new Error(t('The files for this version are no longer stored.'));
  render();renderContent();$('#viewer-tools').disabled=false;
}
$('#viewer-tools').addEventListener('click',()=>{actions.clearSecrets();drawer.showModal();$('#viewer-tools').setAttribute('aria-expanded','true');statistics?.refresh();});
$('#viewer-close').addEventListener('click',closeDrawer);
drawer.addEventListener('click',event=>{if(event.target===drawer){const rect=drawer.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeDrawer();}});
drawer.addEventListener('cancel',event=>{event.preventDefault();closeDrawer();});
drawer.addEventListener('close',()=>{if(drawer.open)return;copyLinkAttempt++;publicationLink.close();sharing.close();actions.close();manualCopy.close();$('#viewer-tools').setAttribute('aria-expanded','false');});
$('#viewer-versions').addEventListener('click',event=>{const button=event.target.closest('[data-revision]');if(button)selectRevision(button.dataset.revision);});
$('#viewer-current').addEventListener('click',()=>selectRevision(material.current_revision,{latest:true}));
function replaceMaterialLinkURL(stableURL, revision, currentRevision) {
  const url = new URL(location.href);
  url.pathname = new URL(stableURL, location.origin).pathname;
  if (revision && (explicitRevision() || revision !== currentRevision)) url.searchParams.set('revision', revision);
  history.replaceState(null, '', url);
  initialRevision = null;
}
function updateMaterialLink(snapshot) {
  if (snapshot.id !== identifier || snapshot.stable_url === material.stable_url) return;
  material.stable_url = snapshot.stable_url;
  material.link_name = snapshot.link_name;
  replaceMaterialLinkURL(snapshot.stable_url, selected?.revision_id, material.current_revision);
  $('#viewer-copy-address').textContent = snapshot.stable_url;
  $('#viewer-clean').href = originalURL();
  const exactVersion = !!explicitRevision() || selected?.revision_id !== material.current_revision;
  if (!exactVersion || !selected?.shared) $('#viewer-guest').href = snapshot.stable_url + '?guest=1';
}
const publicationLink = mountPublicationLink({
  api, getSession: () => session, t, escape, onError: showError,
  errorMessage: error => error instanceof TypeError ? t('The server is unavailable. Check your connection and try again.') : error.message,
  showManualCopy,
});
$('#viewer-edit-link').addEventListener('click', event => {
  if (!current() || !drawer.open) return;
  copyLinkAttempt++;
  publicationLink.open(identifier, event.currentTarget, updateMaterialLink);
});
$('#viewer-copy').addEventListener('click', async () => {
  if (!current() || !drawer.open) return;
  const owner = session, attempt = ++copyLinkAttempt;
  const active = () => session === owner && drawer.open && attempt === copyLinkAttempt;
  try {
    await copyResolvedLink({
      isCurrent: active,
      load: async () => {
        const value = await api(linkEndpoint(identifier));
        if (active()) updateMaterialLink(value);
        return value.stable_url;
      },
      onCopied: () => notify('Material link copied.'),
      fallback: url => showManualCopy(url, t('Copy this link')),
    });
  } catch (error) {
    if (active()) showError(error);
  }
});
$('#viewer-restore').addEventListener('click',()=>{if(restoreBusy||!current())return;$('#viewer-confirm-error').hidden=true;$('#viewer-confirm').showModal();});
$('#viewer-restore-cancel').addEventListener('click',()=>{if(!restoreBusy)$('#viewer-confirm').close();});
$('#viewer-confirm').addEventListener('cancel',event=>{if(restoreBusy)event.preventDefault();});
$('#viewer-restore-confirm').addEventListener('click',async event=>{
  const dialog=$('#viewer-confirm');if(restoreBusy||!current()||!dialog.open)return;
  const owner=session,revision=selected.revision_id,expectedRevision=material.current_revision;
  const controls=[event.currentTarget,$('#viewer-restore-cancel')];
  restoreBusy=true;dialog.setAttribute('aria-busy','true');controls.forEach(control=>control.disabled=true);
  $('#viewer-confirm-error').hidden=true;
  let committed=false;
  try{
    await api(`${endpoint}/revisions/${encodeURIComponent(revision)}/restore`,{method:'POST',body:{expected_revision:expectedRevision}});committed=true;
    if(!current()||session!==owner)return;
    dialog.close();setRevisionURL();await refresh();
  }catch(error){
    if(current()&&session===owner){
      if(error.status===401||committed)showError(error);
      else if(dialog.open){$('#viewer-confirm-error').textContent=error.status===409?t('The material changed. Reload it before restoring a version.'):error.message;$('#viewer-confirm-error').hidden=false;}
    }
  }finally{restoreBusy=false;dialog.removeAttribute('aria-busy');controls.forEach(control=>control.disabled=false);}
});
$('#viewer-open-tools').addEventListener('change',event=>prefs.write('mp_owner_view',event.target.checked?'tools':'clean'));
$('#viewer-theme').addEventListener('change',event=>{prefs.write('mp_theme',event.target.value);prefs.apply();});
$('#viewer-language').addEventListener('change',async event=>{publicationLink.close();clearPassword();sharing.close();actions.close();prefs.write('mp_lang',event.target.value);prefs.apply();render();renderContent();statistics?.render();document.dispatchEvent(new Event('publish:languagechange'));});
async function start(){
  try{[,session]=await Promise.all([loadMessages(),api('/session')]);translateStatic();mountTooltips();if(!session?.authenticated){const error=new Error(t('Sign in to manage this material.'));error.status=401;throw error;}const owner=session;statistics=await mountStatistics({root:$('#viewer-statistics'),id:identifier,api,t,escape,date,isCurrent:()=>current()&&session===owner,onError:showError,lazy:true});mountSessionActivity({getSession:()=>session,refresh:()=>api('/auth/refresh',{method:'POST',body:{}}),onError:showError});await refresh({loadSettings:true});}
  catch(error){showError(error);}
}
actions=mountViewerActions({root:$('#viewer-management'),drawer,api,getMaterial:()=>material,getSettings:()=>settings,getSession:()=>session,t,escape,date,onChanged:async()=>{try{await refresh();}catch(error){if([404,410].includes(error.status)){$('#viewer-content').replaceChildren();location.assign('/');return;}throw error;}},onDeleted:()=>{actions.close();$('#viewer-content').replaceChildren();location.assign('/');},onError:showError,notify});
sharing=mountVersionSharing({root:$('#viewer-version-sharing'),api,getMaterial:()=>material,getSelected:()=>selected,getSession:()=>session,t,escape,onChanged:refresh,onError:showError,notify,showManualCopy});
start();
