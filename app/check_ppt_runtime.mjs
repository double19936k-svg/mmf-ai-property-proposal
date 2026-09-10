// Import-only readiness check: never creates a presentation or renders files.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';

try {
  const modules = path.resolve(process.argv[2] || path.join(path.dirname(fileURLToPath(import.meta.url)), 'node_modules'));
  const pkg = path.join(modules, '@oai', 'artifact-tool', 'package.json');
  if (!fs.existsSync(pkg)) throw new Error('MISSING_PPT_PACKAGE: @oai/artifact-tool');
  const manifest = JSON.parse(fs.readFileSync(pkg, 'utf8'));
  const appManifest = JSON.parse(fs.readFileSync(path.join(modules, '..', 'package.json'), 'utf8'));
  const expected = appManifest.dependencies?.['@oai/artifact-tool'];
  if (!expected || manifest.version !== expected) throw new Error('PPT_PACKAGE_VERSION_MISMATCH');
  const require = createRequire(path.join(modules, '..', 'package.json'));
  const entry = require.resolve('@oai/artifact-tool');
  const relative = path.relative(fs.realpathSync(modules), fs.realpathSync(entry));
  if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('PPT_PACKAGE_OUTSIDE_RUNTIME');
  const api = await import(pathToFileURL(entry).href);
  if (typeof api.Presentation !== 'function' || typeof api.PresentationFile !== 'function') throw new Error('PPT_EXPORTS_MISSING');
  console.log(JSON.stringify({status: 'PASS', version: manifest.version, node: process.version, entry}));
} catch (error) {
  console.log(JSON.stringify({status: 'FAIL', error: String(error.message || error)}));
  process.exitCode = 1;
}
