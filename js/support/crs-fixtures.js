// CRSs that the built-in projections do not cover, with reference values from pyproj 3.8.0 (PROJ 9.8.1).
//
// Each entry holds the CRS as pyproj writes it (`wkt2`, WKT2:2019; `projjson` for the first only) and 7 points
// `[lon, lat, x, y]`: x and y are `Transformer.from_crs(4326, crs, always_xy=True).transform(lon, lat)` rounded to
// 10 micrometres. The two entries without a code also have a `grid`: a 200 x 200 pixel store with the affine
// `transform`, its outline (west, south, east, north in degrees, from 33 points along each edge, as footprintBounds
// samples it) and the lon/lat of the centre of four pixels `[col, row, lon, lat]`, all from the inverse transform.
// Recreate with
//   crs = CRS.from_proj4("<proj string>")  or  CRS.from_epsg(<code>)
//   crs.to_wkt("WKT2_2019"), json.loads(crs.to_json())
// where the proj strings are
//   Daymet LCC          +proj=lcc +lat_1=25 +lat_2=60 +lat_0=42.5 +lon_0=-100 +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs
//   MODIS sinusoidal    +proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs

/** Daymet v4 Lambert conformal conic, from its proj string; no authority code. */
export const DAYMET_LCC = {
  code: null,
  wkt2: `PROJCRS["unknown",BASEGEOGCRS["unknown",DATUM["Unknown based on WGS 84 ellipsoid",ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1],ID["EPSG",7030]]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8901]]],CONVERSION["unknown",METHOD["Lambert Conic Conformal (2SP)",ID["EPSG",9802]],PARAMETER["Latitude of false origin",42.5,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8821]],PARAMETER["Longitude of false origin",-100,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8822]],PARAMETER["Latitude of 1st standard parallel",25,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8823]],PARAMETER["Latitude of 2nd standard parallel",60,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8824]],PARAMETER["Easting at false origin",0,LENGTHUNIT["metre",1],ID["EPSG",8826]],PARAMETER["Northing at false origin",0,LENGTHUNIT["metre",1],ID["EPSG",8827]]],CS[Cartesian,2],AXIS["(E)",east,ORDER[1],LENGTHUNIT["metre",1,ID["EPSG",9001]]],AXIS["(N)",north,ORDER[2],LENGTHUNIT["metre",1,ID["EPSG",9001]]]]`,
  projjson: {"$schema":"https://proj.org/schemas/v0.7/projjson.schema.json","type":"ProjectedCRS","name":"unknown","base_crs":{"type":"GeographicCRS","name":"unknown","datum":{"type":"GeodeticReferenceFrame","name":"Unknown based on WGS 84 ellipsoid","ellipsoid":{"name":"WGS 84","semi_major_axis":6378137,"inverse_flattening":298.257223563,"id":{"authority":"EPSG","code":7030}}},"coordinate_system":{"subtype":"ellipsoidal","axis":[{"name":"Longitude","abbreviation":"lon","direction":"east","unit":"degree"},{"name":"Latitude","abbreviation":"lat","direction":"north","unit":"degree"}]}},"conversion":{"name":"unknown","method":{"name":"Lambert Conic Conformal (2SP)","id":{"authority":"EPSG","code":9802}},"parameters":[{"name":"Latitude of false origin","value":42.5,"unit":"degree","id":{"authority":"EPSG","code":8821}},{"name":"Longitude of false origin","value":-100,"unit":"degree","id":{"authority":"EPSG","code":8822}},{"name":"Latitude of 1st standard parallel","value":25,"unit":"degree","id":{"authority":"EPSG","code":8823}},{"name":"Latitude of 2nd standard parallel","value":60,"unit":"degree","id":{"authority":"EPSG","code":8824}},{"name":"Easting at false origin","value":0,"unit":"metre","id":{"authority":"EPSG","code":8826}},{"name":"Northing at false origin","value":0,"unit":"metre","id":{"authority":"EPSG","code":8827}}]},"coordinate_system":{"subtype":"Cartesian","axis":[{"name":"Easting","abbreviation":"E","direction":"east","unit":"metre"},{"name":"Northing","abbreviation":"N","direction":"north","unit":"metre"}]}},
  points: [
    [-100, 42.5, 0, 0],
    [-122.4194, 37.7749, -1869270.02075, -248433.39589],
    [-80.19, 25.76, 1961305.80946, -1564503.64104],
    [-67, 44.8, 2425744.37111, 729854.61039],
    [-124.5, 48.4, -1711653.25764, 879091.62771],
    [-105, 40, -407543.87997, -252704.58481],
    [-95.4, 29.8, 435142.43567, -1344524.63619],
  ],
  grid: {
    transform: [1000, 0, 1500000, 0, -1000, -300000],
    size: 200,
    outline: [-82.48445001534228, 35.885019627440435, -79.674805731367, 38.13781975875058],
    centres: [
      [3, 5, -81.96105426095198, 38.080205114770976],
      [100, 100, -81.09002580853883, 37.00843084319209],
      [150, 20, -80.30130918492075, 37.63253477856481],
      [199, 199, -80.23494538004768, 35.89067666915674],
    ],
  },
};

