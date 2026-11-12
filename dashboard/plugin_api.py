"""Somnia's REST door: ``POST /api/plugins/hermes-somnia/rpc/<method>`` runs the same handlers as the
``plugin.hermes-somnia.<method>`` gateway RPC methods.

The desktop page calls the gateway first. A Desktop connected to a backend without a session authority
(a standalone ``hermes dashboard``) answers plugin RPC with -32601, so the page falls back to this route,
which every backend that serves the Desktop mounts.

The dashboard is a long-lived process. When the plugin's Python files change on disk (``hermes plugins
update``, a git pull), the handlers are re-imported on the next call, so an update never needs a restart
of the backend for the page's data methods.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PKG = "hermes_somnia"
_lock = threading.Lock()
_loaded: dict[str, Any] = {"module": None, "stamp": None}


def _stamp() -> tuple:
    return tuple((f.name, f.stat().st_mtime_ns) for f in sorted((_PLUGIN_ROOT / _PKG).glob("*.py")))


def _rpc_module():
    stamp = _stamp()
    with _lock:
        if _loaded["module"] is not None and _loaded["stamp"] == stamp:
            return _loaded["module"]
        if str(_PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(_PLUGIN_ROOT))
        # Drop only this door's own top-level copy; the plugin manager's namespaced copy stays untouched.
        for name in [n for n in sys.modules if n == _PKG or n.startswith(_PKG + ".")]:
            del sys.modules[name]
        importlib.invalidate_caches()
        module = importlib.import_module(f"{_PKG}.rpc")
        _loaded.update(module=module, stamp=stamp)
        return module


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "methods": sorted(_rpc_module().HANDLERS)}


@router.post("/rpc/{method}")
async def call(method: str, params: dict = Body(default_factory=dict)) -> Any:
    rpc = _rpc_module()
    fn = rpc.HANDLERS.get(method)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"unknown Somnia method: {method}")
    try:
        return await asyncio.to_thread(rpc.wrap(fn), params if isinstance(params, dict) else {}, session_id=None)
    except rpc.RpcError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
