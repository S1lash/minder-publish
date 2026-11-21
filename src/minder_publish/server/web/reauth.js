/* A separate modal preserves the form that requested a fresh owner confirmation. */
let pending;
export function confirmOwner({session,getSession,t,escape,request}) {
  if(pending?.session===session)return pending.promise;
  pending?.cancel();
  let cancel;
  const promise=new Promise((resolve,reject)=>{
    const modal=document.createElement('dialog');modal.className='dialog owner-confirmation';
    modal.setAttribute('aria-labelledby','owner-confirm-title');
    modal.innerHTML=`<header class="dialog-head"><h2 id="owner-confirm-title">${escape(t('Confirm owner password'))}</h2></header><form id="owner-confirm-form" class="dialog-body" method="post" action="/api/v1/auth/reauth"><p>${escape(t('Confirm your owner password to continue. Your other work stays open.'))}</p><label for="owner-confirm-username">${escape(t('Username'))}</label><input id="owner-confirm-username" name="username" type="text" autocomplete="username" value="${escape(session.username)}" readonly><label for="owner-confirm-password">${escape(t('Password'))}</label><input id="owner-confirm-password" name="password" type="password" autocomplete="current-password" required><p class="callout error" role="alert" hidden></p><div class="owner-confirm-actions"><button type="button" data-cancel>${escape(t('Cancel'))}</button><button type="submit" class="primary">${escape(t('Continue'))}</button></div></form>`;
    const form=modal.querySelector('form'),input=form.elements.password,output=modal.querySelector('[role=alert]');
    let finished=false;
    const finish=(error,value)=>{if(finished)return;finished=true;input.value='';modal.close();modal.remove();if(pending?.promise===promise)pending=null;error?reject(error):resolve(value);};
    cancel=()=>finish(new Error(t('Confirmation cancelled.')));
    modal.querySelector('[data-cancel]').addEventListener('click',cancel);
    modal.addEventListener('cancel',event=>{event.preventDefault();cancel();});
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(!form.reportValidity())return;
      const submit=form.querySelector('[type=submit]');submit.disabled=true;output.hidden=true;
      try {
        const value=await request(input.value);input.value='';
        if(finished)return;
        if(getSession()!==session){cancel();return;}
        Object.assign(session,value);finish(null,value);
      }catch(error){
        input.value='';if(finished)return;
        if(getSession()!==session){cancel();return;}
        output.textContent=t(error.status===401?'Invalid username or password.':'Could not complete the request.');output.hidden=false;submit.disabled=false;input.focus();
      }
    });
    document.body.append(modal);modal.showModal();input.focus();
  });
  pending={session,promise,cancel};return promise;
}
export function cancelOwnerConfirmation() {pending?.cancel();}
