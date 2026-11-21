/* Owner-only aggregates. No visitor identifiers or collection notices are rendered. */
const mountedPanels = new WeakMap();

export async function mountStatistics({root, id, api, t, escape, date, isCurrent = () => true, onError, onUpdate, lazy = false}) {
  const mount = {};
  mountedPanels.set(root, mount);
  const active = () => mountedPanels.get(root) === mount && root.isConnected && isCurrent();
  let selectedAudience = 'both';
  let generation = 0, selectedDays = 30, cachedDays = 30, cached = null, pending = null;
  const label = key => escape(t(key));

  function chart(visible, number) {
    const dates = [...new Set(visible.flatMap(({series}) => (series.daily ?? []).map(item => item.day)))].sort();
    if (!dates.length) return '';
    const readings = visible.map(({audience,title,series}) => ({
      audience, title, days: new Map((series.daily ?? []).map(item => [item.day,item.opens])),
    }));
    const maximum = Math.max(1, ...readings.flatMap(({days}) => [...days.values()].filter(Number.isFinite)));
    const paths = readings.map(({audience,days}) => {
      let drawing = false;
      const points = [];
      dates.forEach((day,index) => {
        const count = days.get(day);
        if (!Number.isFinite(count)) { drawing = false; return; }
        const x = dates.length === 1 ? 300 : 4 + 592 * index / (dates.length - 1);
        const y = 110 - Math.max(0, count) * 100 / maximum;
        points.push(`${drawing?'L':'M'}${x.toFixed(2)} ${y.toFixed(2)}${drawing?'':'h.01'}`);
        drawing = true;
      });
      return `<path data-statistics-series="${audience}" d="${points.join(' ')}"/>`;
    }).join('');
    const chartLabel = visible.length === 2 ? 'Daily openings, UTC' : visible[0].audience === 'owner' ? 'Your daily openings, UTC' : 'Daily guest openings, UTC';
    const legend = readings.map(({audience,title}) => `<span><i class="statistics-key" data-series="${audience}" aria-hidden="true"></i>${label(title)}</span>`).join('');
    const cell = (reading,day) => {
      const count = reading.days.get(day);
      return Number.isFinite(count) ? number(count) : label('Not collected');
    };
    const dayCells = dates.map(day => `<tr><th scope="row">${escape(day)}</th>${readings.map(reading => `<td>${cell(reading,day)}</td>`).join('')}</tr>`).join('');
    return `<figure class="statistics-figure">
      <div class="statistics-legend">${legend}</div>
      <div class="statistics-plot">
        <div class="statistics-scale" aria-hidden="true"><span>${number(maximum)}</span><span>${number(0)}</span></div>
        <svg viewBox="0 0 600 120" preserveAspectRatio="none" role="img" aria-label="${label(chartLabel)}">
          <path class="statistics-grid" d="M0 10h600M0 60h600M0 110h600"/>
          ${paths}
        </svg>
      </div>
      <figcaption class="statistics-caption"><span>${escape(dates[0])}</span><span>UTC</span><span>${escape(dates.at(-1))}</span></figcaption>
    </figure>
    <details class="statistics-data"><summary>${label('Daily counts')}</summary>
      <table class="statistics-table"><thead><tr><th scope="col">${label('Date')} · UTC</th>${readings.map(({title}) => `<th scope="col">${label(title)}</th>`).join('')}</tr></thead><tbody>${dayCells}</tbody></table>
    </details>`;
  }

  function audienceDetails({audience,title,series}, number) {
    const owner = audience === 'owner';
    return `<div class="statistics-audience-detail">
      <h3>${label(title)}</h3>
      <p class="statistics-summary">${label('Total since collection started')}: ${number(series.total)}<br>
      ${label(owner?'Your last opening':'Last guest opening')}: ${escape(series.last_opened?date(series.last_opened):t('No openings yet'))}
      ${series.started_at?`<br>${label('Collection started')}: ${escape(date(series.started_at))}`:''}</p>
      ${!series.enabled?`<p class="callout">${label('Collection is paused for this material. Existing counts are retained.')}</p>`:''}
      ${series.incomplete?`<p class="callout warning">${label('Some daily history is unavailable. The total still includes those openings.')}</p>`:''}
      ${!owner&&series.legacy_total?`<p class="statistics-summary">${label('Earlier counter')}: ${number(series.legacy_total)}. ${label('Earlier counts may include owner checks and failed openings.')}</p>`:''}
      ${owner&&series.collection_interrupted?`<p class="callout warning">${label('Owner collection may have gaps while an older release was running.')}</p>`:''}
      <p class="statistics-summary">${label(owner?'Counts your signed-in openings, including previews, history and clean view. Guest access checks are excluded. Earlier owner openings are unavailable.':'Counts accepted guest openings through the shared link, not unique people or completed reading. Your signed-in openings and guest access checks are excluded.')}</p>
    </div>`;
  }

  function render(value = cached, days = cachedDays) {
    if (!active() || !value) return;
    const audiences = [
      {audience:'guest', title:'Guests', series:value},
      {audience:'owner', title:'You', series:value.owner},
    ].filter(({series}) => series);
    const visible = selectedAudience === 'both' ? audiences : audiences.filter(({audience}) => audience === selectedAudience);
    const number = amount => new Intl.NumberFormat(document.documentElement.lang).format(Number(amount) || 0);
    const focus = document.activeElement?.dataset;
    const focusSelector = focus?.statisticsAudience ? `[data-statistics-audience="${focus.statisticsAudience}"]` :
      focus?.statisticsDays ? `[data-statistics-days="${focus.statisticsDays}"]` :
      focus?.statisticsEnabled ? `[data-statistics-enabled="${focus.statisticsEnabled}"]` : null;
    const restoreFocus = focusSelector && root.contains(document.activeElement);

    root.innerHTML = `<div class="viewer-section-head"><h2>${label('Opening statistics')}</h2></div>
      <div class="statistics-periods" role="group" aria-label="${label('Audience')}">
        ${[['both','Both audiences'],...audiences.map(({audience,title})=>[audience,title])].map(([audience,title])=>`<button type="button" data-statistics-audience="${audience}" aria-pressed="${selectedAudience===audience}">${label(title)}</button>`).join('')}
      </div>
      <div class="statistics-periods" role="group" aria-label="${label('Period')}">
        ${[7,30,90].map(period=>`<button type="button" data-statistics-days="${period}" aria-pressed="${days===period}">${escape(t('{days} days',{days:period}))}</button>`).join('')}
      </div>
      <p class="statistics-period-label">${label('openings in this period')}</p>
      <div class="statistics-totals">
        ${visible.map(({title,series}) => `<div><span>${label(title)}</span><strong>${number(series.period_total)}</strong></div>`).join('')}
      </div>
      ${chart(visible,number)}
      <div class="statistics-audience-details">${visible.map(series => audienceDetails(series,number)).join('')}</div>
      <div class="statistics-actions">
        ${visible.map(({audience,series}) => `<label class="checkbox-label"><input type="checkbox" data-statistics-enabled="${audience}" ${series.enabled?'checked':''}><span>${label(audience==='owner'?'Count my openings':'Count guest openings')}</span></label>`).join('')}
        <button type="button" class="danger" data-statistics-clear>${label('Clear all statistics')}</button>
      </div>`;

    root.querySelectorAll('[data-statistics-audience]').forEach(button => button.addEventListener('click', () => {
      selectedAudience = button.dataset.statisticsAudience;
      render();
    }));
    root.querySelectorAll('[data-statistics-days]').forEach(button => button.addEventListener('click', () => load(Number(button.dataset.statisticsDays))));
    root.querySelectorAll('[data-statistics-enabled]').forEach(control => control.addEventListener('change', async event => {
      const input = event.currentTarget;
      const policy = input.dataset.statisticsEnabled === 'owner' ? 'owner_counts' : 'guest_counts';
      input.disabled = true;
      try {
        await api(`/publications/${encodeURIComponent(id)}`,{method:'PATCH',body:{[policy]:input.checked}});
        if (active()) await load(selectedDays,{force:true});
      } catch(error) {
        input.checked = !input.checked;
        if (active()) onError(error);
      } finally {input.disabled = false;}
    }));
    if (restoreFocus) root.querySelector(focusSelector)?.focus();
    root.querySelector('[data-statistics-clear]').addEventListener('click',()=>{
      const modal=document.createElement('dialog');modal.className='dialog';modal.setAttribute('aria-label',t('Clear all statistics'));
      modal.innerHTML=`<div class="dialog-body"><h2>${escape(t('Clear all statistics'))}</h2><p>${escape(t('Delete all guest, owner and earlier opening counts for this material? This cannot be undone.'))}</p><p class="callout error" role="alert" hidden></p></div><footer class="dialog-actions"><button type="button" data-cancel>${escape(t('Cancel'))}</button><button type="button" class="danger" data-confirm>${escape(t('Clear all statistics'))}</button></footer>`;
      const dispose=()=>{modal.close();modal.remove();};
      modal.querySelector('[data-cancel]').addEventListener('click',dispose);
      modal.addEventListener('cancel',event=>{event.preventDefault();dispose();});
      modal.querySelector('[data-confirm]').addEventListener('click',async event=>{
        const button=event.currentTarget;button.disabled=true;
        try{if(!active())return dispose();await api(`/publications/${encodeURIComponent(id)}/statistics`,{method:'DELETE'});dispose();if(active())await load(days,{force:true});}
        catch(error){if(error.status===401){dispose();onError(error);return;}if(!active())return dispose();const output=modal.querySelector('[role=alert]');output.textContent=error.message;output.hidden=false;button.disabled=false;}
      });
      document.body.append(modal);modal.showModal();
    });
    document.dispatchEvent(new Event('publish:formchange'));
  }
  function load(days = selectedDays, {force = false} = {}) {
    if (!active()) return Promise.resolve();
    if (!force && pending?.days === days) return pending.promise;
    selectedDays = days;
    const request = ++generation;
    root.setAttribute('aria-busy','true');
    const operation = {days};
    operation.promise = (async () => {
      try {
        const value = await api(`/publications/${encodeURIComponent(id)}/statistics?days=${days}`);
        if (!active() || request !== generation) return;
        cached = value;cachedDays = days;
        onUpdate?.(value);
        render(value, days);
      } catch(error) {if (active() && request===generation) onError(error);}
      finally {
        if(pending===operation)pending=null;
        if(active()&&request===generation)root.removeAttribute('aria-busy');
      }
    })();
    pending = operation;
    return operation.promise;
  }
  if (!lazy) await load();
  return {render:()=>render(), refresh:()=>load()};
}
