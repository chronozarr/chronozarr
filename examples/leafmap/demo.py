# Run these cells in a local Jupyter notebook after installing leafmap and chronozarr.
# %%
import leafmap.maplibregl as leafmap
from IPython.display import display

from chronozarr import add_chronozarr

# %%
m = leafmap.Map(style="positron", height="600px", add_sidebar=False, add_floating_sidebar=False)
add_chronozarr(m)
display(m)
