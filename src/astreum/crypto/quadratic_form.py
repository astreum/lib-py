import math


def extended_gcd(a: int, b: int) -> tuple[int, int, int]:
    """Return (g, x, y) such that a*x + b*y = g = gcd(a, b).

    Args:
        a: First integer.
        b: Second integer.

    Returns:
        tuple[int, int, int]: Description of the return value.
    """
    if b == 0:
        return (a, 1, 0)
    g, x1, y1 = extended_gcd(b, a % b)
    return (g, y1, x1 - (a // b) * y1)


def modinv(a: int, m: int) -> int:
    """Return modular inverse of a mod m.

    Args:
        a: Integer to find inverse for.
        m: Modulus.

    Returns:
        int: Description of the return value.
    """
    g, x, _ = extended_gcd(a, m)
    if g != 1:
        raise ValueError(f"No modular inverse for {a} modulo {m}")
    return x % m


def is_reduced(q: 'QuadraticForm') -> bool:
    """Check if the form q is in reduced Gauss form.

    Args:
        q: Quadratic form to check.

    Returns:
        bool: Description of the return value.
    """
    return abs(q.b) <= q.a <= q.c and not (q.a == q.c and q.b < 0)


def is_primitive(a: int, b: int, c: int) -> bool:
    """Check if the form coefficients are coprime.

    Args:
        a: First coefficient.
        b: Second coefficient.
        c: Third coefficient.

    Returns:
        bool: Description of the return value.
    """
    return math.gcd(math.gcd(a, b), c) == 1


class QuadraticForm:
    """Represents a binary quadratic form in reduced, primitive form.

    The form is ax^2 + bxy + cy^2 with discriminant D.
    """

    def __init__(self, a: int, b: int, c: int, D: int):
        if b*b - 4*a*c != D:
            raise ValueError(f"Discriminant mismatch: b^2 - 4ac = {b*b - 4*a*c}, expected {D}")
        if not is_primitive(a, b, c):
            raise ValueError("Form coefficients are not coprime (not primitive)")
        self.a = a
        self.b = b
        self.c = c
        self.D = D

    def reduce(self) -> 'QuadraticForm':
        """
        Perform Gauss reduction until the form is reduced.
        """
        while not is_reduced(self):
            self._gauss_step()
        return self

    def _gauss_step(self) -> None:
        """
        One iteration of Gauss reduction on the current form.
        """
        a, b, c = self.a, self.b, self.c
        # Compute m = round(b / (2a)) using integer arithmetic
        sign = 1 if b >= 0 else -1
        m = (b + sign * a) // (2 * a)
        # Update b and c
        b_new = b - 2 * m * a
        c_new = m * m * a - m * b + c
        # Assign back
        self.b = b_new
        self.c = c_new
        # Swap if needed to ensure a <= c and proper sign
        if a > self.c or (a == self.c and self.b < 0):
            self.a, self.b, self.c = self.c, -self.b, a

    def __mul__(self, other: 'QuadraticForm') -> 'QuadraticForm':
        """
        Dirichlet (NUCOMP) composition of two forms of the same discriminant.
        """
        if self.D != other.D:
            raise ValueError("Cannot compose forms with different discriminants")
        a1, b1, c1 = self.a, self.b, self.c
        a2, b2, c2 = other.a, other.b, other.c
        D = self.D
        # Compute g = gcd(a1, a2, (b1 + b2)//2)
        k = (b1 + b2) // 2
        g = math.gcd(math.gcd(a1, a2), k)
        a1p = a1 // g
        a2p = a2 // g
        # Solve m * a1p ≡ (b2 - b1)//2 mod a2p
        diff = (b2 - b1) // 2
        inv = modinv(a1p, a2p)
        m = (diff * inv) % a2p
        # Compute composed coefficients
        b3 = b1 + 2 * m * a1
        a3 = a1 * a2p
        c3 = (b3 * b3 - D) // (4 * a3)
        return QuadraticForm(a3, b3, c3, D).reduce()

    def to_bytes(self) -> bytes:
        """
        Serialize this form to bytes (a and b, big-endian, fixed width).
        """
        # Width: enough to hold |D| bitlength / 8 rounded up
        width = (self.D.bit_length() + 15) // 8
        return self.a.to_bytes(width, 'big', signed=True) + \
               self.b.to_bytes(width, 'big', signed=True)

    @classmethod
    def from_bytes(cls, data: bytes, D: int) -> 'QuadraticForm':
        """
        Deserialize bytes back into a QuadraticForm for discriminant D.
        """
        width = len(data) // 2
        a = int.from_bytes(data[:width], 'big', signed=True)
        b = int.from_bytes(data[width:], 'big', signed=True)
        c = (b*b - D) // (4 * a)
        return cls(a, b, c, D).reduce()
