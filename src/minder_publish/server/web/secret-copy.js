/* An explicit owner action may deliver a secret only to its still-current context. */
export async function copyOwnerSecret({load,isCurrent,write,fallback,onCopied}) {
  if (!isCurrent()) return;
  let response;
  try {
    response = await load();
    if (!isCurrent()) return;
    if (typeof response?.password !== 'string' || !response.password) {
      throw new Error('Could not complete the request.');
    }
    try {
      await write(response.password);
    } catch {
      if (isCurrent()) fallback(response.password);
      return;
    }
    if (isCurrent()) onCopied();
  } finally {
    response = null;
  }
}
