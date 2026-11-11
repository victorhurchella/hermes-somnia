"""Somnia's REST door: ``POST /api/plugins/hermes-somnia/rpc/<method>`` runs the same handlers as the
``plugin.hermes-somnia.<method>`` gateway RPC methods.

The desktop page calls the gateway first. A Desktop connected to a backend without a session authority
(a standalone ``hermes dashboard``) answers plugin RPC with -32601, so the page falls back to this route,
which every backend that serves the Desktop mounts.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _rpc_module():
    for name, module in list(sys.modules.items()):
        if name.endswith("hermes_somnia.rpc") and getattr(module, "HANDLERS", None):
            return module
    if str(_PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_ROOT))
    return importlib.import_module("hermes_somnia.rpc")


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
