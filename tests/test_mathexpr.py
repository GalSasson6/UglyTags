import pytest

from uglytags.mathexpr import MathError, evaluate


@pytest.mark.parametrize(
    ("equation", "expected"),
    [
        ("1 + 2", "3"),
        ("2 + 3 * 4", "14"),
        ("(2 + 3) * 4", "20"),
        ("2 ** 3", "8"),
        ("2^3^2", "512"),
        ("-2^2", "-4"),
        ("10 / 4", "2.5"),
        ("1 / 3", "0.33"),
        ("10 % 3", "1"),
        ("-7 mod 3", "2"),
        ("5!", "120"),
        ("2pi", "6.283185307179586"),
        ("sqrt(16) + abs(-2)", "6"),
        ("round(2.555, 2)", "2.56"),
        ("max(1, 9, 3)", "9"),
        ("1,000 + 1", "1001"),
        ("1 / 0", "Infinity"),
        ("0 / 0", "NaN"),
        ("3 > 2", "1"),
        ("2^100", "1.267650600228229401496703205376e+30"),
        ("0.0000001 * 1", "1e-7"),
    ],
)
def test_math(equation, expected):
    assert evaluate(equation) == expected


@pytest.mark.parametrize("equation", ["", "1 +", "foo(1)", "import os", "__class__", "2000!"])
def test_math_rejects_invalid_input(equation):
    with pytest.raises(MathError):
        evaluate(equation)
