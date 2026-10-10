import { defineConfig } from 'vocs'

export default defineConfig({
  title: 'chronozarr',
  description: 'chronozarr turns a raster time series into static files that you can put in a storage bucket.',
  iconUrl: '/favicon.svg',
  theme: { accentColor: { light: '#1767a6', dark: '#7dc3f5' } },
  topNav: [
    { text: 'Docs', link: '/getting-started' },
    { text: 'Specification', link: '/specification' },
    // Absolute so Vocs does not route it client-side: /demo/ is a static page, not a Vocs route.
    { text: 'Demo', link: 'https://chronozarr.org/demo/' },
  ],
  socials: [{ icon: 'github', link: 'https://github.com/chronozarr/chronozarr' }],
  sidebar: [
    { text: 'Start here', items: [
      { text: 'Getting started', link: '/getting-started' },
      { text: 'Live demo', link: 'https://chronozarr.org/demo/' },
    ] },
    { text: 'Create and explore', items: [
      { text: 'Convert your data', link: '/guides/convert' },
      { text: 'Preview locally', link: '/guides/preview' },
      { text: 'Explore in a notebook', link: '/guides/notebooks' },
      { text: 'Bring your own data (full example)', link: '/guides/bring-your-data' },
      { text: 'PNG frames', link: '/guides/png-frames' },
    ] },
    { text: 'Share and publish', items: [
      { text: 'Share a local store instantly', link: '/guides/share' },
      { text: 'Hosting overview', link: '/guides/hosting' },
      { text: 'Publish a store', link: '/guides/publish' },
      { text: 'Hosting recipes', link: '/guides/hosting-providers' },
      { text: 'Hosting requirements and troubleshooting', link: '/guides/hosting-requirements' },
      { text: 'Append timesteps', link: '/guides/append' },
      { text: 'Embed the viewer', link: '/guides/embedding' },
      { text: 'Self-host the packaged viewer', link: '/guides/viewer-distribution' },
      { text: 'Private stores', link: '/guides/private' },
    ] },
    { text: 'Integrations', items: [
      { text: 'Python and xarray', link: '/reference/python' },
      { text: 'JavaScript reader', link: '/reference/javascript' },
      { text: 'MapLibre layer', link: '/reference/maplibre' },
    ] },
    { text: 'Reference and internals', items: [
      { text: 'Command line', link: '/reference/cli' },
      { text: 'How it works', link: '/how-it-works' },
      { text: 'Specification v0.3 (draft)', link: '/specification' },
      { text: 'Format comparison', link: '/guides/format-comparison' },
    ] },
  ],
})
