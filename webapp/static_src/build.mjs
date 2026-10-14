import * as esbuild from 'esbuild';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.resolve(here, '..', 'static', 'vendor');

await esbuild.build({
  entryPoints: [path.resolve(here, 'entry.js')],
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ['chrome100', 'edge100', 'firefox100', 'safari15'],
  format: 'iife',
  outdir,
  entryNames: 'univer',
  assetNames: 'assets/[name]-[hash]',
  loader: {
    '.woff': 'file',
    '.woff2': 'file',
    '.ttf': 'file',
    '.eot': 'file',
    '.svg': 'file',
    '.png': 'file',
    '.jpg': 'file',
    '.gif': 'file',
  },
  logLevel: 'info',
});

console.log('Univer bundle built ->', outdir);
