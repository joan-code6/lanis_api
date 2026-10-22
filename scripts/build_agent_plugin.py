"""Build a private ChatGPT plugin package against a verified LANIS MCP issuer.

Does not deploy, upload, or publish. Run after deploying the API and UI.
"""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--issuer", required=True, help="Public HTTPS LANIS_AGENT_ISSUER"
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Destination ZIP outside plugin source",
    )
    args = parser.parse_args()
    issuer = args.issuer.rstrip("/")
    url = urlsplit(issuer)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error(
            "Issuer must be a public HTTPS URL without credentials, query, or fragment."
        )
    if url.path:
        parser.error(
            "Use an origin without a path; LANIS metadata routes are at the origin root."
        )
    session = requests.Session()
    # Verify deployed discovery instead of packaging a guessed endpoint.
    response = session.get(
        issuer + "/.well-known/oauth-protected-resource/mcp",
        timeout=15,
        allow_redirects=False,
    )
    response.raise_for_status()
    resource = response.json()
    if resource.get("resource") != issuer + "/mcp" or issuer not in resource.get(
        "authorization_servers", []
    ):
        parser.error("Deployed resource metadata does not match this issuer.")
    response = session.get(
        issuer + "/.well-known/oauth-authorization-server",
        timeout=15,
        allow_redirects=False,
    )
    response.raise_for_status()
    metadata = response.json()
    if metadata.get("issuer") != issuer or "S256" not in metadata.get(
        "code_challenge_methods_supported", []
    ):
        parser.error(
            "Deployed authorization metadata must advertise this issuer and S256 PKCE."
        )
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "plugins/lanis/plugin.json").read_text())
    assert (
        len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) <= 30
    )
    mcp = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            "lanis": {
                "type": "streamable-http",
                "url": resource["resource"],
                "extensions": {
                    "com.openai": {
                        "auth": {
                            "type": "oauth",
                            "client": {"mode": "dcr"},
                            "defaultScopes": [
                                "schoolwork:read",
                                "timetable:read",
                                "calendar:read",
                            ],
                        }
                    }
                },
            }
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "lanis/plugin.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        archive.writestr(
            "lanis/mcp.json", json.dumps(mcp, ensure_ascii=False, indent=2)
        )
    with ZipFile(args.output) as archive:
        assert sorted(archive.namelist()) == ["lanis/mcp.json", "lanis/plugin.json"]
        for name in archive.namelist():
            json.loads(archive.read(name))
    print(f"Private LANIS plugin package created: {args.output}")
    print(
        "Server deployment and user OAuth approval are separate from plugin installation."
    )


if __name__ == "__main__":
    main()
