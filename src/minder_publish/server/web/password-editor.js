/* Draft passwords stay in the current owner form until it is explicitly submitted. */
import { copyOwnerSecret } from './secret-copy.js';

const mounted = new WeakSet();
const passwordAlphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

export function passwordEditorControl({name,required=false,help,t,escape,copyIcon=''}) {
  const label=key=>escape(t(key));
  const mark=copyIcon||'<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/></svg>';
  return `<div class="password-editor" data-password-editor><div class="password-input-row"><input id="${escape(name)}" name="password" type="password" autocomplete="new-password" maxlength="256" data-draft-password aria-describedby="${escape(name)}-help" ${required?'required':''}><button type="button" class="icon-button" data-copy-draft-password aria-label="${label('Copy entered password')}" data-tooltip="${label('Copy entered password')}" disabled>${mark}</button></div><div class="password-editor-actions"><button type="button" data-generate-password>${label('Generate strong password')}</button><small id="${escape(name)}-help">${label(help)}</small></div><small data-password-editor-status role="status"></small><div class="password-manual-copy" data-draft-password-fallback hidden><label for="${escape(name)}-copy">${label('Copy entered password manually')}</label><textarea id="${escape(name)}-copy" readonly autocomplete="off" spellcheck="false" class="secret" data-draft-password-copy></textarea><button type="button" data-hide-draft-password>${label('Hide password')}</button></div></div>`;
}

export function generatePublicationPassword(random = globalThis.crypto) {
  if (typeof random?.getRandomValues !== 'function') throw new Error('Secure random generation is unavailable.');
  const bytes = random.getRandomValues(new Uint8Array(24));
  // All 64 symbols have four equally likely byte values: 24 * 6 = 144 random bits.
  return Array.from(bytes, value => passwordAlphabet[value & 63]).join('');
}

export function clearPasswordEditors(root) {
  root.querySelectorAll('[data-draft-password], [data-draft-password-copy]').forEach(input => { input.value = ''; });
  root.querySelectorAll('[data-draft-password-fallback]').forEach(panel => { panel.hidden = true; });
  root.querySelectorAll('[data-copy-draft-password]').forEach(button => { button.disabled = true; });
}

export function mountPasswordEditors({root,getSession,getDialogEpoch,t,onCopied,onError}) {
  root.querySelectorAll('[data-password-editor]').forEach(editor => {
    if (mounted.has(editor)) return;
    mounted.add(editor);
    const input = editor.querySelector('[data-draft-password]');
    const copy = editor.querySelector('[data-copy-draft-password]');
    const generate = editor.querySelector('[data-generate-password]');
    const status = editor.querySelector('[data-password-editor-status]');
    const panel = editor.querySelector('[data-draft-password-fallback]');
    const manual = editor.querySelector('[data-draft-password-copy]');
    const session = getSession(), epoch = getDialogEpoch();
    const isCurrent = () => getSession() === session && !!session?.authenticated && getDialogEpoch() === epoch && root.open && root.contains(editor) && !input.disabled;
    const hideCopy = () => { manual.value = ''; panel.hidden = true; };
    const update = () => { hideCopy(); copy.disabled = !input.value; status.textContent = ''; };
    input.addEventListener('input', update);
    input.addEventListener('change', update);
    editor.querySelector('[data-hide-draft-password]').addEventListener('click', () => { hideCopy(); input.focus(); });
    generate.addEventListener('click', () => {
      if (!isCurrent()) return;
      try {
        input.value = generatePublicationPassword();
        input.dispatchEvent(new Event('input', {bubbles:true}));
        status.textContent = t('Strong password generated. Save or publish to apply it.');
      } catch {
        onError(new Error(t('Secure password generation is unavailable. Enter a password manually.')));
      }
    });
    copy.addEventListener('click', async () => {
      if (!isCurrent() || !input.value) return;
      let password = input.value;
      copy.disabled = true;
      try {
        await copyOwnerSecret({
          load:() => ({password}),
          isCurrent:() => isCurrent() && input.value === password,
          write:value => navigator.clipboard.writeText(value),
          onCopied,
          fallback:value => { panel.hidden = false; manual.value = value; manual.focus(); manual.select(); }
        });
      } catch(error) {
        if (isCurrent()) onError(error);
      } finally {
        if (isCurrent()) copy.disabled = !input.value;
        password = '';
      }
    });
    update();
  });
}
