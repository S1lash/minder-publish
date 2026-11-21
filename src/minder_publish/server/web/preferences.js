/* Non-authentication preferences, shared by the cabinet and system viewer chrome. */
(() => {
  const choices = { mp_lang:['en','ru','es','fr'], mp_theme:['system','light','dark'], mp_sidebar:['expanded','collapsed'], mp_owner_view:['tools','clean'] };
  function read(name, fallback) {
    if (!choices[name]) return fallback;
    const cookie = document.cookie.split(';').map(value=>value.trim()).find(value=>value.startsWith(`${name}=`));
    try { const value = cookie ? decodeURIComponent(cookie.slice(name.length+1)) : fallback; return choices[name].includes(value) ? value : fallback; }
    catch { return fallback; }
  }
  function write(name,value) {
    if (!choices[name]?.includes(value)) throw new Error('Invalid browser preference');
    document.cookie = `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;
  }
  function apply() {
    document.documentElement.lang=read('mp_lang','en');
    const theme=read('mp_theme','system');
    document.documentElement.dataset.theme=theme==='system'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):theme;
    document.documentElement.dataset.sidebar=read('mp_sidebar','expanded');
  }
  window.PublishPreferences={read,write,apply};
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{const theme=read('mp_theme','system');document.documentElement.dataset.theme=theme==='system'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):theme;});
  apply();
})();
