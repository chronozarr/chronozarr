"""Optional notebook player. Importing chronozarr does not require anywidget."""

from pathlib import Path

from chronozarr._install import extra_hint
from chronozarr.view import VIEWER_URL, local_access


def player(
    store: str | Path,
    *,
    height: int = 560,
    viewer: str | None = None,
    port: int = 0,
    base_url: str | None = None,
    viewer_dir: str | Path | None = None,
    t: int = 0,
    product: str = "",
    controls: bool = False,
    band: str = "",
    range: list[float] | None = None,
    colormap: str = "gray",
    speed: float = 4,
    playing: bool = False,
    theme: str = "light",
):
    """Return an anywidget player for a local or hosted store.

    Install ``chronozarr[notebook]``. Display the result in a trusted Jupyter,
    VS Code or compatible notebook. Set ``widget.t``, ``product``, ``speed`` or
    ``playing``, ``band`` (name), ``range`` (physical limits or None) and ``colormap``
    (``gray``, ``viridis``, ``magma``, ``turbo``; the single-band product's colors) from Python.
    By default only play, the time slider and, once the store is open, a link that opens the same
    view in the full viewer (which has Export: MP4 or WebM in Chrome and Safari, WebM in Firefox,
    GIF everywhere) are shown. ``controls=True`` reveals product, band, colormap, display-limit
    and speed controls.
    Inspect ``times``, ``state``, ``click``, ``error`` and ``hint``.
    Local stores use the same CORS/range server as view(), on 127.0.0.1 of the machine
    that runs the kernel. In a remote notebook pass ``base_url`` (where the browser reaches
    that server) or ``viewer_dir`` (a self-hosted viewer; on JupyterHub it is served through
    jupyter-server-proxy). When the viewer cannot open the store, ``hint`` says what the
    server has seen and what to try. Closing the widget removes its iframe.
    """
    try:
        from chronozarr.widget import Player
    except ImportError as exc:
        raise ImportError(f"chronozarr.player needs anywidget: {extra_hint('notebook')}") from exc
    text = str(store)
    hosted = text.startswith(("https://", "http://"))
    if hosted and (base_url is not None or viewer_dir is not None):
        raise ValueError("base_url and viewer_dir apply to local stores, not to a URL")
    # The traits check the display options before a local server starts.
    widget = Player(
        height=height,
        t=t,
        product=product,
        controls=controls,
        band=band,
        range=range,
        colormap=colormap,
        speed=speed,
        playing=playing,
        theme=theme,
    )
    try:
        if hosted:
            widget.viewer_url = viewer or VIEWER_URL
            widget.store_url = text
        else:
            access = local_access(
                text, port=port, base_url=base_url, viewer=viewer, viewer_dir=viewer_dir
            )
            widget.access = access
            widget.viewer_url = access.viewer_url
            widget.store_url = access.store_url
    except Exception:
        widget.close()
        raise
    return widget
