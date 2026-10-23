import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.events import nkey

pytestmark = pytest.mark.unit

SEED = bytes(range(32))


def _raw_public(public: str) -> bytes:
    return base64.b32decode(public + "=" * (-len(public) % 8))[1:-2]


def _unsegment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def test_crc16_is_the_xmodem_checksum():
    assert nkey.crc16(b"123456789") == 0x31C3
    assert nkey.crc16(b"") == 0


@pytest.mark.parametrize(
    ("prefix", "seed_head", "public_head"),
    [(nkey.OPERATOR, "SO", "O"), (nkey.ACCOUNT, "SA", "A"), (nkey.USER, "SU", "U")],
)
def test_a_key_pair_encodes_its_role_in_both_keys(prefix, seed_head, public_head):
    pair = nkey.from_raw(prefix, SEED)
    assert (pair.seed[:2], pair.public[0], len(pair.seed), len(pair.public)) == (seed_head, public_head, 58, 56)
    assert nkey.from_seed(pair.seed) == pair


def test_a_key_pair_signs_with_its_public_key():
    pair = nkey.create(nkey.USER)
    Ed25519PublicKey.from_public_bytes(_raw_public(pair.public)).verify(pair.sign(b"nonce"), b"nonce")
    assert nkey.create(nkey.USER) != pair


def test_a_derived_key_is_stable_per_label():
    parent = nkey.from_raw(nkey.ACCOUNT, SEED)
    first = nkey.derive(parent, nkey.USER, "m1")
    assert first == nkey.derive(parent, nkey.USER, "m1")
    assert first != nkey.derive(parent, nkey.USER, "m2")
    assert first.public[0] == "U"


def test_a_key_pair_keeps_its_seed_out_of_its_repr():
    pair = nkey.create(nkey.USER)
    assert pair.seed not in repr(pair)


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ("not base32!", "^an nkey must be base32 text$"),
        ("AA", "^the nkey checksum does not match$"),
        ("AAAA", "^the nkey checksum does not match$"),
        (nkey.from_raw(nkey.USER, SEED).seed[:-1] + "A", "^the nkey checksum does not match$"),
    ],
)
def test_a_broken_seed_is_refused(seed, message):
    with pytest.raises(nkey.NkeyError, match=message):
        nkey.from_seed(seed)


def test_a_public_key_is_not_a_seed():
    with pytest.raises(nkey.NkeyError, match="^not an nkey seed$"):
        nkey.from_seed(nkey.from_raw(nkey.USER, SEED).public)


def test_a_jwt_carries_its_claims_and_the_signers_signature(monkeypatch):
    monkeypatch.setattr(nkey.time, "time", lambda: 1700.9)
    signer = nkey.create(nkey.ACCOUNT)
    header, claims, signature = nkey.encode_jwt(signer, "UX", "hive-m1", {"type": "user"}).split(".")
    assert json.loads(_unsegment(header)) == {"typ": "JWT", "alg": "ed25519-nkey"}
    decoded = json.loads(_unsegment(claims))
    assert {key: decoded[key] for key in ("iat", "iss", "name", "sub", "nats")} == {
        "iat": 1700,
        "iss": signer.public,
        "name": "hive-m1",
        "sub": "UX",
        "nats": {"type": "user"},
    }
    assert len(decoded["jti"]) == 32
    assert "=" not in header + claims + signature
    Ed25519PublicKey.from_public_bytes(_raw_public(signer.public)).verify(
        _unsegment(signature), f"{header}.{claims}".encode()
    )


def test_creds_text_round_trips_its_jwt_and_seed():
    user = nkey.create(nkey.USER)
    text = nkey.creds("a.b.c", user.seed)
    assert text.startswith("-----BEGIN NATS USER JWT-----\na.b.c\n------END NATS USER JWT------\n")
    assert nkey.parse_creds(text) == ("a.b.c", user)


JWT_BLOCK = "-----BEGIN NATS USER JWT-----\na.b.c\n------END NATS USER JWT------\n"


@pytest.mark.parametrize("text", [JWT_BLOCK, nkey.creds("a.b.c", nkey.from_raw(nkey.USER, SEED).seed) + JWT_BLOCK])
def test_creds_text_without_exactly_one_jwt_and_seed_is_refused(text):
    with pytest.raises(nkey.NkeyError, match="^a NATS creds file holds one user JWT and one seed$"):
        nkey.parse_creds(text)


def test_b64_is_url_safe_and_strips_only_padding():
    assert (nkey.b64(b"\x00\x00\x17"), nkey.b64(b"\xff"), nkey.b64(b"\xfb\xff")) == ("AAAX", "_w", "-_8")


def test_a_jwt_segment_is_compact_json():
    assert _unsegment(nkey._segment({"a": 1, "b": [2]})) == b'{"a":1,"b":[2]}'


def test_a_derived_key_seeds_from_the_hmac_of_its_parent_seed():
    import hashlib
    import hmac

    parent = nkey.from_raw(nkey.ACCOUNT, SEED)
    expected = nkey.from_raw(nkey.USER, hmac.new(SEED, b"m1", hashlib.sha256).digest())
    assert nkey.derive(parent, nkey.USER, "m1") == expected


@pytest.mark.parametrize(
    "raw", [bytes([nkey.SEED, nkey.USER << 3 & 0xFF]) + bytes(33), bytes([0x90])], ids=["long-seed", "one-byte"]
)
def test_a_checksummed_key_of_the_wrong_shape_is_not_a_seed(raw):
    with pytest.raises(nkey.NkeyError, match="^not an nkey seed$"):
        nkey.from_seed(nkey._encode(raw))
