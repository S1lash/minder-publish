export const previewPolicyLabel = 'Link previews in chats';

export function previewModeOptions(t, escape, mode, defaultEnabled, {renaming = false} = {}) {
  const inherited = defaultEnabled ? 'Use instance default (on)' : 'Use instance default (off)';
  const options = [['inherit', inherited], ['on', 'On'], ['off', 'Off']];
  if (renaming) options.unshift(['', 'Keep address setting']);
  return options.map(([value, key]) =>
    `<option value="${value}"${value === mode ? ' selected' : ''}>${escape(t(key))}</option>`
  ).join('');
}
