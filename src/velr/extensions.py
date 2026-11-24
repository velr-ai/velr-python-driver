"""Configuration for explicitly enabled native extensions."""

from dataclasses import dataclass
from os import PathLike
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Extension:
    """An installed native extension to load in the Velr process.

    Pass the descriptor to ``Velr.open(extensions=[...])``. Library paths are
    resolved when the descriptor is created. Supply only a library you trust
    to execute in your application. The library receives the positive
    ``memory_limit_bytes`` budget at open time.
    """

    library_path: str | PathLike[str]
    memory_limit_bytes: int

    def __post_init__(self) -> None:
        path = Path(self.library_path)
        if "\x00" in str(path):
            raise ValueError("extension library path cannot contain NUL")
        if isinstance(self.memory_limit_bytes, bool) or not isinstance(self.memory_limit_bytes, int):
            raise TypeError("extension memory_limit_bytes must be an integer")
        if self.memory_limit_bytes <= 0:
            raise ValueError("extension memory_limit_bytes must be positive")
        object.__setattr__(self, "library_path", str(path.resolve()))
