export function mountTooltips() {
    const selector = '[data-tooltip], [data-tooltip-overflow], .icon-button[aria-label], .nav-button, .segmented button, #sidebar-toggle, #theme-button, #owner-button, #language-select-trigger';
    const tooltip = document.createElement('div');
    tooltip.id = 'publish-tooltip'; tooltip.className = 'ui-tooltip';
    tooltip.setAttribute('role', 'tooltip'); tooltip.hidden = true;
    document.body.append(tooltip);
    let anchor = null, pinned = false, showTimer, hideTimer;
    const stripNativeTitles = () => [...document.querySelectorAll(selector)].forEach(element => element.removeAttribute('title'));
    function hide() {
      clearTimeout(showTimer); clearTimeout(hideTimer);
      if (anchor) {
        const descriptions = (anchor.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== tooltip.id);
        if (descriptions.length) anchor.setAttribute('aria-describedby', descriptions.join(' '));
        else anchor.removeAttribute('aria-describedby');
      }
      anchor = null; pinned = false; tooltip.hidden = true;
      if (tooltip.parentElement !== document.body) document.body.append(tooltip);
    }
    function show(element, delay = 0) {
      clearTimeout(hideTimer);
      if (anchor === element && !tooltip.hidden) return;
      hide();
      if (!element || element.disabled) return;
      element.removeAttribute('title');
      anchor = element;
      showTimer = setTimeout(() => {
        if (!element.isConnected || !element.getClientRects().length) { hide(); return; }
        const overflowSelector = element.dataset.tooltipOverflow;
        const overflow = overflowSelector ? element.parentElement.querySelector(overflowSelector) : element;
        const label = overflowSelector !== undefined
          ? overflow && (overflow.scrollWidth > overflow.clientWidth || overflow.scrollHeight > overflow.clientHeight) && overflow.textContent
          : element.dataset.tooltip || element.getAttribute('aria-label');
        if (!label) { hide(); return; }
        // Keep the tooltip in the modal's top layer when its trigger is in a dialog.
        (element.closest('dialog[open]') || document.body).append(tooltip);
        tooltip.classList.toggle('ui-tooltip-description', overflowSelector !== undefined);
        tooltip.textContent = label; tooltip.hidden = false;
        const descriptions = new Set((element.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
        descriptions.add(tooltip.id); element.setAttribute('aria-describedby', [...descriptions].join(' '));
        const trigger = element.getBoundingClientRect();
        const bounds = tooltip.getBoundingClientRect();
        const left = Math.max(12, Math.min(trigger.left + (trigger.width - bounds.width) / 2, innerWidth - bounds.width - 12));
        const above = trigger.top - bounds.height - 8;
        tooltip.dataset.placement = above >= 12 ? 'above' : 'below';
        const top = above >= 12 ? above : Math.min(trigger.bottom + 8, innerHeight - bounds.height - 12);
        tooltip.style.left = `${left}px`; tooltip.style.top = `${Math.max(12, top)}px`;
      }, delay);
    }
    const scheduleHide = () => { clearTimeout(showTimer); clearTimeout(hideTimer); hideTimer = setTimeout(hide, 120); };
    document.addEventListener('pointerover', event => {
      if (event.pointerType === 'touch' || pinned) return;
      if (tooltip.contains(event.target)) { clearTimeout(hideTimer); return; }
      const element = event.target.closest(selector);
      if (element && !element.contains(event.relatedTarget)) show(element, 180);
    });
    document.addEventListener('pointerout', event => {
      if (!pinned && !anchor?.matches(':focus-visible') && (anchor?.contains(event.target) || tooltip.contains(event.target))
        && !anchor?.contains(event.relatedTarget) && !tooltip.contains(event.relatedTarget)) scheduleHide();
    });
    document.addEventListener('focusin', event => {
      const element = event.target.closest(selector);
      if (element?.matches(':focus-visible')) show(element);
    });
    document.addEventListener('focusout', event => { if (anchor?.contains(event.target)) hide(); });
    document.addEventListener('pointerdown', event => {
      if (!event.target.closest('[data-tooltip-toggle]')) hide();
    });
    document.addEventListener('click', event => {
      const element = event.target.closest('[data-tooltip-toggle]');
      if (!element) return;
      if (pinned && anchor === element) { hide(); return; }
      show(element); clearTimeout(hideTimer); pinned = true;
    });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !anchor) return;
      if (!tooltip.hidden) event.preventDefault();
      hide();
    });
    document.addEventListener('scroll', hide, true);
    document.addEventListener('close', event => {
      if (event.target.contains(tooltip) || !tooltip.isConnected) hide();
    }, true);
    window.addEventListener('resize', hide);
    document.addEventListener('publish:languagechange', () => { hide(); stripNativeTitles(); });
    document.addEventListener('publish:formchange', () => { if (anchor && !anchor.isConnected) hide(); });
    stripNativeTitles();
  }
