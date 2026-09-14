/* Bundle only the public research engine. Never point this at the working trading project. */
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const core=path.resolve(__dirname,'research_core');
const output=path.resolve(__dirname,'web/engine-bundle.json');
if(path.basename(core)!=='research_core')throw new Error('Unexpected research source directory.');
const files=[];
function walk(directory){
  for(const entry of fs.readdirSync(directory,{withFileTypes:true})){
    if(entry.name.startsWith('.')||entry.name==='__pycache__')continue;
    const absolute=path.join(directory,entry.name);
    if(entry.isSymbolicLink())throw new Error('Symlinks are not allowed in the public bundle.');
    if(entry.isDirectory())walk(absolute);
    else if(entry.name.endsWith('.py')||entry.name.endsWith('.json')){
      const relative=path.relative(core,absolute).replaceAll('\\','/');
      if(relative.startsWith('tests/')||relative.includes('sample-result')||relative.includes('verification'))continue;
      const content=fs.readFileSync(absolute,'utf8');
      if(/(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|C:\\\\Users\\\\)/.test(content))throw new Error('Sensitive-looking content in '+relative);
      files.push({path:relative,content});
    }
  }
}
for(const folder of ['seb_engine','frozen_config'])walk(path.join(core,folder));
for(const name of ['public_api.py']){
  const absolute=path.join(core,name);
  if(!fs.existsSync(absolute))throw new Error('Missing '+name);
  files.push({path:name,content:fs.readFileSync(absolute,'utf8')});
}
if(!files.some(f=>f.path==='public_api.py'))throw new Error('Missing public_api.py.');
for(const file of files){if(/(?:github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|sk-(?:ant-)?[A-Za-z0-9_-]{20,})/.test(file.content))throw new Error('Sensitive-looking content in '+file.path);}
files.sort((a,b)=>a.path.localeCompare(b.path));
const serialized=JSON.stringify({files});
fs.writeFileSync(output,serialized+'\n');
console.log(JSON.stringify({files:files.length,bytes:Buffer.byteLength(serialized),sha256:crypto.createHash('sha256').update(serialized).digest('hex')}));
