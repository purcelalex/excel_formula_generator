"""
entry.py — the Cloudflare Worker entry point.

Cloudflare calls `fetch` on this class for every request the static-assets
layer did not already answer. The FastAPI app is exactly the one the container
runs; `asgi.fetch` bridges the two and puts the `env` bindings into the ASGI
scope, which is where runtime.py reads them from.

There is deliberately no logic here. Anything that had to differ between
Cloudflare and the container lives in app/runtime.py instead, so that this file
never becomes a second, divergent copy of the application.
"""

import asgi
from workers import WorkerEntrypoint

from app.main import app


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request, self.env)
