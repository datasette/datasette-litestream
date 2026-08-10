"""Shared route registry and permission decorator for datasette-litestream.

Route handlers live in ``routes.py`` and register themselves on ``router``;
``register_routes()`` in ``__init__.py`` hands ``router.routes()`` to
Datasette. The router also emits the OpenAPI document the frontend types are
generated from (``just types-routes``).
"""

from functools import wraps

from datasette import Forbidden
from datasette_plugin_router import Router

router = Router(title="datasette-litestream", version="0.3a0")

VIEW_STATUS_ACTION = "litestream-view-status"
MANAGE_ACTION = "litestream-manage"


def permission_required(action):
    """Decorator for router handlers: enforce a permission before running.

    Handlers must declare ``datasette`` and ``request`` parameters so the
    router binds them and this wrapper can run the check.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(datasette, request, **kwargs):
            if not await datasette.allowed(actor=request.actor, action=action):
                raise Forbidden(f"Permission denied for {action}")
            return await func(datasette=datasette, request=request, **kwargs)

        return wrapper

    return decorator
