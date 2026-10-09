# chronozarr in GeoLibre

`js/geolibre/plugin.js` exports `createChronozarrPlugin`, an adapter for the GeoLibre plugin API. The plugin adds a chronozarr layer and a time slider to the MapLibre map of the host app. This directory holds a browser check of that adapter.

## Run it

The check loads the NISAR store of the [NISAR example](../nisar/README.md). That store needs source files that are not in this repository. The check also needs Node.js and `npm ci --prefix js`.

1. Build the NISAR store as the [NISAR example](../nisar/README.md) describes.
2. Run the check from the repository root.

   ```sh
   node examples/geolibre/browser_check.mjs
   ```

The check passes a mock host object to the plugin on a real MapLibre map. It checks the layer, the host registration, the opacity bridge, the timestep and the cleanup after `deactivate`.

To use the plugin, create it and pass the host app to `activate`.

```js
import { createChronozarrPlugin } from './js/geolibre/plugin.js';

const plugin = createChronozarrPlugin({ url, product: 'band', range: [-25, 0], name: 'NISAR' });
plugin.activate(app);
```

The check does not run GeoLibre itself.
