// The marks of a first visit (first-visit.mjs) from what the page recorded and what CDP logged: pure, so that a test can read it.

import { median } from './compare.js';

const round = (ms) => (ms === null ? null : Math.round(ms * 10) / 10);

/**
 * The marks from the page's own record and from CDP's log of its requests.
 * @param {{ shaderMs: number, programs: number, workers: {spawnedAt: number, firstMessageAt: number|null}[], firstFrameMs: number|null, targetFrameMs: number|null }} page
 * @param {{ url: string, type: string, startedAt: number, endedAt: number|null }[]} requests  with CDP's times in seconds
 * @param {string} storeOrigin  where the chunks are
 */
export function visitMarks(page, requests, storeOrigin) {
  const navigation = requests.find((request) => request.type === 'Document');
  const chunks = requests.filter((request) => request.url.startsWith(storeOrigin) && /\/c\/\d+\//.test(request.url));
  const codecs = requests.filter((request) => /\/codecs\/[\w-]+\.js$/.test(new URL(request.url).pathname));
  const finished = codecs.filter((request) => request.endedAt !== null);
  const readyWorkers = page.workers.filter((worker) => worker.firstMessageAt !== null);
  return {
    firstRequestMs: navigation && chunks.length ? round((Math.min(...chunks.map((request) => request.startedAt)) - navigation.startedAt) * 1000) : null,
    shaderMs: page.programs ? round(page.shaderMs) : null,
    programs: page.programs,
    workerReadyMs: readyWorkers.length ? round(median(readyWorkers.map((worker) => worker.firstMessageAt - worker.spawnedAt))) : null,
    workers: page.workers.length,
    codecMs: finished.length ? round((Math.min(...finished.map((request) => request.endedAt)) - Math.min(...codecs.map((request) => request.startedAt))) * 1000) : null,
    firstFrameMs: round(page.firstFrameMs),
    targetFrameMs: round(page.targetFrameMs),
  };
}
