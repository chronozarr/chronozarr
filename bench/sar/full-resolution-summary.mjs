import fs from 'node:fs';
const median=a=>a.length?[...a].sort((a,b)=>a-b)[Math.floor(a.length/2)]:null;
for(const file of process.argv.slice(2)){
 const rows=fs.readFileSync(file,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
 const output=[];
 for(const sensor of ['sentinel1','nisar'])for(const view of ['full','bounded']){
  const samples=rows.filter(r=>r.sensor===sensor&&r.view===view);
  if(!samples.length)continue;
  for(const name of ['cold','prefetch','play30','play60','switch']){
   const phases=samples.map(r=>r.phases.find(p=>(p.name==='play'?`play${p.args.fps}`:p.name)===name)).filter(Boolean);
   const switches=phases.flatMap(p=>p.result?.measurements??[]);
   const network=phases.map(p=>({requests:p.after.network.requests-(p.statsReset?0:p.before.network.requests),bytes:p.after.network.bytes-(p.statsReset?0:p.before.network.bytes)}));
   output.push({sensor,view,phase:name,runs:phases.length,elapsedMs:median(phases.map(p=>p.elapsedMs)),firstCompleteLod0Ms:median(phases.map(p=>p.firstCompleteLod0Ms).filter(Number.isFinite)),
    achievedStepsPerSecond:median(phases.map(p=>p.result?.achievedStepsPerSecond).filter(Number.isFinite)),finished:phases.map(p=>p.result?.finished??null),steps:phases.map(p=>p.result?.stats?.steps??null),
    readerRequests:network.map(n=>n.requests),readerBytes:network.map(n=>n.bytes),pageMaskRequests:phases.map(p=>p.pageRequestsByArray['0/mask']??null),
    cells:phases.map(p=>p.paints.cells),paintLevels:phases.map(p=>p.paints.levels),completePaints:phases.map(p=>p.paints.complete),partialPaints:phases.map(p=>p.paints.partial),
    switchComplete:switches.length?switches.filter(s=>s.complete).length:null,switchCount:switches.length||null,switchMedianMs:median(switches.map(s=>s.ms)),switchP95Ms:switches.length?[...switches.map(s=>s.ms)].sort((a,b)=>a-b)[Math.ceil(switches.length*0.95)-1]:null,switchMaxMs:switches.length?Math.max(...switches.map(s=>s.ms)):null,switchRequests:switches.length?switches.reduce((n,s)=>n+s.requests,0):null,
    budgets:phases[0]?.budgets,gpuSlots:phases[0]?.gpu.slots,cache:phases[0]?.cache});
  }
 }
 console.log(JSON.stringify({file,errors:rows.filter(r=>r.error).map(r=>({sensor:r.sensor,view:r.view,error:r.error})),results:output},null,2));
}
