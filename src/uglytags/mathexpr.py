import math
import re
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

MAX_LENGTH = 2000
MAX_FACTORIAL = 1000
CONTEXT = Context(prec=64, Emax=999_999, Emin=-999_999, traps=[InvalidOperation, Overflow])
INFINITY = Decimal("Infinity")
NAN = Decimal("NaN")
TOKEN = re.compile(
    r"\s*(?:(?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)|(?P<name>[A-Za-z_]\w*)"
    r"|(?P<op>==|!=|<=|>=|[-+*/^%(),!<>]))"
)
CONSTANTS = {
    "pi": Decimal(str(math.pi)),
    "PI": Decimal(str(math.pi)),
    "e": Decimal(str(math.e)),
    "E": Decimal(str(math.e)),
    "tau": Decimal(str(math.tau)),
    "phi": Decimal("1.618033988749894848204586834365638117720309179805762862135448623"),
    "Infinity": INFINITY,
    "NaN": NAN,
    "true": Decimal(1),
    "false": Decimal(0),
}


class MathError(Exception):
    pass


def _float_function(function):
    def wrapper(*values):
        try:
            return Decimal(repr(function(*(float(value) for value in values))))
        except (ValueError, OverflowError) as error:
            raise MathError(str(error)) from error

    return wrapper


def _mod(x, y):
    if y == 0:
        return x
    return x - y * (x / y).to_integral_value(rounding="ROUND_FLOOR")


def _divide(x, y):
    if y == 0:
        if x == 0 or x.is_nan():
            return NAN
        return INFINITY if x > 0 else -INFINITY
    return x / y


def _round(x, places=Decimal(0)):
    return x.quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)


def _log(x, base=None):
    if base is None:
        return x.ln()
    return x.ln() / base.ln()


def _nth_root(x, n=Decimal(2)):
    if x < 0 and int(n) % 2:
        return -((-x) ** (1 / n))
    return x ** (1 / n)


def _gcd(*values):
    return Decimal(math.gcd(*(int(value) for value in values)))


def _lcm(*values):
    return Decimal(math.lcm(*(int(value) for value in values)))


FUNCTIONS = {
    **{
        name: _float_function(getattr(math, name))
        for name in (
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan",
            "atan2",
            "sinh",
            "cosh",
            "tanh",
            "asinh",
            "acosh",
            "atanh",
            "log1p",
            "expm1",
            "hypot",
        )
    },
    "exp": lambda x: x.exp(),
    "log": _log,
    "log2": lambda x: x.ln() / Decimal(2).ln(),
    "log10": lambda x: x.log10(),
    "sqrt": lambda x: x.sqrt(),
    "cbrt": lambda x: _nth_root(x, Decimal(3)),
    "nthRoot": _nth_root,
    "pow": lambda x, y: x**y,
    "square": lambda x: x * x,
    "cube": lambda x: x * x * x,
    "round": _round,
    "floor": lambda x: x.to_integral_value(rounding="ROUND_FLOOR"),
    "ceil": lambda x: x.to_integral_value(rounding="ROUND_CEILING"),
    "fix": lambda x: x.to_integral_value(rounding="ROUND_DOWN"),
    "sign": lambda x: Decimal((x > 0) - (x < 0)),
    "abs": abs,
    "add": lambda x, y: x + y,
    "subtract": lambda x, y: x - y,
    "multiply": lambda x, y: x * y,
    "divide": _divide,
    "mod": _mod,
    "min": min,
    "max": max,
    "gcd": _gcd,
    "lcm": _lcm,
}


