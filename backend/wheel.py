"""European wheel layout + Nn cluster math (shared by API and audit)."""

WHEEL = [
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23,
    10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
]

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def color_of(number: int) -> str:
    if number == 0:
        return "Green"
    return "Red" if number in REDS else "Black"


def nn_cluster(number: int) -> list[int]:
    """Nn = N + 2 left + 2 right wheel neighbours (5 numbers)."""
    i = WHEEL.index(number)
    return [
        WHEEL[(i - 2) % 37],
        WHEEL[(i - 1) % 37],
        number,
        WHEEL[(i + 1) % 37],
        WHEEL[(i + 2) % 37],
    ]
