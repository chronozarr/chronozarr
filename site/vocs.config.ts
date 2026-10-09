import { defineConfig } from 'vocs'

export default defineConfig({
  title: 'chronozarr',
  description: 'chronozarr turns a raster time series into static files that you can put in a storage bucket.',
  iconUrl: '/favicon.svg',
  theme: { accentColor: { light: '#1767a6', dark: '#7dc3f5' } },
  topNav: [
    { text: 'Docs', link: '/getting-started' },
    { text: 'Specification', link: '/specification' },
    { text: 'Demo', link: '/demo/' },
  ],
  socials: [{ icon: 'github', link: 'https://github.com/chronozarr/chronozarr' }],
  sidebar: [
    { text: 'Start here', items: [
      { text: 'Getting started', link: '/getting-started' },
      { text: 'How it works', link: '/getting-started#how-it-works' },
    ] },
    { text: 'Use chronozarr', items: [
      { text: 'Bring your own data', link: '/guides/bring-your-data' },
      { text: 'Hosting recipes', link: '/guides/hosting' },
      { text: 'Append timesteps', link: '/guides/append' },
      { text: 'Embed the viewer', link: '/guides/embedding' },
      { text: 'Self-host the packaged viewer', link: '/guides/viewer-distribution' },
      { text: 'PNG frames', link: '/guides/png-frames' },
    ] },
    { text: 'Reference', items: [
      { text: 'Specification v0.3 (draft)', link: '/specification' },
      { text: 'Python API', link: '/reference/python' },
      { text: 'Command line', link: '/getting-started#commands' },
      { text: 'JavaScript reader', link: '/reference/javascript' },
      { text: 'MapLibre layer', link: '/reference/maplibre' },
      { text: 'Format comparison', link: '/guides/format-comparison' },
    ] },
  ],
})
