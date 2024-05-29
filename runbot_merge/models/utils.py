from typing import Tuple


def enum(model: str, field: str) -> Tuple[str, str]:
    n = f'{model.replace(".", "_")}_{field}_type'
    return n, n
