// CRS identity shared by metadata discovery and map adapters. Definitions outrank codes.
const stable = (value) => JSON.stringify(value, (_, v) => v && !Array.isArray(v) && typeof v === 'object'
  ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b))) : v);
const squashWkt = (text) => text.replace(/("(?:[^"]|"")*")|\s+/g, (_, quoted) => quoted ?? '');

export function identifyCrs({ crs = null, wkt2 = null, projjson = null }) {
  if (wkt2 !== null) {
    try {
      const id = wktTree(wkt2).args.find((arg) => ['ID', 'AUTHORITY'].includes(arg?.type));
      return id ? `${id.args[0]}:${id.args[1]}` : null;
    } catch { return null; }
  }
  if (projjson !== null) return projjson.id ? `${projjson.id.authority}:${projjson.id.code}` : null;
  return crs;
}

export const loadProj4 = async () => (await import('../vendor/proj4/proj4.js')).default;

// EPSG:4326 uses latitude,longitude axis order and the WGS84 ensemble. proj4's shorthand
// defaults to longitude,latitude and a legacy WGS84 datum, so it cannot stand in for this definition.
const WGS84_GEOGRAPHIC = {
  type: 'GeographicCRS', name: 'WGS 84',
  datum_ensemble: { name: 'World Geodetic System 1984 ensemble',
    members: ['Transit', 'G730', 'G873', 'G1150', 'G1674', 'G1762', 'G2139', 'G2296'].map((name) => ({ name: `World Geodetic System 1984 (${name})` })),
    ellipsoid: { name: 'WGS 84', semi_major_axis: 6378137, inverse_flattening: 298.257223563 }, accuracy: '2.0' },
  coordinate_system: { subtype: 'ellipsoidal', axis: [
    { name: 'Geodetic latitude', abbreviation: 'Lat', direction: 'north', unit: 'degree' },
    { name: 'Geodetic longitude', abbreviation: 'Lon', direction: 'east', unit: 'degree' },
  ] },
};

function projectedWgs84(method, parameters) {
  return { type: 'ProjectedCRS', name: 'WGS 84 projected grid', base_crs: WGS84_GEOGRAPHIC,
    conversion: { name: method, method: { name: method }, parameters: parameters.map(([name, value, unit]) => ({ name, value, unit })) },
    coordinate_system: { subtype: 'Cartesian', axis: [
      { name: 'Easting', abbreviation: 'E', direction: 'east', unit: 'metre' },
      { name: 'Northing', abbreviation: 'N', direction: 'north', unit: 'metre' },
    ] } };
}

// Pseudo-Mercator uses the ellipsoid's semimajor radius as a sphere. proj4 normally gets
// this from a root EPSG shortcut; express the METHOD's meaning explicitly so stale IDs stay harmless.
function preserveMethod(definition) {
  const method = typeof definition === 'string' ? /METHOD\[\s*"([^"]+)"/.exec(definition)?.[1] : definition.conversion?.method?.name;
  if (String(method).toLowerCase().replace(/[^a-z]/g, '') !== 'popularvisualisationpseudomercator') return definition;
  if (typeof definition === 'string') return definition.replace(/(ELLIPSOID\[\s*"[^"]*"\s*,\s*[^,]+,\s*)[^,\]]+/, (_, prefix) => `${prefix}0`);
  const copy = structuredClone(definition);
  const ellipsoid = (copy.base_crs.datum ?? copy.base_crs.datum_ensemble).ellipsoid;
  ellipsoid.inverse_flattening = 0;
  if (ellipsoid.semi_minor_axis !== undefined) ellipsoid.semi_minor_axis = ellipsoid.semi_major_axis;
  return copy;
}

// Remove root authority nodes without touching nested datum/method identifiers. WKT2 IDs can include
// versions, citations and nested URI nodes, so neither a two-field regex nor a root EPSG shortcut is safe.
function withoutRootAuthority(wkt) {
  const text = wkt.trim(), cuts = [];
  let depth = 0, quoted = false, start = null;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '"') {
      if (quoted && text[i + 1] === '"') { i++; continue; }
      quoted = !quoted;
    }
    if (quoted) continue;
    if (text[i] === '[') {
      if (depth === 1) {
        let at = i - 1;
        while (/\s/.test(text[at])) at--;
        const end = at + 1;
        while (at >= 0 && /[A-Za-z_]/.test(text[at])) at--;
        if (['ID', 'AUTHORITY'].includes(text.slice(at + 1, end).toUpperCase())) {
          while (at >= 0 && /\s/.test(text[at])) at--;
          start = text[at] === ',' ? at : at + 1;
        }
      }
      depth++;
    } else if (text[i] === ']') {
      depth--;
      if (depth === 1 && start !== null) { cuts.push([start, i + 1]); start = null; }
    }
  }
  return cuts.reverse().reduce((value, [from, to]) => value.slice(0, from) + value.slice(to), text);
}

