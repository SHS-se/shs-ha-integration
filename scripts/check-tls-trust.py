"""Reject the trust anchors for the October 2026 eMudhra compromise."""
import ssl


def check_trust_store(context):
    blocked = {"AAA Certificate Services", "emSign ECC Root CA - G3", "emSign Root TLS CA - G3"}
    certificates = context.get_ca_certs()
    if not certificates:
        raise RuntimeError("The TLS trust store is empty")
    trusted = {value for certificate in certificates for rdn in certificate["subject"]
               for key, value in rdn if key == "commonName"}
    remaining = blocked & trusted
    if remaining:
        raise RuntimeError("Compromised eMudhra chains remain trusted: " + ", ".join(sorted(remaining)))
    print(f"TLS trust store checked: {len(certificates)} CAs; compromised eMudhra paths distrusted")


if __name__ == "__main__":
    check_trust_store(ssl.create_default_context())
