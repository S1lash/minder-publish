import {previewModeOptions, previewPolicyLabel} from './preview-policy.js';

/* Explicit version links preserve material policy and never reserve historical bytes. */
export function mountVersionSharing({root,api,getMaterial,getSelected,getPreviewDefault,getSession,t,escape,onChanged,onPreviewChanged,onError,notify,showManualCopy}) {
  let modal=null,generation=0,busy=false;
  const label=key=>escape(t(key));
  const current=()=>root.isConnected&&!!getSession()?.authenticated;
  const sharingPath=(material,revision)=>`/publications/${encodeURIComponent(material.id)}/revisions/${encodeURIComponent(revision.revision_id)}/sharing`;
  function close(){
    generation++;
    if(modal){const previous=modal;modal=null;previous.close();previous.remove();}
  }
  function error(failure){
    if(failure.status===401){close();onError(failure);return;}
    const output=modal?.querySelector('[role=alert]')||root.querySelector('[data-version-error]');
    if(output){output.textContent=failure.message;output.hidden=false;}else onError(failure);
  }
  async function copy(url){
    if(!current())return;
    if(typeof url!=='string'||!url)throw new Error(t('Could not complete the request.'));
    const owner=getSession(),context=generation;
    const active=()=>current()&&getSession()===owner&&context===generation;
    try{await navigator.clipboard.writeText(url);if(active())notify('Version link copied.');}
    catch { if (active()) showManualCopy(url, t('Copy this link')); }
  }
  function confirm(enabled){
    if(busy||!current())return;close();
    const owner=getSession(),revision=getSelected(),material=getMaterial(),context=generation;
    const element=document.createElement('dialog');modal=element;element.className='dialog viewer-action-dialog';
    element.setAttribute('aria-labelledby','version-sharing-title');
    element.innerHTML=`<form><header class="dialog-head"><h2 id="version-sharing-title">${label(enabled?'Share this version':'Stop sharing this version')}</h2></header><div class="dialog-body"><p><strong>${escape(t('Version {version}',{version:revision.sequence}))}</strong></p><p>${label(enabled?'This link opens these exact files. The material’s current access, password and expiry still apply.':'The version link will stop opening for guests. The material link and saved files stay unchanged.')}</p>${enabled?`<p class="subtext">${label('Sharing does not reserve files. History cleanup can remove this version when its age, count or storage limit is reached.')}</p>${material.access==='private'?`<p class="callout">${label('This material is private. Its version link also requires owner sign-in.')}</p>`:''}`:''}<p class="callout error" role="alert" hidden></p></div><footer class="dialog-actions"><button type="button" data-sharing-cancel>${label('Cancel')}</button><button type="submit" class="${enabled?'primary':'danger'}">${label(enabled?'Share and copy link':'Stop sharing')}</button></footer></form>`;
    const active=()=>current()&&getSession()===owner&&generation===context&&modal===element&&element.open;
    element.querySelector('[data-sharing-cancel]').addEventListener('click',()=>{if(!busy)close();});
    element.addEventListener('cancel',event=>{event.preventDefault();if(!busy)close();});
    element.addEventListener('close',()=>{if(modal===element){modal=null;generation++;element.remove();}});
    element.querySelector('form').addEventListener('submit',async event=>{
      event.preventDefault();if(!active()||busy)return;
      const form=event.currentTarget,controls=[...form.elements];busy=true;form.setAttribute('aria-busy','true');controls.forEach(control=>{control.disabled=true;});
      let committed=false;
      try{
        const result=await api(sharingPath(material,revision),{method:'POST',body:{enabled}});committed=true;
        const visible=active();
        if(current()&&getSession()===owner){
          if(visible)close();
          await onChanged();
          if(visible&&current()&&getSession()===owner){if(enabled)await copy(result.version_url);else notify('Version sharing stopped.');}
        }
      }catch(failure){if(current()&&getSession()===owner){if(active()||failure.status===401)error(failure);else if(committed&&!modal)onError(failure);}}
      finally{busy=false;form.removeAttribute('aria-busy');if(element.isConnected)controls.forEach(control=>{control.disabled=false;});}
    });
    document.body.append(element);element.showModal();element.querySelector(enabled?'[type=submit]':'[data-sharing-cancel]').focus();
  }
  function render(){
    generation++;
    const revision=getSelected();
    if(!current()||!revision?.available){root.replaceChildren();root.hidden=true;return;}
    root.hidden=false;
    root.innerHTML=`<div class="viewer-section-head"><h3>${escape(t('Version {version}',{version:revision.sequence}))}</h3><span class="subtext">${label(revision.shared?'Version link enabled':'Version link off')}</span></div><div class="viewer-quick-actions">${revision.shared?`<button type="button" data-version-action="copy">${label('Copy version link')}</button><button type="button" data-version-action="stop">${label('Stop sharing')}</button>`:`<button type="button" data-version-action="share">${label('Share this version')}</button>`}</div>${revision.shared?`<div class="viewer-version-preview"><label for="viewer-version-preview-mode">${label(previewPolicyLabel)}</label><select id="viewer-version-preview-mode">${previewModeOptions(t,escape,revision.preview_mode,getPreviewDefault())}</select></div>`:''}<p class="subtext">${label('Uses the material’s access rules. History cleanup may remove the files; sharing does not keep them forever.')}</p><p class="callout error" data-version-error role="alert" tabindex="-1" hidden></p>`;
  }
  root.addEventListener('change',async event=>{
    const control=event.target.closest('#viewer-version-preview-mode');
    if(!control||!current()||busy)return;
    const revision=getSelected(),owner=getSession(),context=generation;
    if(!revision?.shared||control.value===revision.preview_mode)return;
    const actions=[...root.querySelectorAll('[data-version-action]')];
    const alert=root.querySelector('[data-version-error]');
    busy=true;
    control.disabled=true;
    actions.forEach(action=>{action.disabled=true;});
    alert.hidden=true;
    const active=()=>current()&&getSession()===owner&&generation===context&&control.isConnected;
    let failed=false;
    try{
      const result=await api(sharingPath(getMaterial(),revision),{method:'PATCH',body:{preview_mode:control.value}});
      if(current()&&getSession()===owner)onPreviewChanged(result);
      if(active())notify('Link preview setting saved.');
    }catch(failure){
      failed=true;
      if(active()||failure.status===401&&current()&&getSession()===owner)error(failure);
    }finally{
      busy=false;
      if(control.isConnected){
        control.innerHTML=previewModeOptions(t,escape,getSelected().preview_mode,getPreviewDefault());
        control.disabled=false;
        actions.forEach(action=>{action.disabled=false;});
      }
      queueMicrotask(()=>{
        if(active()&&(failed||document.activeElement===document.body))(failed?alert:control).focus();
      });
    }
  });
  root.addEventListener('click',async event=>{
    const button=event.target.closest('[data-version-action]');if(!button||!root.contains(button)||!current()||busy)return;
    try{if(button.dataset.versionAction==='copy')await copy(getSelected().version_url);else confirm(button.dataset.versionAction==='share');}
    catch(failure){error(failure);}
  });
  return {render,close};
}
