from __future__ import annotations

"""Providers sub-package — re-exports all provider symbols."""

# ruff: noqa: I001  -- import order intentionally grouped with comments
from rubetunes.providers.amazon import *  # noqa: F401, F403, E402
from rubetunes.providers.deezer import *  # noqa: F401, F403, E402

# monochrome provider (Tidal via community-run proxy instances)
from rubetunes.providers.monochrome import *  # noqa: F401, F403, E402

# musicdl provider (multi-source Chinese/global music downloader)
from rubetunes.providers.musicdl import *  # noqa: F401, F403, E402
from rubetunes.providers.qobuz import *  # noqa: F401, F403, E402

# spotiflac provider (Qobuz + Amazon Music FLAC downloads via SpotiFLAC backend)
from rubetunes.providers.spotiflac import *  # noqa: F401, F403, E402
from rubetunes.providers.tidal import *  # noqa: F401, F403, E402
from rubetunes.providers.tidal_alt import *  # noqa: F401, F403, E402
from rubetunes.providers.youtube import *  # noqa: F401, F403, E402

# Explicit __all__ needed so that `from rubetunes.providers import *`
# re-exports private (_-prefixed) names into caller's namespace.
from rubetunes.providers import (  # noqa: E402, E501
    amazon as _am,
    deezer as _dz,
    monochrome as _mc,
    musicdl as _mdl,
    qobuz as _qz,
    spotiflac as _sf,
    tidal as _td,
    tidal_alt as _ta,
    youtube as _yt,
)

__all__: list[str] = []
for _m in [_dz, _qz, _td, _ta, _am, _yt, _mc, _mdl, _sf]:
    __all__.extend(getattr(_m, "__all__", []))
del _dz, _qz, _td, _ta, _am, _yt, _mc, _mdl, _sf, _m

