# Production API hostname migration

The preferred REST endpoint is https://api.lanis.arg-server.de. Keep
https://lanis-backend.joancode.dev serving the same application without redirects
for existing apps and integrations. No second API process or database is needed.
This PR does not apply infrastructure changes. Do not merge client migration PRs
until the new endpoint has passed the checks below.

## Infrastructure rollout (separate production action)

1. Verify DNS ownership and Cloudflare account membership for arg-server.de and
   joancode.dev. A tunnel DNS target must belong to the same Cloudflare account
   as its zone; if accounts differ, use a second tunnel to the same local API.
2. Confirm Cloudflare edge certificate coverage for api.lanis.arg-server.de.
   A wildcard for *.arg-server.de does not cover this nested hostname. Provision
   suitable coverage before switching clients.
3. Use deploy/cloudflared.yml.example as the ingress configuration, substituting
   the existing tunnel ID and credential path. Preserve any other existing rules.
   Validate with `cloudflared --config <config-path> tunnel ingress validate`.
   Route the new hostname to the existing tunnel using a proxied DNS record;
   retain the old record. Apply the tunnel configuration during the agreed
   Europe/Berlin nighttime maintenance window. The tunnel is a user service:
   `systemctl --user restart lanis-api-tunnel.service`.
4. Keep LANIS_AGENT_ISSUER=https://lanis-backend.joancode.dev. Initially keep
   PUBLIC_BASE_URL unchanged too. This retains OAuth discovery, token resource
   validation, MCP host checks, and existing generated links. After verifying
   the new hostname, PUBLIC_BASE_URL can separately become the new REST origin;
   retain the explicit legacy LANIS_AGENT_ISSUER. API environment changes require
   a planned restart of the user service lanis-api.service.
5. Match API cache, WAF, rate limit and challenge settings across both names.
   Authenticated API responses must not be publicly cached. Investigate any 403
   from the old health endpoint rather than copying a broken rule blindly.
6. Test both HTTPS endpoints: /health, browser OPTIONS with the actual frontend
   Origin, login, refresh, authenticated reads and file downloads. Use the test
   account outside the repository; never record credentials or tokens. Verify
   existing sessions still work after switching the UI backend URL.
7. Verify OAuth discovery and an existing MCP connection on the legacy hostname.
   MCP on the new hostname is intentionally not advertised: the current server
   has one issuer/resource identity and allows the issuer hostname explicitly.
   Supporting a second MCP identity requires separate protocol compatibility
   work; do not simply change LANIS_AGENT_ISSUER or broaden host validation.
8. Merge/deploy the UI and admin client PRs only after these checks pass. Rebuild
   clients because Vite embeds the URL at build time. Existing custom backend
   overrides remain unchanged. Older apps continue to use the old endpoint.

CORS authorizes frontend origins, not backend aliases. The frontend origin
remains https://lanis.arg-server.de; adding this API hostname needs no CORS change.

## Rollback

Revert/redeploy the client URL commits. Keep the old DNS and ingress in place
throughout. If the new route fails, remove only the new route after reverting
clients; leave the API state and OAuth issuer unchanged. Both hostnames share
one server and therefore do not provide independent failover.

## References

- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/dns/
- https://developers.cloudflare.com/ssl/edge-certificates/universal-ssl/limitations/
