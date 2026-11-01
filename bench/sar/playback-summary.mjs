import fs from 'node:fs';
const paths=process.argv.slice(2);
const rows=paths.flatMap(p=>fs.readFileSync(p,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse)).filter(r=>r.result?.play);
const distribution=a=>{a=[...a].sort((a,b)=>a-b);return {n:a.length,min:a[0],median:a[Math.floor(a.length/2)],p95:a[Math.min(a.length-1,Math.floor(a.length*.95))],max:a.at(-1)}};
const output={};
for(const sensor of new Set(rows.map(r=>r.sensor))){const rs=rows.filter(r=>r.sensor===sensor);output[sensor]={runs:rs.length,coldMetadataMs:distribution(rs.flatMap(r=>r.result.coldOpenConsolidated?.map(x=>x.openMs)??[])),coldFullLod0Ms:distribution(rs.flatMap(r=>r.result.coldOpenConsolidated?.map(x=>x.firstPaintMs)??[])),warmLod0Switch:rs.map(r=>r.result.warmSwitchStepping),decode:rs.map(r=>r.result.decodePerChunk),play:rs.map(r=>r.result.play.map(p=>({...p,store:undefined,resources:undefined}))),gpu:rs[0].initial.gpu,canvas:rs[0].initial.canvas,levels:rs[0].initial.levels};}
console.log(JSON.stringify(output,null,2));
