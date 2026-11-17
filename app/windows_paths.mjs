// Win32 extended filesystem paths are not valid Node module-resolution bases.
// Normalize their spelling before createRequire/import; bytes stay at the same path.
export function modulePath(value) {
  if (process.platform !== 'win32') return value;
  if (value.startsWith('\\\\?\\UNC\\')) return '\\\\' + value.slice(8);
  if (value.startsWith('\\\\?\\')) return value.slice(4);
  return value;
}
