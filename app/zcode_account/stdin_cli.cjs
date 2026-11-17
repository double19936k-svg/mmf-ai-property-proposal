/* Keep long prompts off the Windows command line; run the unmodified official CLI. */
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const cli=process.argv[2];
if(!cli || !path.isAbsolute(cli))throw new Error('Official CLI path is required');
const input=fs.readFileSync(0,'utf8');
if(Buffer.byteLength(input,'utf8')>8*1024*1024)throw new Error('CLI input is too large');
const args=JSON.parse(input);
if(!Array.isArray(args)||args.length>512||args.some(x=>typeof x!=='string'))throw new Error('Invalid CLI arguments');
process.argv=[process.execPath,cli,...args];
require(cli);
