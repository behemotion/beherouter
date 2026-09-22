"""Mint the e2e key material: an IdP key set and three users' tokens.

Kept out of `up.sh` so the material is a FILE the stack and the assertions both
read — the same JWT must reach the gateway and be compared against what the
backend saw, byte for byte, or the forwarding check proves nothing.

The private key never leaves this process: `material.json` carries the PUBLIC
JWKS and three already-signed tokens, and it is gitignored regardless.
"""

import json
import pathlib

from authlib.jose import JsonWebKey
from fastmcp.server.auth.providers.jwt import RSAKeyPair

ISSUER = "https://idp.e2e.invalid/realms/main"
AUDIENCE = "beherouter"
KID = "e2e-key-1"

# alice and bob hold the gated role; carol does not, which is the refusal path.
USERS = {
    "alice": ["ai-plane-access"],
    "bob": ["ai-plane-access"],
    "carol": ["some-other-role"],
}


def main() -> None:
    kp = RSAKeyPair.generate()
    public = kp.public_key
    public = public.get_secret_value() if hasattr(public, "get_secret_value") else public
    jwk = JsonWebKey.import_key(public, {"kty": "RSA"}).as_dict()
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})

    material = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks": {"keys": [jwk]},
        "tokens": {
            name: kp.create_token(
                subject=name,
                issuer=ISSUER,
                audience=AUDIENCE,
                kid=KID,
                expires_in_seconds=3600,
                additional_claims={
                    "email": f"{name}@bank.invalid",
                    "preferred_username": name,
                    "realm_access": {"roles": roles},
                },
            )
            for name, roles in USERS.items()
        },
    }
    out = pathlib.Path(__file__).parent / "material.json"
    out.write_text(json.dumps(material, indent=2))
    print(f"wrote {out} ({', '.join(USERS)}; tokens valid 1h)")


if __name__ == "__main__":
    main()
