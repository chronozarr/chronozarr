import * as React from 'react'
import { defineConfig } from 'vocs'
import legacyAnchors from './legacy-anchors.json'

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
  description:
    'Turn a georeferenced raster time series into an interactive dataset that anyone can open with a link, with the stored values intact.',
  iconUrl: '/favicon.svg',
  // No "Ask in ChatGPT" button on the pages: it sends readers to a third-party service.
  aiCta: false,
  // A function, not a bare element: Vocs 1.4.1 reads a bare element as a path-to-element map
  // (`typeof === 'object'`), finds no key matching the URL, and renders nothing.
  // Link previews: Vocs writes og:title and og:description per page; the image and the page address come from here.
  // The address ends in a slash because the host redirects /page to /page/. The image is made by
  // scripts/make-previews.py; JPEG because X does not reliably accept WebP.
  head: ({ path }) => (
    <>
      <meta property="og:url" content={`https://chronozarr.org${path.replace(/(\/index)?(\.html)?\/?$/, '')}/`} />
      <meta property="og:image" content="https://chronozarr.org/og.jpg" />
      <meta property="og:image:width" content="1200" />
      <meta property="og:image:height" content="630" />
      <meta property="og:image:alt" content="False-color Sentinel-2 image of meanders of the Ucayali River, Peru, August 2025" />
      <meta name="twitter:image" content="https://chronozarr.org/og.jpg" />
      <meta name="twitter:image:alt" content="False-color Sentinel-2 image of meanders of the Ucayali River, Peru, August 2025" />
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link
        rel="stylesheet"
        href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,300..800&family=IBM+Plex+Mono:wght@400;500&display=swap"
      />
      {/* Old section anchors: a fragment never reaches the server, so the Worker's path redirects cannot
          move it. This sends /page#old-anchor to the section's new address. Map: legacy-anchors.json. Only the
          map's own keys count (not #constructor), and a malformed fragment is left alone. */}
      <script
        dangerouslySetInnerHTML={{
          __html: `(function(){var m=${JSON.stringify(legacyAnchors)};var own=function(o,k){return o&&Object.prototype.hasOwnProperty.call(o,k)?o[k]:null};function go(){try{var t=own(own(m,location.pathname.replace(/\\/$/,'')),decodeURIComponent(location.hash.slice(1)));if(typeof t==='string')location.replace(t)}catch(e){}}go();addEventListener('hashchange',go)})()`,
        }}
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
    { text: 'Quickstart', link: '/quickstart' },
    { text: 'Docs', link: '/what-is-chronozarr' },
    { text: 'Specification', link: '/specification' },
    // Absolute so Vocs does not route it client-side: /demo/ is a static page, not a Vocs route.
    { text: 'Demo', link: 'https://chronozarr.org/demo/' },
  ],
  socials: [{ icon: 'github', link: 'https://github.com/chronozarr/chronozarr' }],
  sidebar: [
    { text: 'Start here', items: [
      { text: 'What is chronozarr?', link: '/what-is-chronozarr' },
      { text: 'Quickstart', link: '/quickstart' },
      { text: 'Develop from source', link: '/guides/development' },
      { text: 'Explore the viewer', link: '/viewer' },
    ] },
    { text: 'Create and share', items: [
      { text: 'Convert your data', link: '/guides/convert' },
      { text: 'Preview on your computer', link: '/guides/preview' },
      { text: 'Share a temporary link', link: '/guides/share' },
      { text: 'Publish a permanent link', link: '/guides/publish' },
      { text: 'Analyze in Python', link: '/guides/analyze' },
    ] },
    { text: 'Guides and examples', items: [
      { text: 'Sentinel-2 for your area', link: '/guides/sentinel-2' },
      { text: 'Your own data, end to end', link: '/guides/bring-your-data' },
      { text: 'SWOT, NISAR and other examples', link: '/guides/examples' },
      { text: 'PNG frames', link: '/guides/png-frames' },
      { text: 'Append timesteps', link: '/guides/append' },
      { text: 'Embed the viewer', link: '/guides/embedding' },
      { text: 'Notebooks and remote machines', link: '/guides/notebooks' },
      { text: 'Private stores', link: '/guides/private' },
      { text: 'Advanced hosting', collapsed: true, items: [
        { text: 'Overview', link: '/guides/hosting' },
        { text: 'Hosting recipes', link: '/guides/hosting-providers' },
        { text: 'Requirements and checks', link: '/guides/hosting-requirements' },
        { text: 'Self-host the viewer', link: '/guides/viewer-distribution' },
      ] },
    ] },
    { text: 'Reference', items: [
      { text: 'Command line', link: '/reference/cli' },
      { text: 'Python API', link: '/reference/python' },
      { text: 'JavaScript reader', link: '/reference/javascript' },
      { text: 'MapLibre layer', link: '/reference/maplibre' },
      { text: 'Specification v0.3 (draft)', link: '/specification' },
      { text: 'How it works', link: '/how-it-works' },
      { text: 'Performance and evidence', link: '/performance' },
      { text: 'Measurement log', link: '/reference/evidence' },
      { text: '0.5.1 release notes', link: '/reference/release-0.5.1' },
      { text: 'Troubleshooting', link: '/troubleshooting' },
      { text: 'Format comparison', link: '/guides/format-comparison' },
    ] },
  ],
})
