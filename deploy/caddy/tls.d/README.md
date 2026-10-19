Optional Caddy TLS snippets (`*.caddy`) imported inside the site block of
`deploy/Caddyfile`. Empty means Caddy's automatic public ACME certificate for
`RAS_PUBLIC_HOST`. Keep keys and certificates out of Git; point
`RAS_CADDY_TLS_DIR` at an operator directory instead of adding files here.
