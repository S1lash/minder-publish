import {copyResolvedLink} from './link-copy.js';
import {previewModeOptions, previewPolicyLabel} from './preview-policy.js';

export const linkEndpoint = id => `/publications/${encodeURIComponent(id)}/link`;

export function materialLink(url, name) {
  const target = new URL(url, location.href);
  if (!['http:', 'https:'].includes(target.protocol)) throw new TypeError('Invalid link');
  if (name !== undefined) target.pathname = `/p/${encodeURIComponent(name)}`;
  return target.href;
}

export function mountPublicationLink({api, getSession, t, escape, onError, errorMessage, showManualCopy}) {
  let modal;
  const label = key => escape(t(key));

  function close() {
    if (!modal) return;
    const dialog = modal;
    modal = null;
    dialog.close();
    dialog.remove();
  }

  function open(id, source, onUpdate) {
    close();
    const session = getSession();
    const parent = source.closest('dialog');
    const dialog = document.createElement('dialog');
    modal = dialog;
    dialog.className = 'dialog publication-link-dialog';
    dialog.setAttribute('aria-labelledby', 'publication-link-title');
    dialog.innerHTML = `
      <form novalidate>
        <header class="dialog-head"><h2 id="publication-link-title">${label('Edit link')}</h2></header>
        <div class="dialog-body">
          <p data-link-loading role="status">${label('Loading…')}</p>
          <div class="publication-link-fields" hidden>
            <div class="form-field"><span class="subtext">${label('Current address')}</span><strong data-link-address></strong></div>
            <div class="form-field">
              <label for="publication-link-name">${label('Link name')}</label>
              <div class="publication-link-input"><span data-link-prefix></span><input id="publication-link-name" name="name" type="text" maxlength="128" required autocomplete="off" autocapitalize="off" spellcheck="false" aria-describedby="publication-link-help" aria-errormessage="publication-link-error"></div>
              <small id="publication-link-help">${label('Use 2–64 lowercase letters, digits or dashes. Start with a letter or digit.')}</small>
            </div>
            <div class="form-field"><span class="subtext">${label('New address')}</span><output for="publication-link-name" data-link-preview></output></div>
            <div>
              <label class="checkbox-label"><input type="checkbox" name="keep_previous" aria-describedby="publication-link-effect"><span>${label('Keep previous address active')}</span></label>
              <p id="publication-link-effect" class="subtext" aria-live="polite"></p>
            </div>
            <details class="publication-preview-settings">
              <summary>${label(previewPolicyLabel)}</summary>
              <div class="publication-preview-fields">
                <label data-preview-mode-label for="publication-preview-mode"></label>
                <select id="publication-preview-mode" name="preview_mode"></select>
                <div data-preview-aliases></div>
              </div>
            </details>
            <section data-link-history hidden aria-labelledby="publication-link-history-title">
              <h3 id="publication-link-history-title">${label('Previous links')}</h3>
              <ul class="publication-link-history"></ul>
            </section>
          </div>
          <p data-link-status role="status" class="subtext" hidden></p>
          <p id="publication-link-error" data-link-error role="alert" tabindex="-1" class="callout error" hidden></p>
        </div>
        <footer class="dialog-actions">
          <button type="button" data-link-close autofocus>${label('Close')}</button>
          <button type="button" data-link-copy hidden>${label('Copy new link')}</button>
          <button type="submit" class="primary" disabled>${label('Save')}</button>
        </footer>
      </form>`;

    const element = selector => dialog.querySelector(selector);
    const form = element('form');
    const input = form.elements.name;
    const keep = form.elements.keep_previous;
    const previewSettings = element('.publication-preview-settings');
    const mode = form.elements.preview_mode;
    const modeLabel = element('[data-preview-mode-label]');
    const aliases = element('[data-preview-aliases]');
    const save = element('[type=submit]');
    const copy = element('[data-link-copy]');
    const dismiss = element('[data-link-close]');
    const error = element('[data-link-error]');
    const status = element('[data-link-status]');
    const path = linkEndpoint(id);
    const active = () => modal === dialog && dialog.open && source.isConnected && (!parent || parent.open) && getSession() === session;
    let snapshot, busy = false, mayClose = true, previousToDisable, saved = false, previewEdit, modeOptions;
    const validName = () => input.value === snapshot?.id || /^[a-z0-9][a-z0-9-]{1,63}$/u.test(input.value);

    function renderMode() {
      const renaming = input.value !== snapshot.link_name;
      const selected = previewEdit ?? (renaming ? '' : snapshot.preview_mode);
      const options = previewModeOptions(t, escape, selected, snapshot.preview_default, {renaming});
      const name = t(renaming ? 'New address' : 'Current address');
      if (modeLabel.textContent !== name) modeLabel.textContent = name;
      if (modeOptions !== options) {
        mode.innerHTML = options;
        modeOptions = options;
      }
      mode.value = selected;
    }

    function renderDraft() {
      if (!snapshot) return;
      save.disabled = busy || input.value === snapshot.link_name && previewEdit === undefined;
      mode.disabled = busy || !validName();
      renderMode();
      const isCurrent = input.value === snapshot.link_name;
      const preview = element('[data-link-preview]');
      preview.textContent = validName() ? materialLink(snapshot.stable_url, input.value) : '—';
      preview.parentElement.hidden = isCurrent;
      keep.closest('div').hidden = isCurrent;
      const effect = t(keep.checked ? 'People can still open the material through the previous address.' : 'The previous address will stop opening.');
      const effectNode = element('[id=publication-link-effect]');
      if (effectNode.textContent !== effect) effectNode.textContent = effect;
      copy.hidden = !saved || !isCurrent;
      copy.disabled = busy;
    }

    function renderHistory() {
      const section = element('[data-link-history]');
      section.hidden = !snapshot.previous_links.length;
      const list = section.querySelector('ul');
      list.innerHTML = snapshot.previous_links.map(({name, url}) => `
        <li><span>${escape(url)}</span>${previousToDisable === name ? `
          <div class="publication-link-confirmation" role="group" aria-label="${label('Disable previous link?')}">
            <p>${label('People using this address will lose access through it.')}</p>
            <button type="button" data-link-cancel>${label('Cancel')}</button>
            <button class="danger" type="button" data-link-confirm="${escape(name)}">${label('Disable link')}</button>
          </div>` : `<button type="button" data-link-disable="${escape(name)}" aria-label="${escape(t('Disable {name}', {name}))}">${label('Disable link')}</button>`}</li>`).join('');
    }

    function renderAliasModes() {
      aliases.innerHTML = snapshot.previous_links.map(({name, preview_mode: previewMode}, index) => `
        <div class="publication-preview-alias">
          <label for="publication-preview-alias-${index}">${escape(t('Previous address {name}', {name}))}</label>
          <select id="publication-preview-alias-${index}" data-preview-alias="${escape(name)}">
            ${previewModeOptions(t, escape, previewMode, snapshot.preview_default)}
          </select>
        </div>`).join('');
    }

    function setSnapshot(value, updateDraft) {
      snapshot = value;
      element('[data-link-loading]').hidden = true;
      element('.publication-link-fields').hidden = false;
      element('[data-link-address]').textContent = materialLink(snapshot.stable_url);
      element('[data-link-prefix]').textContent = new URL(snapshot.stable_url, location.href).origin + '/p/';
      if (updateDraft) {
        input.value = snapshot.link_name;
        previewEdit = undefined;
      }
      onUpdate(snapshot);
      renderDraft();
      renderHistory();
      renderAliasModes();
    }

    function failure(problem, message) {
      if (!active()) return;
      if (problem.status === 401) {
        close();
        onError(problem);
        return;
      }
      element('[data-link-loading]').hidden = true;
      error.hidden = false;
      error.textContent = message || errorMessage(problem);
      error.focus();
    }

    async function run(task, {blockClose = true} = {}) {
      if (!active() || busy) return;
      busy = true;
      mayClose = !blockClose;
      form.setAttribute('aria-busy', 'true');
      const controls = [...form.elements];
      const disabled = controls.map(control => control.disabled);
      controls.forEach(control => { if (control !== dismiss || blockClose) control.disabled = true; });
      error.hidden = true;
      status.hidden = true;
      let focus;
      try {
        focus = await task();
      } catch (problem) {
        failure(problem);
      } finally {
        busy = false;
        mayClose = true;
        form.removeAttribute('aria-busy');
        controls.forEach((control, index) => { control.disabled = disabled[index]; });
        if (active()) {
          renderDraft();
          if (focus) queueMicrotask(() => { if (active()) focus.focus(); });
        }
      }
    }

    function dismissEditor() {
      close();
      if (source.isConnected && (!parent || parent.open)) source.focus();
    }

    dismiss.addEventListener('click', dismissEditor);
    dialog.addEventListener('cancel', event => { event.preventDefault(); if (mayClose) dismissEditor(); });
    input.addEventListener('input', () => {
      error.hidden = true;
      status.hidden = true;
      input.removeAttribute('aria-invalid');
      previewEdit = undefined;
      renderDraft();
    });
    previewSettings.addEventListener('toggle', () => {
      if (previewSettings.open && active()) {
        mode.parentElement.scrollIntoView({block: 'nearest'});
      }
    });
    mode.addEventListener('change', () => {
      error.hidden = true;
      status.hidden = true;
      const original = input.value === snapshot.link_name ? snapshot.preview_mode : '';
      previewEdit = mode.value === original ? undefined : mode.value;
      renderDraft();
    });
    keep.addEventListener('change', renderDraft);
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (!active() || busy || !snapshot || save.disabled) return;
      if (!validName()) {
        failure({}, t('Use 2–64 lowercase letters, digits or dashes. Start with a letter or digit.'));
        input.setAttribute('aria-invalid', 'true');
        input.focus();
        return;
      }
      void run(async () => {
        try {
          const renamed = input.value !== snapshot.link_name;
          const body = {name: input.value, keep_previous: keep.checked, expected_name: snapshot.link_name};
          if (previewEdit !== undefined) body.preview_mode = previewEdit;
          const value = await api(path, {method: 'PATCH', body});
          if (!active()) return;
          saved ||= renamed;
          keep.checked = false;
          previousToDisable = undefined;
          setSnapshot(value, true);
          status.textContent = t(renamed ? 'Link saved.' : 'Link preview setting saved.');
          status.hidden = false;
          return renamed ? copy : mode;
        } catch (problem) {
          const code = problem.code || problem.problem?.code;
          if (code === 'reserved_key') {
            failure(problem, t('This link name is reserved. Choose another name.'));
            input.setAttribute('aria-invalid', 'true');
            return input;
          }
          if (!['publication_link_changed', 'publication_exists'].includes(code)) throw problem;
          const latest = await api(path);
          if (!active()) return;
          if (code === 'publication_link_changed') previewEdit = undefined;
          setSnapshot(latest, false);
          if (code === 'publication_exists') {
            failure(problem, t('This link name is already in use.'));
            input.setAttribute('aria-invalid', 'true');
            return input;
          }
          failure(problem, t('The current address changed. Review it before saving again.'));
        }
      });
    });
    aliases.addEventListener('change', event => {
      const control = event.target.closest('[data-preview-alias]');
      if (!control || !active() || busy) return;
      const name = control.dataset.previewAlias;
      const original = snapshot.previous_links.find(link => link.name === name).preview_mode;
      const preview_mode = control.value;
      if (preview_mode === original) return;
      void run(async () => {
        let value;
        try {
          value = await api(`${path}/${encodeURIComponent(name)}`, {method: 'PATCH', body: {preview_mode}});
        } catch (problem) {
          if (!active()) return;
          if ((problem.code || problem.problem?.code) === 'publication_link_not_found') {
            const latest = await api(path);
            if (active()) setSnapshot(latest, false);
          } else {
            control.value = original;
          }
          throw problem;
        }
        if (!active()) return;
        setSnapshot(value, false);
        status.textContent = t('Link preview setting saved.');
        status.hidden = false;
        return [...aliases.querySelectorAll('select')].find(select => select.dataset.previewAlias === name);
      });
    });
    element('[data-link-history]').addEventListener('click', event => {
      if (!active() || busy) return;
      const trigger = event.target.closest('button');
      if (!trigger) return;
      if (trigger.hasAttribute('data-link-confirm')) {
        const name = trigger.dataset.linkConfirm;
        void run(async () => {
          let value;
          try {
            value = await api(`${path}/${encodeURIComponent(name)}`, {method: 'DELETE'});
          } catch (problem) {
            if ((problem.code || problem.problem?.code) !== 'publication_link_not_found') throw problem;
            value = await api(path);
          }
          if (!active()) return;
          previousToDisable = undefined;
          setSnapshot(value, false);
          status.textContent = t('Previous link disabled.');
          status.hidden = false;
          return element('[data-link-disable]') || input;
        });
      } else {
        const name = trigger.dataset.linkDisable || previousToDisable;
        previousToDisable = trigger.dataset.linkDisable;
        renderHistory();
        if (previousToDisable) element('[data-link-cancel]').focus();
        else [...dialog.querySelectorAll('[data-link-disable]')].find(button => button.dataset.linkDisable === name)?.focus();
      }
    });
    copy.addEventListener('click', async () => {
      if (!active() || busy) return;
      let manualURL;
      await run(async () => {
        await copyResolvedLink({
          isCurrent: active,
          load: async () => {
            const value = await api(path);
            if (active()) setSnapshot(value, true);
            return materialLink(value.stable_url);
          },
          onCopied: () => {
            status.textContent = t('Copied.');
            status.hidden = false;
          },
          fallback: url => { manualURL = url; },
        });
        return copy;
      }, {blockClose: false});
      if (active() && manualURL) showManualCopy(manualURL, t('Copy this link'));
    });
    source.focus({preventScroll: true});
    document.body.append(dialog);
    dialog.showModal();
    void run(async () => {
      const value = await api(path);
      if (!active()) return;
      setSnapshot(value, true);
      return input;
    }, {blockClose: false});
  }

  return {open, close};
}
