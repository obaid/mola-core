const HOST = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+decodo\.com$/;

/** Private host input only. Never expose endpoint authentication in a description. */
export function validateBrowserProxy(value, imageRef) {
  if (value == null) return null;
  if (!imageRef?.startsWith('ubuntu-xfce:')) throw new Error('Browser proxies currently require the Ubuntu desktop image.');
  if (typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== 'host,password,port,provider,username'
    || value.provider !== 'decodo' || typeof value.host !== 'string'
    || value.host.length > 253 || !HOST.test(value.host)
    || !Number.isInteger(value.port) || value.port < 1 || value.port > 65535
    || !['username', 'password'].every(key => typeof value[key] === 'string'
      && value[key].length > 0 && value[key].length <= 512 && !/[\x00-\x1f\x7f]/.test(value[key]))
    || value.username.includes(':')) throw new Error('Invalid Decodo browser proxy configuration.');
  return { provider: 'decodo', host: value.host, port: value.port, username: value.username, password: value.password };
}
