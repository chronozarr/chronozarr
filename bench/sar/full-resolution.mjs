// node bench/sar/full-resolution.mjs [label=after] [repeats=1]
// BASELINE_DIR optionally serves an immutable snapshot with js/... paths.
// CASES=full,bounded SENSORS=sentinel1,nisar FPS=30,60 CAP_MS=45000 TOTAL_GIB optional diagnostic
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {startServer,launchBrowser,newPage,attachNet,subtract} from '../lib/harness.mjs';
const label=process.argv[2]??'after', repeats=Number(process.argv[3]??1);
const out=process.env.OUT??`bench/sar/results/full-resolution-${label}.jsonl`;
const stores={sentinel1:'ucayali_santa_maria/s1-rtc-monthly-v1',nisar:'colorado_yuma/nisar-gcov-v2'};
const sourceRoot=process.env.BASELINE_DIR??'.';
const hash=p=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const provenance=Object.fromEntries(['js/demo/viewer.js','js/chronozarr/decoder.js','js/chronozarr/cache.js','js/chronozarr/aux-cache.js'].filter(p=>fs.existsSync(path.join(sourceRoot,p))).map(p=>[p,hash(path.join(sourceRoot,p))]));
const server=await startServer(); fs.mkdirSync(path.dirname(out),{recursive:true});
try {
for(const sensor of (process.env.SENSORS??'sentinel1,nisar').split(','))
for(const view of (process.env.CASES??'full,bounded').split(','))
for(let repeat=0;repeat<repeats;repeat++) {
 const browser=await launchBrowser({channel:'chromium'});
 const row={label,sensor,view,repeat,harnessHash:hash('bench/sar/full-resolution.mjs'),storeRootHash:hash(`data/stores/${stores[sensor]}/zarr.json`),at:new Date().toISOString(),provenance,environment:{cpu:os.cpus()[0].model,totalMemory:os.totalmem()},phases:[]};
 try {
 const {page,context}=await newPage(browser,{width:1440,height:900,deviceScaleFactor:2,blockCatalog:true});
 if(process.env.BASELINE_DIR) await page.route('**/js/**/*.js',async route=>{
  const file=path.join(sourceRoot,new URL(route.request().url()).pathname);
  if(fs.existsSync(file)) await route.fulfill({path:file,contentType:'text/javascript',headers:{'cache-control':'no-store'}});else await route.continue();
 });
 const url=`${server.url}/data/stores/${stores[sensor]}`;
 const {counter}=await attachNet(context,page,[url]);
 row.errors=[];page.on('pageerror',e=>row.errors.push(e.message));
 await page.goto(`${server.url}/js/demo/index.html?store=${encodeURIComponent(url)}`);
 await page.waitForFunction(()=>window.chronozarr?.ready,null,{timeout:60000});
 // Every phase has a wall clock cap; reader stats count worker traffic, unlike page-only CDP.
 const phase=async(name,args)=>{
  const before=counter.snapshot(), index=counter.entries().length;
  const measurement=await Promise.race([page.evaluate(async({name,args,capMs})=>{
   const v=chronozarr.viewer; await Promise.race([chronozarr.ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error('initial ready timeout')),capMs))]);
   const start=performance.now();
   const sleep=ms=>new Promise(r=>setTimeout(r,ms));
   const bounded=promise=>Promise.race([promise,new Promise(resolve=>setTimeout(()=>resolve({timeout:true}),capMs))]);
   const before=structuredClone(v.store.stats()); const events=[];
   v.probe=e=>events.push(e);
   let result;
   if(name==='cold') {
    const l=v.store.levels[0];
    const camera=args.view==='bounded'?{cx:768,cy:768,scale:Math.max(v.canvas.width,v.canvas.height)/1024}:undefined;
    result=await bounded(v.loadStore(v.store.url,{lod:0,camera,fetch:(input,init)=>fetch(input,{...init,cache:'no-store'})}));
    while(performance.now()-start<capMs&&!(v.shownFrame?.lod===0&&v.shownFrame?.kind==='target')) await sleep(20);
   } else if(name==='budget') {const previous=v.store.budgets();v.store.setBudgets({totalBytes:args.totalBytes});result={previous,current:v.store.budgets()};}
   else if(name==='prefetch') result=await bounded(v.prefetchNow({movie:true}));
   else if(name==='switch') {
    const measurements=[];
    for(let i=0;i<20;i++){const b=v.store.stats(),at=performance.now();v.goToTime((v.t+1)%v.store.times.length);const frame=v.renderNow();v.renderer.finish();const a=v.store.stats();measurements.push({ms:performance.now()-at,complete:frame.complete,lod:frame.lod,requests:a.network.requests-b.network.requests,bytes:a.network.bytes-b.network.bytes});await sleep(30);}
    result={measurements};
   } else {
    v.playback.setSpeed(args.fps);v.play();const wanted=v.store.times.length*2;
    while(v.playback.stats.steps<wanted&&performance.now()-start<capMs) await sleep(10);
    const movie=v.movieInfo,stats={...v.playback.stats};v.pause();
    result={wanted,finished:stats.steps>=wanted,stats,movie,effectiveStepsPerSecond:v.playback.effectiveStepsPerSecond,refreshHz:v.playback.refreshHz,
      achievedStepsPerSecond:stats.lastStepAt>stats.firstStepAt?(stats.steps-1)*1000/(stats.lastStepAt-stats.firstStepAt):null};
   }
   v.probe=null;
   const paints=events.filter(e=>e.type==='paint');const steps=events.filter(e=>e.type==='input');const wraps=steps.map((e,i)=>e.t===0?i:-1).filter(i=>i>=0);const boundaries=[0,...wraps.filter(i=>i>0)];
   const loopRates=boundaries.slice(1).map((end,i)=>1000*(end-boundaries[i])/(steps[end].at-steps[boundaries[i]].at));
   const gl=v.canvas.getContext('webgl2'), ext=gl.getExtension('WEBGL_debug_renderer_info');
   return {elapsedMs:performance.now()-start,result,before,after:structuredClone(v.store.stats()),shownFrame:v.shownFrame,lodOverride:v.lodOverride,
    camera:{...v.camera},canvas:{width:v.canvas.width,height:v.canvas.height},budgets:v.store.budgets(),cache:v.store.cacheInfo(),gpu:{slots:v.renderer.slots,budgetBytes:384*1024**2,renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER)},
    statsReset:name==='cold',timesteps:v.store.times.length,levels:v.store.levels.map(l=>({width:l.width,height:l.height,chunkBytes:l.chunkBytes,gridRows:l.gridRows,gridCols:l.gridCols})),
    firstCompleteLod0Ms:paints.find(e=>e.complete&&e.lod===0)?.at-start,loopRates,paints:{count:paints.length,complete:paints.filter(e=>e.complete).length,partial:paints.filter(e=>e.partial).length,coarse:paints.filter(e=>e.lod!==0).length,levels:[...new Set(paints.map(e=>e.lod))],cells:[...new Set(paints.map(e=>e.cells))]}};
  },{name,args,capMs:Number(process.env.CAP_MS??45000)}),new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error(`phase ${name} exceeded outer deadline`)),Number(process.env.CAP_MS??45000)+20000);timer.unref();})]);
  const requests=counter.entries().slice(index), byArray={};
  for(const r of requests){const a=/\/(\d+)\/(data|mask|coverage)\//.exec(r.url);const k=a?`${a[1]}/${a[2]}`:'metadata';const bucket=byArray[k]??={requests:0,bytes:0,paths:{}};bucket.requests++;bucket.bytes+=r.wire??r.body;bucket.paths[new URL(r.url).pathname]=(bucket.paths[new URL(r.url).pathname]??0)+1;}
  for(const b of Object.values(byArray)){b.uniquePaths=Object.keys(b.paths).length;b.duplicateRequests=b.requests-b.uniquePaths;b.maxRequestsPerPath=Math.max(0,...Object.values(b.paths));delete b.paths;}
  row.phases.push({name,args,...measurement,network:subtract(counter.snapshot(),before),pageRequestsByArray:byArray});
  console.log(JSON.stringify({label,sensor,view,repeat,phase:name,elapsedMs:measurement.elapsedMs,finished:measurement.result?.finished,steps:measurement.result?.stats?.steps,lod:measurement.shownFrame?.lod,byArray}));
 };
 await phase('cold',{view});
 if(process.env.TOTAL_GIB) await phase('budget',{totalBytes:Number(process.env.TOTAL_GIB)*1024**3});
 await phase('prefetch',{});
 for(const fps of (process.env.FPS??'30,60').split(',').map(Number))await phase('play',{fps});
 await phase('switch',{});
 }catch(e){row.error=e.stack;console.error(e);}
 finally{fs.appendFileSync(out,JSON.stringify(row)+'\n');await browser.close();}
}
}finally{await server.close();}
