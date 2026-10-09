from uuid import UUID


def synthetic_uuid(value: int) -> UUID:
    """Return a stable synthetic UUID for one visible test example."""

    return UUID(int=value)
