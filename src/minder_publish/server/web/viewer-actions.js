/* Owner quick actions use the same policy endpoints and private password editor as the library. */
import {copyOwnerSecret} from './secret-copy.js';
import {clearPasswordEditors,mountPasswordEditors,passwordEditorControl} from './password-editor.js';

const DAY=86400;
export function mountViewerActions({root,drawer,api,getMaterial,getSettings,getSession,t,escape,date,onChanged,onDeleted,onError,notify}) {
  let generation=0,modal=null,busy=false;
  const label=key=>escape(t(key));
  const endpoint=()=>`/publications/${encodeURIComponent(getMaterial().id)}`;
  const current=()=>!!getSession()?.authenticated&&root.isConnected;
  const name=()=>getMaterial().title?.trim()||getMaterial().filename?.trim()||getMaterial().id;
  function clearSecrets(){
    generation++;
    root.querySelectorAll('[data-saved-password]').forEach(input=>{input.value='';});
    root.querySelectorAll('[data-saved-password-fallback]').forEach(panel=>{panel.hidden=true;});
    const button=root.querySelector('[data-owner-action="copy-password"]');
    if(button)button.disabled=!getMaterial()?.password_copy_available||busy;
  }
  function close(){
    clearSecrets();
    if(!modal)return;
    const previous=modal;modal=null;
    clearPasswordEditors(previous);previous.close();previous.remove();
  }
  function error(errorValue){
    if(errorValue.status===401){close();onError(errorValue);return;}
    const output=modal?.querySelector('[data-action-error]')||root.querySelector('[data-quick-error]');
    if(output){output.textContent=errorValue.message||t('Could not complete the request.');output.hidden=false;}
    else onError(errorValue);
  }
  function dialog(title,body,submit,perform,{danger=false}={}){
    close();
    const owner=getSession(),context=++generation;
    const element=document.createElement('dialog');modal=element;
    element.className='dialog viewer-action-dialog';element.setAttribute('aria-labelledby','viewer-action-title');
    element.innerHTML=`<form><header class="dialog-head"><div><h2 id="viewer-action-title">${label(title)}</h2><p class="subtext">${escape(name())}</p></div></header><div class="dialog-body">${body}<p data-action-error class="callout error" role="alert" hidden></p></div><footer class="dialog-actions"><button type="button" data-action-cancel>${label('Cancel')}</button><button type="submit" class="${danger?'danger':'primary'}">${label(submit)}</button></footer></form>`;
    const active=()=>current()&&getSession()===owner&&generation===context&&modal===element&&element.open;
    element.querySelector('[data-action-cancel]').addEventListener('click',()=>{if(!busy)close();});
    element.addEventListener('cancel',event=>{event.preventDefault();if(!busy)close();});
    element.addEventListener('close',()=>{clearPasswordEditors(element);if(modal===element){modal=null;generation++;element.remove();}});
    element.querySelector('form').addEventListener('submit',async event=>{
      event.preventDefault();const form=event.currentTarget;if(!active()||busy||!form.reportValidity())return;
      const controls=[...form.elements].map(control=>[control,control.disabled]);
      busy=true;form.setAttribute('aria-busy','true');
      controls.forEach(([control])=>{control.disabled=true;});
      element.querySelector('[data-action-error]').hidden=true;
      let committed=false;
      try{
        const result=await perform(form);committed=true;
        if(result?.removed){if(current()&&getSession()===owner){close();onDeleted();}return;}
        const visible=active();
        if(current()&&getSession()===owner){if(visible)close();await onChanged();if(visible)notify('Saved.');}
      }catch(failure){
        if(current()&&getSession()===owner){if(active()||failure.status===401)error(failure);else if(committed&&!modal)onError(failure);}
      }finally{
        busy=false;form.removeAttribute('aria-busy');
        if(element.isConnected)controls.forEach(([control,disabled])=>{control.disabled=disabled;});
        if(!modal&&current())render();
      }
    });
    document.body.append(element);element.showModal();
    mountPasswordEditors({root:element,getSession,getDialogEpoch:()=>generation,t,onCopied:()=>notify('Entered password copied.'),onError:error});
    element.querySelector('input,select,[type=submit]')?.focus();
  }
  function expiryChoice({privateSharing=false}={}){
    const maximum=getSettings().ceilings.retention_days;
    const days=Math.min(7,maximum);
    return `${privateSharing?`<p class="callout">${label('A password lets recipients open this private material. Choose how long the shared link will remain available.')}</p>`:''}<label class="form-field"><span>${label('Expiry')}</span><select name="expiry" id="viewer-action-expiry" aria-label="${label('Expiry')}"><option value="default">${label('Use default')}</option><option value="days">${label('After a number of days')}</option><option value="never">${label('No expiry')}</option></select></label><label class="form-field" data-expiry-days hidden><span>${label('Days')}</span><input type="number" name="days" min="1" max="${maximum}" value="${days}" step="1" required disabled></label>`;
  }
  function wireExpiry(){
    const select=modal?.querySelector('[name=expiry]');if(!select)return;
    select.addEventListener('change',()=>{const panel=modal.querySelector('[data-expiry-days]');panel.hidden=select.value!=='days';panel.querySelector('input').disabled=panel.hidden;});
  }
  function readExpiry(form){
    const mode=form.elements.expiry.value;
    if(mode==='days')return {mode:'at',value:new Date(Date.now()+Number(form.elements.days.value)*DAY*1000).toISOString()};
    return {mode};
  }
  function passwordDialog(){
    const material=getMaterial();const sharing=material.access==='private';
    dialog(material.access==='password'?'Change publication password':'Set publication password',
      `<div class="form-field"><label for="viewer-new-password">${label('New publication password')}</label>${passwordEditorControl({name:'viewer-new-password',required:true,help:'Generate a password or enter your own. Save to apply it.',t,escape})}</div>${sharing?expiryChoice({privateSharing:true}):`<p class="subtext">${label('The current expiry and cleanup rules stay unchanged.')}</p>`}`,
      'Save password',form=>api(endpoint(),{method:'PATCH',body:{access:'password',password:form.elements.password.value,...(sharing?{expiry:readExpiry(form)}:{})}}));
    wireExpiry();
  }
  function removePassword(){
    dialog('Remove publication password',`<p>${label('Choose who may open the material after removing its password. The current expiry and cleanup rules stay unchanged.')}</p><label class="checkbox-label"><input type="radio" name="access" value="link" required><span>${label('Anyone with the link')}</span></label><label class="checkbox-label"><input type="radio" name="access" value="private" required><span>${label('Only me')}</span></label>`,
      'Remove password',form=>api(endpoint(),{method:'PATCH',body:{access:form.elements.access.value}}),{danger:true});
  }
  async function copyPassword(button){
    const owner=getSession(),context=generation;
    const active=()=>current()&&getSession()===owner&&generation===context&&drawer.open;
    button.disabled=true;
    try{
      await copyOwnerSecret({isCurrent:active,load:()=>api(endpoint()+'/password-copy',{method:'POST'}),write:value=>navigator.clipboard.writeText(value),onCopied:()=>notify('Password copied.'),fallback:value=>{
        const panel=root.querySelector('[data-saved-password-fallback]');panel.hidden=false;
        const input=panel.querySelector('textarea');input.value=value;input.focus();input.select();
      }});
    }catch(failure){if(active())error(failure);}finally{if(active())button.disabled=false;}
  }
  async function quickMutation(button,perform){
    if(busy)return;const owner=getSession();busy=true;clearSecrets();root.querySelectorAll('button').forEach(control=>{control.disabled=true;});
    root.querySelector('[data-quick-error]').hidden=true;
    try{await perform();if(current()&&getSession()===owner){await onChanged();notify('Saved.');}}
    catch(failure){if(current()&&getSession()===owner)error(failure);}
    finally{busy=false;if(current())render();}
  }
  function render(){
    if(!current()||!getMaterial())return;
    const errorOutput=root.querySelector('[data-quick-error]');
    const previousError=errorOutput&&!errorOutput.hidden?errorOutput.textContent:'';
    clearSecrets();
    const material=getMaterial(),present=material.content_state==='present',protectedLink=material.access==='password';
    const maximum=getSettings().ceilings.retention_days*DAY*1000;
    const expiry=material.expires_at?new Date(material.expires_at).getTime():null;
    const now=Date.now();
    const presets=[1,7,30].filter(days=>days*DAY*1000+Math.max(now,expiry||now)-now<=maximum);
    const signedDays = new Intl.NumberFormat(document.documentElement.lang || 'en', {
      style: 'unit',
      unit: 'day',
      unitDisplay: 'long',
      signDisplay: 'always',
    });
    const presetButtons = presets.map(days =>
      `<button type="button" data-owner-action="extend" data-days="${days}" ${busy?'disabled':''}>${escape(signedDays.format(days))}</button>`
    ).join('');
    root.innerHTML=`<section class="viewer-section"><h2>${label('Password')}</h2><div class="viewer-quick-actions"><button type="button" data-owner-action="password" ${busy?'disabled':''}>${label(protectedLink?'Change password':'Set password')}</button>${protectedLink?`<button type="button" data-owner-action="copy-password" ${material.password_copy_available&&!busy?'':'disabled'}>${label('Copy saved password')}</button><button type="button" data-owner-action="remove-password" ${busy?'disabled':''}>${label('Remove password')}</button>`:''}</div>${protectedLink&&!material.password_copy_available?`<p class="subtext">${label('This password has no saved copy. Set and save a new password to enable copying.')}</p>`:''}<div data-saved-password-fallback hidden><label for="viewer-saved-password">${label('Copy this password')}</label><textarea id="viewer-saved-password" data-saved-password readonly autocomplete="off" spellcheck="false" class="secret"></textarea><button type="button" data-owner-action="hide-password">${label('Hide password')}</button></div></section><section class="viewer-section"><h2>${label('Expiry')}</h2><p class="viewer-expiry-value">${escape(expiry?date(material.expires_at):t('No expiry'))}</p>${present?`${expiry?`<div class="viewer-expiry-presets">${presetButtons}</div>`:''}<div class="viewer-quick-actions"><button type="button" data-owner-action="expiry" ${busy?'disabled':''}>${label(expiry?'Change expiry':'Set expiry')}</button></div>`:`<p class="subtext">${label('The file is no longer stored. Upload a new revision to restore content.')}</p>`}${present?`<p class="subtext">${label(expiry?'Time is added from now or the current expiry, whichever is later. Access and cleanup rules stay unchanged.':'Access and cleanup rules stay unchanged.')}${material.paused?' '+label('A paused material stays paused.'):''}</p>`:''}</section><section class="viewer-section"><details class="viewer-danger-actions"><summary>${label('Manage access and deletion')}</summary><p class="subtext">${label('Pausing is reversible. Expiring closes access and follows the saved cleanup policy. Deleting removes the material and its files.')}</p><div class="viewer-quick-actions">${present?`<button type="button" data-owner-action="pause" ${busy?'disabled':''}>${label(material.paused?'Resume access':'Pause access')}</button><button type="button" data-owner-action="expire" ${busy?'disabled':''}>${label('Expire now')}</button>`:''}<button type="button" class="danger" data-owner-action="delete" ${busy?'disabled':''}>${label('Delete material')}</button></div></details></section><p data-quick-error class="callout error" role="alert" ${previousError?'':'hidden'}>${escape(previousError)}</p>`;
    document.dispatchEvent(new Event('publish:formchange'));
  }
  root.addEventListener('click',async event=>{
    const button=event.target.closest('[data-owner-action]');if(!button||!root.contains(button)||button.disabled||!current()||busy)return;
    switch(button.dataset.ownerAction){
      case 'password':passwordDialog();break;
      case 'remove-password':removePassword();break;
      case 'copy-password':await copyPassword(button);break;
      case 'hide-password':clearSecrets();break;
      case 'expiry':dialog('Change expiry',expiryChoice(),'Save',form=>api(endpoint(),{method:'PATCH',body:{expiry:readExpiry(form)}}));wireExpiry();break;
      case 'extend':await quickMutation(button,()=>api(endpoint()+'/extend',{method:'POST',body:{seconds:Number(button.dataset.days)*DAY}}));break;
      case 'pause':await quickMutation(button,()=>api(endpoint()+'/pause',{method:'POST',body:{paused:!getMaterial().paused}}));break;
      case 'expire':dialog('Expire now',`<p>${label('Access closes now. The saved cleanup policy may delete the files, including saved versions.')}</p>`,'Expire now',()=>api(endpoint()+'/expire',{method:'POST',body:{}}),{danger:true});break;
      case 'delete':dialog('Delete material',`<p>${label('Delete this material and all its saved versions? Access closes immediately. File cleanup may finish in the background.')}</p>`,'Delete material',async()=>{await api(endpoint(),{method:'DELETE'});return {removed:true};},{danger:true});break;
    }
  });
  return {render,close,clearSecrets};
}
