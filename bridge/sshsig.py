"""Verify SSH (SSHSIG) signatures on git tags with the standard library only, so no ssh-keygen is needed.

Formats: OpenSSH PROTOCOL.sshsig and RFC 8032 (Ed25519). Only ssh-ed25519 keys are accepted.
"""
import base64
import hashlib

BEGIN, END = "-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----"
P = 2 ** 255 - 19
Q = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


class BadSignature(ValueError):
    pass


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    return P - x if (x & 1) != sign else x


def _decode_point(data):
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        raise BadSignature("bad_point")
    return (x, y, 1, x * y % P)


def _add(a, b):
    e, f = (a[1] - a[0]) * (b[1] - b[0]) % P, (a[1] + a[0]) * (b[1] + b[0]) % P
    g, h = 2 * a[3] * b[3] * D % P, 2 * a[2] * b[2] % P
    e, f, g, h = f - e, h - g, h + g, f + e
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, point):
    result = (0, 1, 1, 0)
    while s:
        if s & 1:
            result = _add(result, point)
        point, s = _add(point, point), s >> 1
    return result


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def ed25519_verify(public, message, signature):
    """RFC 8032 section 5.1.7. Returns True or False; never raises for a well-formed key length."""
    if len(public) != 32 or len(signature) != 64:
        return False
    try:
        a, r = _decode_point(public), _decode_point(signature[:32])
    except BadSignature:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % Q
    left, right = _mul(s, G), _add(r, _mul(h, a))
    return (left[0] * right[2] - right[0] * left[2]) % P == 0 and (left[1] * right[2] - right[1] * left[2]) % P == 0


def _strings(data, count):
    out, i = [], 0
    for _ in range(count):
        if i + 4 > len(data):
            raise BadSignature("truncated")
        n = int.from_bytes(data[i:i + 4], "big")
        out.append(data[i + 4:i + 4 + n])
        i += 4 + n
        if i > len(data):
            raise BadSignature("truncated")
    return out, data[i:]


def public_key_blob(line):
    """The binary key from an `ssh-ed25519 AAAA...` field (an allowed_signers or .pub line)."""
    fields = line.split()
    index = fields.index("ssh-ed25519")
    return base64.b64decode(fields[index + 1])


def fingerprint(blob):
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def split_signed_tag(raw):
    """Split a `git cat-file tag` object into (signed payload, armoured signature)."""
    text = raw.decode("utf-8")
    start = text.rfind(BEGIN)
    if start < 0 or END not in text[start:]:
        raise BadSignature("unsigned")
    return text[:start].encode("utf-8"), text[start:text.index(END, start) + len(END)]


def verify(payload, armoured, allowed_blobs, namespace="git"):
    """Check an SSHSIG over payload. Returns the signer's key fingerprint, or raises BadSignature."""
    body = "".join(line.strip() for line in armoured.strip().splitlines()[1:-1])
    blob = base64.b64decode(body)
    if blob[:6] != b"SSHSIG" or int.from_bytes(blob[6:10], "big") != 1:
        raise BadSignature("not_sshsig")
    (key, space, reserved, algorithm, sig), _ = _strings(blob[10:], 5)
    if key not in allowed_blobs:
        raise BadSignature("unknown_key")
    if space.decode() != namespace:
        raise BadSignature("wrong_namespace")
    if algorithm not in (b"sha512", b"sha256"):
        raise BadSignature("unsupported_hash")
    (kind, raw_key), _ = _strings(key, 2)
    (sig_kind, raw_sig), _ = _strings(sig, 2)
    if kind != b"ssh-ed25519" or sig_kind != b"ssh-ed25519":
        raise BadSignature("unsupported_key")
    digest = hashlib.new(algorithm.decode(), payload).digest()
    def string(value):
        return len(value).to_bytes(4, "big") + value
    signed = b"SSHSIG" + string(space) + string(reserved) + string(algorithm) + string(digest)
    if not ed25519_verify(raw_key, signed, raw_sig):
        raise BadSignature("bad_signature")
    return fingerprint(key)
