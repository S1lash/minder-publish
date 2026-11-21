/* Consent preferences use the same non-authentication cookies as the cabinet. */
const controls=document.querySelector('.oauth-preferences');
if(controls && window.PublishPreferences){
  controls.hidden=false;
  controls.addEventListener('click',event=>{
    const button=event.target.closest('button[data-oauth-preference]');
    if(!button)return;
    const name=button.dataset.oauthPreference;
    if(!['mp_lang','mp_theme'].includes(name))return;
    window.PublishPreferences.write(name,button.value);
    location.reload();
  });
  const menus=[...controls.querySelectorAll('details')];
  for(const menu of menus)menu.addEventListener('toggle',()=>{
    if(menu.open)for(const other of menus)if(other!==menu)other.open=false;
  });
  document.addEventListener('click',event=>{
    if(!controls.contains(event.target))for(const menu of menus)menu.open=false;
  });
  document.addEventListener('keydown',event=>{
    if(event.key!=='Escape')return;
    for(const menu of menus)if(menu.open){menu.open=false;menu.querySelector('summary').focus();}
  });
}
