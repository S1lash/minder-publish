/* Only these local continuations may follow an owner login. */
export function safeReturnTo(value) {
  if(typeof value!=='string')return null;
  if(/^\/p\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?:~[1-9][0-9]{0,18})?(?:\?(?:preview=tools|revision=[a-f0-9]{32})(?:&(?:preview=tools|revision=[a-f0-9]{32}))?)?$/.test(value))return value;
  if(/^\/viewer\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?:\?revision=[a-f0-9]{32})?$/.test(value))return value;
  if(/^\/upload\/[a-f0-9]{32}$/.test(value))return value;
  if(/^\/oauth\/consent\?request=[A-Za-z0-9_-]{43}$/.test(value))return value;
  return null;
}
