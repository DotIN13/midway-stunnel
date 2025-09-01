from __future__ import annotations
from typing import List, Dict, Type

from remote_app import RemoteApp


# Simple app registry
class AppRegistry:
    _registry: Dict[str, Type[RemoteApp]] = {}

    @classmethod
    def register(cls, app_cls: Type[RemoteApp]):
        cls._registry[app_cls.name] = app_cls
        return app_cls

    @classmethod
    def get(cls, name: str) -> Type[RemoteApp]:
        if name not in cls._registry:
            raise KeyError(
                f"Unknown app '{name}'. Available: {', '.join(sorted(cls._registry))}"
            )
        return cls._registry[name]

    @classmethod
    def choices(cls) -> List[str]:
        return sorted(cls._registry.keys())
