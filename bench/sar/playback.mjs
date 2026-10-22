// Reproduce: node bench/sar/playback.mjs [repeats=3] [natural|50Mbit-40ms]
// Browser cache is disabled. Local filesystem cache is not flushed; this is not a cold CDN test.
import fs from 'node:fs';
import os from 'node:os';
import crypto from 'node:crypto';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
const hash=p=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const provenance={head:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),files:Object.fromEntries(['js/demo/bench.js','js/demo/playback.js','js/chronozarr/decoder.js'].map(p=>[p,hash(p)]))};
import {startServer, launchBrowser, newPage, attachNet, applyProfile, PROFILES} from '../lib/harness.mjs';
const repeats = Number(process.argv[2] ?? 3);
const profile = process.argv[3] ?? 'natural';
const out = process.env.OUT ?? `bench/sar/results/playback-${profile}.jsonl`;
const stores = {sentinel1:'ucayali_santa_maria/s1-rtc-monthly-v1',nisar:'colorado_yuma/nisar-gcov-v2'};
fs.mkdirSync(new URL('./results/',import.meta.url),{recursive:true});
fs.mkdirSync(path.dirname(path.resolve(out)),{recursive:true});
const server = await startServer();
try {
 for (const [sensor, store] of Object.entries(stores)) for(let repeat=0; repeat<repeats; repeat++) {
  const browser = await launchBrowser({channel:'chromium'});
  try {
   const {context,page} = await newPage(browser,{width:1440,height:900,deviceScaleFactor:2,blockCatalog:true});
   const storeUrl=`${server.url}/data/stores/${store}`;
   const {cdp,counter}=await attachNet(context,page,[storeUrl]);
   await applyProfile(cdp,PROFILES[profile]);
   await page.addInitScript(({fps,playOnly})=>{performance.setResourceTimingBufferSize(20000);window.__sarFps=fps;window.__sarPlayOnly=playOnly},{fps:Number(process.env.FPS ?? 30),playOnly:process.env.PLAY_ONLY==='1'});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.goto(`${server.url}/js/demo/index.html?store=${encodeURIComponent(storeUrl)}`);
   await page.waitForFunction(()=>window.chronozarr?.ready,null,{timeout:60000});
   const initial = await Promise.race([page.evaluate(async()=>{
    const open=await chronozarr.ready; const v=chronozarr.viewer;
    const gl=v.canvas.getContext('webgl2');const ext=gl.getExtension('WEBGL_debug_renderer_info');
    return {open,canvas:{width:v.canvas.width,height:v.canvas.height},gpu:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER),userAgent:navigator.userAgent,hardwareConcurrency:navigator.hardwareConcurrency,deviceMemory:navigator.deviceMemory,levels:v.store.levels.map(l=>({width:l.width,height:l.height,gridRows:l.gridRows,gridCols:l.gridCols,chunkBytes:l.chunkBytes})),timesteps:v.store.times.length};
   }),new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error('initial store readiness exceeded60seconds')),60000);timer.unref();})]);
   const networkInitial=counter.snapshot();
   const result=await Promise.race([page.evaluate(async()=>{
    const {runBenchmarks}=await import('/js/demo/bench.js');
    const result=window.__sarPlayOnly?{}:await runBenchmarks(chronozarr.viewer,{coldRuns:3,switches:20});
    const v=chronozarr.viewer;
    const diagnosticFrame=v.shownFrame;
    const play=[];
    for(const options of [{stepsPerSecond:Number(window.__sarFps ?? 30),loops:1,warm:'prefetch'},{stepsPerSecond:Number(window.__sarFps ?? 30),loops:1,warm:'prefetch',lod:2}]) {
      // Reload restores the fitted overview after the deliberately LOD0 stress test.
      await v.loadStore(v.store.url);
      if(window.__sarPlayOnly) {
       options.prefetchResult=await Promise.race([v.prefetchNow({movie:true}),new Promise((_,reject)=>setTimeout(()=>reject(new Error('prefetch exceeded15seconds')),15000))]);
       await new Promise(r=>setTimeout(r,1000));
       options.warm=null;
      }
      const start=performance.now();
      const measurement=await chronozarr.playBench(options);
      const switches=[];
      for(let i=0;i<10;i++) {
       const before=v.store.stats();const start=performance.now();
       v.goToTime((v.t+1)%v.store.times.length);const frame=v.renderNow();v.renderer.finish();
       const ms=performance.now()-start;const after=v.store.stats();
       switches.push({ms,complete:frame.complete,requests:after.network.requests-before.network.requests,bytes:after.network.bytes-before.network.bytes});
       await new Promise(r=>setTimeout(r,120));
      }
      measurement.isolatedWarmSwitches=switches;measurement.prefetchResult=options.prefetchResult??null;measurement.prefetchMethod=window.__sarPlayOnly?'explicit prefetch promise then1second idle; bypass unbounded quiet loop':'existing playBench settleNetwork';
      measurement.resources=performance.getEntriesByType('resource').filter(e=>e.startTime>=start&&e.name.includes('/data/stores/')).map(e=>({path:new URL(e.name).pathname,startMs:e.startTime-start,durationMs:e.duration,transferSize:e.transferSize}));
      play.push(measurement);
    }
    return {...result,diagnosticFrame,play,shownFrame:v.shownFrame,cacheAfter:v.store.cacheInfo(),heap:performance.memory?{usedJSHeapSize:performance.memory.usedJSHeapSize,totalJSHeapSize:performance.memory.totalJSHeapSize}:null};
   }),new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error('benchmark exceeded120seconds')),120000);timer.unref();})]);
   const row={sensor,repeat,profile,provenance:{...provenance,storeRootHash:hash(`data/stores/${store}/zarr.json`),harnessHash:hash('bench/sar/playback.mjs')},at:new Date().toISOString(),environment:{platform:os.platform(),release:os.release(),cpu:os.cpus()[0]?.model,totalMemory:os.totalmem(),load:os.loadavg()},initial,networkInitial,result,network:counter.snapshot(),requests:counter.entries(),errors};
   fs.appendFileSync(out,JSON.stringify(row)+'\n');
   console.log(JSON.stringify({sensor,repeat,profile,open:result.coldOpenConsolidated,warm:result.warmSwitchStepping,decode:result.decodePerChunk,errors}));
  }catch(error){fs.appendFileSync(out,JSON.stringify({sensor,repeat,profile,error:error.stack})+'\n');console.error(error);}
  finally{await browser.close();}
 }
}finally{await server.close();}