/** the MODIS sinusoidal grid on a sphere of radius 6371007.181 m, from its proj string; no authority code. */
export const MODIS_SINUSOIDAL = {
  code: null,
  wkt2: `PROJCRS["unknown",BASEGEOGCRS["unknown",DATUM["unknown",ELLIPSOID["unknown",6371007.181,0,LENGTHUNIT["metre",1,ID["EPSG",9001]]]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8901]]],CONVERSION["unknown",METHOD["Sinusoidal"],PARAMETER["Longitude of natural origin",0,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8802]],PARAMETER["False easting",0,LENGTHUNIT["metre",1],ID["EPSG",8806]],PARAMETER["False northing",0,LENGTHUNIT["metre",1],ID["EPSG",8807]]],CS[Cartesian,2],AXIS["(E)",east,ORDER[1],LENGTHUNIT["metre",1,ID["EPSG",9001]]],AXIS["(N)",north,ORDER[2],LENGTHUNIT["metre",1,ID["EPSG",9001]]]]`,
  points: [
    [0, 0, 0, 0],
    [-122.4194, 37.7749, -10759584.93425, 4200381.96891],
    [151.2093, -33.8688, 13960703.64465, -3766042.97639],
    [10, 60, 555975.25988, 6671703.1186],
    [-60, -10, -6570344.95699, -1111950.51977],
    [179.9, 5, 19927868.62985, 555975.25988],
    [77.2, 28.6, 7536832.39174, 3180178.48653],
  ],
  grid: {
    transform: [463.3127165279165, 0, -7783653.637667, 0, -463.3127165279165, 5559752.598333],
    size: 200,
    outline: [-108.90066786027634, 49.16666666224811, -105.78192878026825, 49.999999995506855],
    centres: [
      [3, 5, -108.8261150858377, 49.977083328842234],
      [100, 100, -107.31734782155151, 49.58124999554433],
      [150, 20, -107.73381187312592, 49.91458332884783],
      [199, 199, -105.78956616386613, 49.16874999558126],
    ],
  },
};

/** EPSG:32631, WGS 84 / UTM zone 31N, a CRS the built-in projections cover. */
export const WGS84_UTM_31N = {
  code: 'EPSG:32631',
  wkt2: `PROJCRS["WGS 84 / UTM zone 31N",BASEGEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],MEMBER["World Geodetic System 1984 (G2139)"],MEMBER["World Geodetic System 1984 (G2296)"],ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]],ENSEMBLEACCURACY[2.0]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],ID["EPSG",4326]],CONVERSION["UTM zone 31N",METHOD["Transverse Mercator",ID["EPSG",9807]],PARAMETER["Latitude of natural origin",0,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8801]],PARAMETER["Longitude of natural origin",3,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8802]],PARAMETER["Scale factor at natural origin",0.9996,SCALEUNIT["unity",1],ID["EPSG",8805]],PARAMETER["False easting",500000,LENGTHUNIT["metre",1],ID["EPSG",8806]],PARAMETER["False northing",0,LENGTHUNIT["metre",1],ID["EPSG",8807]]],CS[Cartesian,2],AXIS["(E)",east,ORDER[1],LENGTHUNIT["metre",1]],AXIS["(N)",north,ORDER[2],LENGTHUNIT["metre",1]],USAGE[SCOPE["Navigation and medium accuracy spatial referencing."],AREA["Between 0°E and 6°E, northern hemisphere between equator and 84°N, onshore and offshore. Algeria. Andorra. Belgium. Benin. Burkina Faso. Denmark - North Sea. France. Germany - North Sea. Ghana. Luxembourg. Mali. Netherlands. Niger. Nigeria. Norway. Spain. Togo. United Kingdom (UK) - North Sea."],BBOX[0,0,84,6]],ID["EPSG",32631]]`,
};

/** EPSG:3413, WGS 84 / NSIDC Sea Ice Polar Stereographic North. */
export const NSIDC_POLAR_STEREOGRAPHIC_NORTH = {
  code: 'EPSG:3413',
  wkt2: `PROJCRS["WGS 84 / NSIDC Sea Ice Polar Stereographic North",BASEGEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],MEMBER["World Geodetic System 1984 (G2139)"],MEMBER["World Geodetic System 1984 (G2296)"],ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]],ENSEMBLEACCURACY[2.0]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],ID["EPSG",4326]],CONVERSION["US NSIDC Sea Ice polar stereographic north",METHOD["Polar Stereographic (variant B)",ID["EPSG",9829]],PARAMETER["Latitude of standard parallel",70,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8832]],PARAMETER["Longitude of origin",-45,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8833]],PARAMETER["False easting",0,LENGTHUNIT["metre",1],ID["EPSG",8806]],PARAMETER["False northing",0,LENGTHUNIT["metre",1],ID["EPSG",8807]]],CS[Cartesian,2],AXIS["easting (X)",south,MERIDIAN[45,ANGLEUNIT["degree",0.0174532925199433]],ORDER[1],LENGTHUNIT["metre",1]],AXIS["northing (Y)",south,MERIDIAN[135,ANGLEUNIT["degree",0.0174532925199433]],ORDER[2],LENGTHUNIT["metre",1]],USAGE[SCOPE["Polar research."],AREA["Northern hemisphere - north of 60°N onshore and offshore, including Arctic."],BBOX[60,-180,90,180]],ID["EPSG",3413]]`,
  points: [
    [-45, 70, 0, -2187927.64928],
    [-50, 72.5, -166468.23426, -1902740.62434],
    [-30, 80, 281056.85443, -1048918.46054],
    [-150, 72, -1898469.89813, 508693.47606],
    [-20, 65, 1162304.88969, -2492570.87969],
    [100, 85, 310859.32876, 443953.13074],
    [-70, 78.5, -528185.8637, -1132698.24002],
  ],
};
