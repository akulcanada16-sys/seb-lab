const PYODIDE_URL='https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs';
let active=false;
self.onmessage=async event=>{
  const {id,payload}=event.data||{};
  if(active){self.postMessage({id,type:'error',message:'This worker already has a running experiment.'});return;}
  active=true;
  const progress=message=>self.postMessage({id,type:'progress',message});
  try{
    if(!payload||typeof payload!=='object')throw new Error('An experiment input is required.');
    progress('Loading Python locally in your browser…');
    const {loadPyodide}=await import(PYODIDE_URL);
    const pyodide=await loadPyodide();
    progress('Loading the frozen research engine…');
    const response=await fetch(new URL('./engine-bundle.json',import.meta.url));
    if(!response.ok)throw new Error('The research source bundle could not be loaded.');
    const bundle=await response.json();
    pyodide.FS.mkdirTree('/research');
    for(const file of bundle.files){
      if(typeof file.path!=='string'||file.path.includes('..')||file.path.startsWith('/')||file.path.includes('\\'))throw new Error('Invalid research package path.');
      const destination='/research/'+file.path;
      pyodide.FS.mkdirTree(destination.slice(0,destination.lastIndexOf('/')));
      pyodide.FS.writeFile(destination,file.content,{encoding:'utf8'});
    }
    await pyodide.loadPackage('tzdata');
    pyodide.runPython("import sys\nsys.path.insert(0, '/research')");
    pyodide.globals.set('research_payload_json',JSON.stringify(payload));
    progress('Evaluating the strategies on finalized bars. You can cancel this run.');
    const raw=await pyodide.runPythonAsync("import json\nfrom public_api import run_research\njson.dumps(run_research(json.loads(research_payload_json)), allow_nan=False)");
    const result=JSON.parse(raw);
    pyodide.globals.delete('research_payload_json');
    self.postMessage({id,type:'result',result});
  }catch(error){self.postMessage({id,type:'error',message:String(error?.message||error)});}
};

