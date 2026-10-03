import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from '../../js/node_modules/playwright/index.mjs';
import { startStaticServer } from '../../js/support/static-server.js';

const root = path.resolve(import.meta.dirname, '../..');
const output = path.join(root, 'data/reports/notebook-ci');
const server = await startStaticServer(root, 8765);
const browser = await chromium.launch({args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
try {
  const page = await browser.newPage({viewport:{width:900,height:650}});
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.route('**/notebook-ci.html', r => r.fulfill({contentType:'text/html',body:'<!doctype html><html><body></body></html>'}));
  for (const backend of ['leafmap','geemap','player']) {
    await page.goto(`${server.url}/notebook-ci.html`);
    await page.evaluate(async backend => {
      const values = await (await fetch(`/data/reports/notebook-ci/${backend}.json`)).json();
      const listeners = new Map();
      const model = {
        get: k => values[k],
        set(k,v) { if (values[k]===v) return; values[k]=v; for(const callback of listeners.get(`change:${k}`)??[]) callback(); },
        save_changes() {},
        on(k,cb) {if(!listeners.has(k))listeners.set(k,new Set());listeners.get(k).add(cb);},
        off(k,cb) {listeners.get(k)?.delete(cb);},
      };
      window.model=model;window.values=values;
      window.countListeners=()=>[...listeners.values()].reduce((sum,set)=>sum+set.size,0);
      const widget=(await import(`/data/reports/notebook-ci/${backend}.js`)).default;
      window.widget=widget;
      window.dispose=await widget.render({model,el:document.body});
    },backend);
    if (backend === 'player') {
      await page.waitForFunction(()=>window.values.ready && window.values.state.range?.[0]===-128);
      assert.equal(await page.locator('[aria-label=Band]').isVisible(),false);
      const frame=page.frames().find(f=>f!==page.mainFrame());
      await frame.waitForFunction(()=>window.chronozarr?.viewer.paintedT===0 && window.chronozarr.viewer.renderNow().complete);
      const first=await frame.locator('#gl-canvas').screenshot();
      await page.evaluate(()=>window.model.set('t',1));
      await frame.waitForFunction(()=>window.chronozarr.viewer.paintedT===1 && window.chronozarr.viewer.renderNow().complete);
      const second=await frame.locator('#gl-canvas').screenshot();
      assert.notDeepEqual(first,second,'Player must paint different raster values at the second date');
      assert.equal(await page.evaluate(()=>window.values.bands[0].units),'m');
      await page.evaluate(()=>window.model.set('controls',true));
      assert.equal(await page.locator('[aria-label=Band]').isVisible(),true);
      await page.evaluate(()=>window.dispose());
      assert.equal(await page.evaluate(()=>window.countListeners()),0);
      assert.equal(await page.locator('iframe').count(),0);
    } else {
      const slider=page.locator(`[aria-label="${backend} fixture timestep"]`);
      await page.waitForFunction(label=>document.querySelector(`[aria-label="${label}"]`)?.disabled===false,`${backend} fixture timestep`);
      assert.equal(await slider.getAttribute('max'),'2');
      await page.waitForFunction(()=>document.querySelector('canvas')?.width>0);
      const painted = async () => {
        const png = await page.locator('canvas').screenshot();
        const colors = await page.evaluate(async bytes => {
          const bitmap = await createImageBitmap(new Blob([new Uint8Array(bytes)], {type:'image/png'}));
          const canvas = document.createElement('canvas');
          canvas.width=bitmap.width;canvas.height=bitmap.height;
          const ctx=canvas.getContext('2d');ctx.drawImage(bitmap,0,0);bitmap.close();
          const data=ctx.getImageData(0,0,canvas.width,canvas.height).data;
          const shades=new Set();
          for(let i=0;i<data.length;i+=4) if(data[i]===data[i+1]&&data[i]===data[i+2]) shades.add(data[i]);
          return shades.size;
        }, [...png]);
        return {png,colors};
      };
      const deadline=Date.now()+15000;
      let first;
      do { first=await painted(); } while(first.colors<20 && Date.now()<deadline);
      assert.ok(first.colors>=20,`${backend} must paint a gradient, not an empty canvas`);
      await slider.fill('1');await slider.dispatchEvent('input');
      await page.waitForFunction(()=>document.querySelector('label span')?.textContent.includes('2024-02-01'));
      await page.waitForFunction(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true;});
      await page.screenshot({path:path.join(output,`${backend}.png`)});
      let second;
      const switchDeadline=Date.now()+15000;
      do { second=await painted(); } while((second.colors<20 || first.png.equals(second.png)) && Date.now()<switchDeadline);
      assert.ok(second.colors>=20);
      assert.notDeepEqual(first.png,second.png,`${backend} must repaint the raster after changing date`);
      await page.evaluate(()=>window.dispose?.());
      assert.equal(await page.locator('canvas').count(),0);
      assert.equal(await page.evaluate(()=>window.countListeners()),0);
      // Disposal before MapLibre's load event must also release the eventual map.
      await page.evaluate(async()=>{
        const host=document.createElement('div');document.body.append(host);
        const dispose=await window.widget.render({model:window.model,el:host});
        dispose();dispose();
      });
      await page.waitForFunction(()=>document.querySelectorAll('canvas').length===0);
      assert.equal(await page.evaluate(()=>window.countListeners()),0);
    }
    assert.deepEqual(errors,[]);
    console.log(`${backend}: installed-wheel renderer, synthetic float raster, time switch and cleanup passed`);
  }
  await fs.writeFile(path.join(output,'browser-result.json'),JSON.stringify({backends:['leafmap','geemap','player'],passed:true},null,2));
} finally {await browser.close();await server.close();}
