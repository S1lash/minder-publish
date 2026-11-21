export function mountManualCopy(dialog) {
  const heading = dialog.querySelector("h2");
  const output = dialog.querySelector("textarea");
  dialog.addEventListener("close", () => {
    if (!dialog.open) output.value = "";
  });

  return (text, title) => {
    if (dialog.open) return;
    heading.textContent = title;
    output.value = text;
    dialog.showModal();
    output.select();
  };
}

// Pass a promise to the clipboard while the initiating click is still active.
export async function copyResolvedLink({load, isCurrent, onCopied, fallback}) {
  if (!isCurrent()) return;
  const address = Promise.resolve().then(load);
  let written = false;
  let clipboard;
  if (typeof ClipboardItem === 'function' && typeof navigator.clipboard?.write === 'function') {
    const data = address.then(url => {
      if (!isCurrent()) throw new Error('Copy context closed');
      return new Blob([url], {type: 'text/plain'});
    });
    void data.catch(() => {});
    try {
      clipboard = navigator.clipboard.write([new ClipboardItem({'text/plain': data})]).then(() => true, () => false);
    } catch {
      // If the browser rejects deferred clipboard data, try text and then manual copy.
    }
  }
  const url = await address;
  if (!isCurrent()) return;
  if (clipboard) written = await clipboard;
  if (!written && isCurrent()) {
    try {
      await navigator.clipboard.writeText(url);
      written = true;
    } catch {
      // The manual copy dialog is the fallback where clipboard permission is unavailable.
    }
  }
  if (!isCurrent()) return;
  if (written) onCopied();
  else fallback(url);
}
