from .shell_tool import ShellTool

__all__ = ["ShellTool", "MemoryCompactor"]


def __getattr__(name: str):
    if name == "MemoryCompactor":
        from .memory_tool import MemoryCompactor
        return MemoryCompactor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
