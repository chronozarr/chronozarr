"""Optional notebook player. Importing chronozarr does not require anywidget."""

from pathlib import Path

from chronozarr.view import VIEWER_URL, serve_store


def player(
    store: str | Path,
    *,
    height: int = 560,
    viewer: str = VIEWER_URL,
    port: int = 0,
    t: int = 0,
    product: str = "",
    speed: float = 4,
    playing: bool = False,
    theme: str = "light",
):
    """Return an anywidget player for a local or hosted store.

    Install ``chronozarr[notebook]``. Display the result in a trusted Jupyter,
    VS Code or compatible notebook. Set ``widget.t``, ``product``, ``speed`` or
    ``playing`` from Python; inspect ``times``, ``state``, ``click`` and ``error``.
    Local stores use the same CORS/range server as view(); remote kernels need
    a store URL reachable by the browser. Closing the widget removes its iframe.
    """
    try:
        from chronozarr.widget import Player
    except ImportError as exc:
        raise ImportError(
            "chronozarr.player needs anywidget: install 'chronozarr[notebook]'"
        ) from exc
    text = str(store)
    url = text if text.startswith(("https://", "http://")) else serve_store(text, port=port).url
    return Player(
        store_url=url,
        viewer_url=viewer,
        height=height,
        t=t,
        product=product,
        speed=speed,
        playing=playing,
        theme=theme,
    )
