"""Well-known text of two projected CRSs that have no EPSG code.

Both come from pyproj 3.8 (`CRS.from_proj4(...).to_wkt("WKT2_2019")`), so tests do not need
pyproj to hold a CRS without a code: a Daymet-style Lambert conformal conic (standard parallels
25 and 60, origin 42.5 N 100 W) and the MODIS sinusoidal grid (sphere of radius 6371007.181 m).
"""

from __future__ import annotations

LCC_WKT2 = (
    'PROJCRS["unknown",BASEGEOGCRS["unknown",DATUM["Unknown based on WGS 84 ellipsoid",'
    'ELLIPSOID["WGS 84",6378137,298.257223563,'
    'LENGTHUNIT["metre",1],ID["EPSG",7030]]],PRIMEM["Greenwich",'
    '0,ANGLEUNIT["degree",0.0174532925199433],'
    'ID["EPSG",8901]]],CONVERSION["unknown",METHOD["Lambert Conic Conformal (2SP)",'
    'ID["EPSG",9802]],PARAMETER["Latitude of false origin",'
    '42.5,ANGLEUNIT["degree",0.0174532925199433],'
    'ID["EPSG",8821]],PARAMETER["Longitude of false origin",'
    '-100,ANGLEUNIT["degree",0.0174532925199433],'
    'ID["EPSG",8822]],PARAMETER["Latitude of 1st standard parallel",'
    '25,ANGLEUNIT["degree",0.0174532925199433],'
    'ID["EPSG",8823]],PARAMETER["Latitude of 2nd standard parallel",'
    '60,ANGLEUNIT["degree",0.0174532925199433],'
    'ID["EPSG",8824]],PARAMETER["Easting at false origin",'
    '0,LENGTHUNIT["metre",1],ID["EPSG",8826]],'
    'PARAMETER["Northing at false origin",0,LENGTHUNIT["metre",'
    '1],ID["EPSG",8827]]],CS[Cartesian,2],AXIS["(E)",'
    'east,ORDER[1],LENGTHUNIT["metre",1,ID["EPSG",'
    '9001]]],AXIS["(N)",north,ORDER[2],LENGTHUNIT["metre",'
    '1,ID["EPSG",9001]]]]'
)

SINUSOIDAL_WKT2 = (
    'PROJCRS["unknown",BASEGEOGCRS["unknown",DATUM["unknown",'
    'ELLIPSOID["unknown",6371007.181,0,LENGTHUNIT["metre",'
    '1,ID["EPSG",9001]]]],PRIMEM["Greenwich",0,'
    'ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",'
    '8901]]],CONVERSION["unknown",METHOD["Sinusoidal"],'
    'PARAMETER["Longitude of natural origin",0,'
    'ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",'
    '8802]],PARAMETER["False easting",0,LENGTHUNIT["metre",'
    '1],ID["EPSG",8806]],PARAMETER["False northing",'
    '0,LENGTHUNIT["metre",1],ID["EPSG",8807]]],'
    'CS[Cartesian,2],AXIS["(E)",east,ORDER[1],'
    'LENGTHUNIT["metre",1,ID["EPSG",9001]]],AXIS["(N)",'
    'north,ORDER[2],LENGTHUNIT["metre",1,ID["EPSG",'
    "9001]]]]"
)
