"""GR Observer modular monolith."""

__all__ = ["Observer"]


def __getattr__(name):
    if name == "Observer":
        from .application import Observer

        return Observer
    raise AttributeError(name)