function declaredDefinition(source) {
  // An authority annotation can be stale. Let the parser read the actual parameters instead of
  // proj4's special EPSG:3857 shortcut, which replaces an annotated definition with its own.
  if (source.wkt2 != null) return withoutRootAuthority(source.wkt2);
  if (source.projjson != null) {
    const { id, ...parameters } = source.projjson;
    return parameters;
  }
  if (source.crs === 'EPSG:4326') return WGS84_GEOGRAPHIC;
  const utm = /^EPSG:32([67])(\d{2})$/.exec(source.crs);
  if (utm && +utm[2] >= 1 && +utm[2] <= 60) {
    return projectedWgs84('Transverse Mercator', [
      ['Latitude of natural origin', 0, 'degree'], ['Longitude of natural origin', +utm[2] * 6 - 183, 'degree'],
      ['Scale factor at natural origin', 0.9996, 'unity'], ['False easting', 500000, 'metre'], ['False northing', utm[1] === '7' ? 10000000 : 0, 'metre'],
    ]);
  }
  if (source.crs === 'EPSG:3857') return projectedWgs84('Popular Visualisation Pseudo Mercator', [
    ['Latitude of natural origin', 0, 'degree'], ['Longitude of natural origin', 0, 'degree'],
    ['False easting', 0, 'metre'], ['False northing', 0, 'metre'],
  ]);
  return source.crs;
}

export const projectionDefinition = (source) => preserveMethod(declaredDefinition(source));

// proj4 flattens coordinate-system units to one value. Retain each declared axis separately so
// a mismatched second axis or ORDER clause cannot disappear in that flattening.
function wktTree(text) {
  const tokens = text.match(/"(?:[^"]|"")*"|[A-Za-z_][A-Za-z_0-9]*|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[\[\],]/g);
  let i = 0;
  const value = () => {
    const token = tokens[i++];
    if (tokens[i] !== '[') return token.startsWith('"') ? token.slice(1, -1).replaceAll('""', '"') : Number.isFinite(Number(token)) ? Number(token) : token;
    i++;
    const args = [];
    while (tokens[i] !== ']') { args.push(value()); if (tokens[i] === ',') i++; }
    i++;
    return { type: token.toUpperCase(), args };
  };
  return value();
}

function unitScale(unit) {
  if (unit && typeof unit === 'object') return Number(unit.conversion_factor);
  const name = String(unit).toLowerCase().replace(/[ _-]/g, '');
  return ({ degree: Math.PI / 180, degrees: Math.PI / 180, radian: 1, radians: 1, grad: Math.PI / 200, gon: Math.PI / 200,
    metre: 1, meter: 1, m: 1, kilometre: 1000, kilometer: 1000, foot: 0.3048, feet: 0.3048, ft: 0.3048, ussurveyfoot: 1200 / 3937, unity: 1 })[name] ?? name;
}

function declaredAxes(definition) {
  if (typeof definition !== 'string') return definition.coordinate_system?.axis?.map((axis, i) => ({
    direction: axis.direction.toLowerCase(), order: i + 1, unit: unitScale(axis.unit),
    meridian: axis.meridian ? [axis.meridian.longitude, unitScale(axis.meridian.unit)] : null,
  })) ?? null;
  if (!definition.includes('[')) return null;
  const root = wktTree(definition);
  const node = (args, types) => args.find((arg) => types.includes(arg?.type));
  const fallbackUnit = node(root.args, ['UNIT', 'LENGTHUNIT', 'ANGLEUNIT']);
  return root.args.filter((arg) => arg?.type === 'AXIS').map(({ args }, i) => {
    const unit = node(args, ['UNIT', 'LENGTHUNIT', 'ANGLEUNIT']) ?? fallbackUnit;
    const meridian = node(args, ['MERIDIAN']);
    const meridianUnit = meridian && node(meridian.args, ['ANGLEUNIT']);
    return { direction: args[1].toLowerCase(), order: node(args, ['ORDER'])?.args[0] ?? i + 1,
      unit: unit ? Number(unit.args[1]) : null, meridian: meridian ? [meridian.args[0], meridianUnit ? Number(meridianUnit.args[1]) : null] : null };
  });
}