class Parser:
    def __init__(self, equation: str):
        self.tokens = []
        position = 0
        while position < len(equation):
            if equation[position:].strip() == "":
                break
            match = TOKEN.match(equation, position)
            if not match:
                raise MathError(f"Unexpected character {equation[position:].strip()[0]!r}")
            kind = match.lastgroup
            self.tokens.append((kind, match[kind]))
            position = match.end()
        self.position = 0

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else (None, None)

    def take(self, value=None):
        kind, token = self.peek()
        if kind is None or (value is not None and token != value):
            raise MathError(f"Expected {value or 'value'}")
        self.position += 1
        return token

    def parse(self):
        if not self.tokens:
            raise MathError("Empty expression")
        value = self.comparison()
        if self.position != len(self.tokens):
            raise MathError(f"Unexpected token {self.peek()[1]!r}")
        return value

    def comparison(self):
        value = self.additive()
        while self.peek()[1] in ("==", "!=", "<", ">", "<=", ">="):
            operator = self.take()
            other = self.additive()
            value = Decimal(
                {
                    "==": value == other,
                    "!=": value != other,
                    "<": value < other,
                    ">": value > other,
                    "<=": value <= other,
                    ">=": value >= other,
                }[operator]
            )
        return value

    def additive(self):
        value = self.term()
        while self.peek()[1] in ("+", "-"):
            value = value + self.term() if self.take() == "+" else value - self.term()
        return value

    def term(self):
        value = self.unary()
        while True:
            kind, token = self.peek()
            if token in ("*", "/", "%") or (kind == "name" and token == "mod"):
                self.take()
                other = self.unary()
                if token == "*":
                    value = value * other
                elif token == "/":
                    value = _divide(value, other)
                else:
                    value = _mod(value, other)
            elif kind in ("number", "name") or token == "(":
                value = value * self.power()
            else:
                return value

    def unary(self):
        token = self.peek()[1]
        if token in ("-", "+"):
            self.take()
            value = self.unary()
            return -value if token == "-" else value
        return self.power()

    def power(self):
        value = self.postfix()
        if self.peek()[1] == "^":
            self.take()
            exponent = self.unary()
            if value == 0 and exponent < 0:
                return INFINITY
            value = value**exponent
        return value

    def postfix(self):
        value = self.primary()
        while self.peek()[1] == "!":
            self.take()
            if value != value.to_integral_value() or value < 0 or value > MAX_FACTORIAL:
                raise MathError(f"Factorial requires a whole number up to {MAX_FACTORIAL}")
            value = Decimal(math.factorial(int(value)))
        return value

    def primary(self):
        kind, token = self.peek()
        if kind == "number":
            self.take()
            return Decimal(token)
        if token == "(":
            self.take()
            value = self.comparison()
            self.take(")")
            return value
        if kind == "name":
            self.take()
            if token in FUNCTIONS and self.peek()[1] == "(":
                self.take("(")
                arguments = []
                if self.peek()[1] != ")":
                    arguments.append(self.comparison())
                    while self.peek()[1] == ",":
                        self.take()
                        arguments.append(self.comparison())
                self.take(")")
                try:
                    return FUNCTIONS[token](*arguments)
                except TypeError as error:
                    raise MathError(f"Wrong number of arguments for {token}") from error
            if token in CONSTANTS:
                return CONSTANTS[token]
            raise MathError(f"Undefined symbol {token}")
        raise MathError("Unexpected end of expression" if kind is None else f"Unexpected {token!r}")


def clean_trailing_zeros(value: str) -> str:
    if "." not in value:
        return value
    value = value.rstrip("0")
    return value[:-1] if value.endswith(".") else value


def to_fixed(value: Decimal, places: int) -> str:
    return f"{value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"


def to_string(value: Decimal) -> str:
    exponent = value.adjusted()
    if exponent >= 21 or exponent <= -7:
        sign, digits, _ = value.as_tuple()
        mantissa = "".join(map(str, digits)).rstrip("0") or "0"
        mantissa = mantissa[0] + ("." + mantissa[1:] if len(mantissa) > 1 else "")
        return f"{'-' if sign else ''}{mantissa}e{'+' if exponent > 0 else '-'}{abs(exponent)}"
    return clean_trailing_zeros(f"{value:f}")


def format_number(value: Decimal) -> str:
    if value.is_nan():
        return "NaN"
    if value.is_infinite():
        return "Infinity" if value > 0 else "-Infinity"
    value = value.normalize(CONTEXT) if value else Decimal(0)
    long_string = clean_trailing_zeros(to_fixed(value, 20))
    decimals = long_string.partition(".")[2]
    if decimals and len(set(decimals)) == 1:
        return clean_trailing_zeros(to_fixed(value, 2))
    return to_string(value)


def evaluate(equation: str) -> str:
    equation = equation.replace("**", "^")
    equation = re.sub(
        r"\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\b", lambda match: match[0].replace(",", ""), equation
    )
    if len(equation) > MAX_LENGTH:
        raise MathError("Equation is too long")
    with localcontext(CONTEXT):
        try:
            return format_number(Parser(equation).parse())
        except Overflow:
            return "Infinity"
        except (InvalidOperation, DivisionByZero, ValueError) as error:
            raise MathError(type(error).__name__) from error
