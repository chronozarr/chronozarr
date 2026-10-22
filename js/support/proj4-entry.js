// Entry of the proj4 build that js/vendor/proj4/proj4.js is bundled from (js/support/vendor.mjs).
// proj4's own entry registers every projection; this one registers the ones a store CRS is likely to use,
// so the vendored file stays small. proj4's core registers merc and longlat.
import proj4 from 'proj4/lib/core.js';
import Proj from 'proj4/lib/Proj.js';
import aea from 'proj4/lib/projections/aea.js';
import laea from 'proj4/lib/projections/laea.js';
import lcc from 'proj4/lib/projections/lcc.js';
import sinu from 'proj4/lib/projections/sinu.js';
import stere from 'proj4/lib/projections/stere.js';
import tmerc from 'proj4/lib/projections/tmerc.js';

for (const projection of [aea, laea, lcc, sinu, stere, tmerc]) Proj.projections.add(projection);

export default proj4;