// Compare resolved projection parameters, not display names, authority annotations or object key order.
function signature(proj, definition) {
  const keys = ['a', 'b', 'es', 'e', 'ep2', 'lat0', 'lat1', 'lat2', 'lat_ts', 'long0', 'longc', 'alpha', 'gamma', 'x0', 'y0', 'k0', 'to_meter', 'from_greenwich', 'zone', 'm', 'n'];
  const parameters = Object.fromEntries(keys.map((key) => [key, proj[key] ?? (['k0', 'to_meter'].includes(key) ? 1 : 0)]));
  if (proj.projName === 'longlat') parameters.to_meter = proj.to_meter ?? ({ degree: Math.PI / 180, degrees: Math.PI / 180, radian: 1, radians: 1, grad: Math.PI / 200, gon: Math.PI / 200 }[proj.units] ?? null);
  const datumObject = typeof definition === 'object' ? (definition.datum ?? definition.datum_ensemble ?? definition.base_crs?.datum ?? definition.base_crs?.datum_ensemble) : null;
  const datumName = typeof definition === 'string' ? /(?:DATUM|ENSEMBLE)\[\s*"([^"]+)"/.exec(definition)?.[1] : datumObject?.name;
  const datumCode = (datumName ?? proj.datumCode ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  // Preserve geodetic semantics even where a projection method (pseudo-Mercator) computes on a sphere.
  const wktEllipsoid = typeof definition === 'string' ? /(?:ELLIPSOID|SPHEROID)\[\s*"[^"]*"\s*,\s*([^,\]]+)\s*,\s*([^,\]]+)/.exec(definition) : null;
  const ellipsoid = datumObject?.ellipsoid;
  const semiMajor = wktEllipsoid ? Number(wktEllipsoid[1]) : ellipsoid?.semi_major_axis ?? ellipsoid?.radius;
  const inverseFlattening = wktEllipsoid ? Number(wktEllipsoid[2]) : ellipsoid?.inverse_flattening;
  const semiMinor = inverseFlattening === 0 ? semiMajor : inverseFlattening ? semiMajor * (1 - 1 / inverseFlattening) : ellipsoid?.semi_minor_axis ?? semiMajor;
  const members = typeof definition === 'string' ? [...definition.matchAll(/MEMBER\[\s*"([^"]+)"/g)].map((match) => match[1]) : datumObject?.members?.map(({ name }) => name) ?? [];
  const datum = {
    code: ['epsg4326', 'wgs84', 'wgs1984', 'worldgeodeticsystem1984'].includes(datumCode) ? 'wgs84' : datumCode,
    parameters: (proj.datum?.datum_params ?? []).some((n) => +n !== 0) ? proj.datum.datum_params.map(Number) : [],
    grids: proj.datum?.grids ?? [],
    ellipsoid: [semiMajor ?? proj.a, semiMinor ?? proj.b],
    members: members.map((name) => name.toLowerCase().replace(/[^a-z0-9]/g, '')).sort(),
    epoch: typeof definition === 'string' ? Number(/FRAMEEPOCH\[\s*([^\]]+)/.exec(definition)?.[1] ?? 0) : datumObject?.frame_reference_epoch ?? 0,
  };
  return { projection: proj.names?.[0] ?? proj.projName, parameters, datum, axis: proj.axis, declaredAxes: declaredAxes(definition),
    angularUnits: proj.projName === 'longlat' ? String(proj.units ?? 'degrees').replace(/^degrees?$/, 'degree') : null,
    over: !!proj.over, approx: !!proj.approx, utmSouth: !!proj.utmSouth };
}

function equivalent(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) <= 1e-11 * Math.max(1, Math.abs(a), Math.abs(b));
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].every((key) => equivalent(a[key], b[key]));
  }
  return a === b;
}

/** True only when the highest-precedence declarations agree. Never let equal codes hide different WKT. */
export async function sameCrs(a, b) {
  const left = declaredDefinition(a), right = declaredDefinition(b);
  if (typeof left === 'string' && typeof right === 'string' && squashWkt(left) === squashWkt(right)) return true;
  if (left && right && typeof left === 'object' && typeof right === 'object' && stable(left) === stable(right)) return true;
  try {
    const proj4 = await loadProj4();
    return equivalent(signature(proj4(preserveMethod(left)).oProj, left), signature(proj4(preserveMethod(right)).oProj, right));
  } catch {
    return false;
  }
}
