import * as React from 'react'
import { defineConfig } from 'vocs'

// Visual direction: a 1960s to 70s corporate annual report. Grotesk type (Archivo stands in
// for Univers), cool neutrals, one steel-blue accent, hairline rules and no rounded corners.
// Every value below is a Vocs theme variable; `docs/styles.css` carries what the variables
// cannot express.
const paper = { light: '#f4f5f7', dark: '#0b0d11' }
const paper2 = { light: '#eceef2', dark: '#12151b' }
const paper3 = { light: '#e4e7ec', dark: '#181c23' }
const ink = { light: '#0e1217', dark: '#e8eaee' }
const ink2 = { light: '#3f4651', dark: '#b4bac4' }
const ink3 = { light: '#646c78', dark: '#8b929d' }
const rule = { light: '#c9ced6', dark: '#2a2f38' }
const rule2 = { light: '#dde0e6', dark: '#1f242c' }
const steel = { light: '#1c5f8f', dark: '#8fc2ea' }
const steelHover = { light: '#174d74', dark: '#b3d7f3' }

export default defineConfig({
  title: 'chronozarr',
  description: 'chronozarr turns a raster time series into static files that you can put in a storage bucket.',
  iconUrl: '/favicon.svg',
  // A function, not a bare element: Vocs 1.4.1 reads a bare element as a path-to-element map
  // (`typeof === 'object'`), finds no key matching the URL, and renders nothing.
  head: () => (
    <>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link
        rel="stylesheet"
        href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,300..800&family=IBM+Plex+Mono:wght@400;500&display=swap"
      />
    </>
  ),
  theme: {
    accentColor: {
      backgroundAccent: steel,
      backgroundAccentHover: steelHover,
      backgroundAccentText: { light: '#ffffff', dark: '#0b0d11' },
      borderAccent: steel,
      textAccent: steel,
      textAccentHover: steelHover,
    },
    variables: {
      fontFamily: {
        default: '"Archivo", "Helvetica Neue", Helvetica, Arial, sans-serif',
        mono: '"IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace',
      },
      borderRadius: { '0': '0', '2': '0', '3': '0', '4': '0', '6': '0', '8': '0' },
      color: {
        background: paper,
        background2: paper2,
        background3: paper2,
        background4: paper3,
        background5: paper3,
        text: ink,
        text2: ink2,
        text3: ink3,
        text4: ink3,
        textSecondary: ink2,
        heading: ink,
        title: ink,
        border: rule,
        border2: rule2,
        hr: rule,
        link: steel,
        linkHover: steelHover,
        codeBlockBackground: paper2,
        codeInlineBackground: paper2,
        codeInlineBorder: rule2,
        codeInlineText: ink,
        codeTitleBackground: paper3,
        tableBorder: rule,
        tableHeaderBackground: paper,
        tableHeaderText: ink3,
        blockquoteBorder: rule,
        blockquoteText: ink2,
      },
    },
  },
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
