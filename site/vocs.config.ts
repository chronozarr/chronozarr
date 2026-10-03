import { defineConfig } from 'vocs'

export default defineConfig({
  title: 'chronozarr',
  description: 'An open Zarr v3 layout for raster time series.',
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
      { text: 'How it works', link: '/how-it-works' },
      { text: 'Examples', link: '/examples' },
    ] },
    { text: 'Use chronozarr', items: [
      { text: 'Bring your own data', link: '/guides/bring-your-data' },
      { text: 'Publish a store', link: '/publishing' },
      { text: 'Hosting recipes', link: '/guides/hosting' },
      { text: 'Append timesteps', link: '/guides/append' },
      { text: 'Integrate', link: '/integrate' },
      { text: 'Embed the viewer', link: '/guides/embedding' },
      { text: 'Self-host the packaged viewer', link: '/guides/viewer-distribution' },
      { text: 'PNG frames', link: '/guides/png-frames' },
      { text: 'Water masks', link: '/guides/water-masks' },
    ] },
    { text: 'Reference', items: [
      { text: 'Specification v0.2 (draft)', link: '/specification' },
      { text: 'Python and xarray', link: '/reference/python' },
      { text: 'Command line', link: '/reference/cli' },
      { text: 'JavaScript reader', link: '/reference/javascript' },
      { text: 'MapLibre layer', link: '/reference/maplibre' },
      { text: 'Format comparison', link: '/guides/format-comparison' },
      { text: 'Measured comparisons', link: '/guides/comparisons' },
      { text: 'Matched rendered comparison', link: '/guides/rendered-comparison' },
    ] },
  ],
})
