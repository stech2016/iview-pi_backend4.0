# OPTIONAL web firewall (Coraza, Apache-2.0) inside Caddy. Build:  podman build -t stech-caddy-waf deploy/caddy-waf
# Then in docker-compose.yml change the caddy service to  image: stech-caddy-waf  and add inside your Caddyfile site block:
#     coraza_waf { load_owasp_crs
#                  directives `
#                     Include @coraza.conf-recommended
#                     Include @crs-setup.conf.example
#                     Include @owasp_crs/*.conf
#                     SecRuleEngine On
#                  ` }
# and the line  order coraza_waf first  in the global options block at the top of the Caddyfile.
FROM docker.io/library/caddy:2-builder AS build
RUN xcaddy build --with github.com/corazawaf/coraza-caddy/v2
FROM docker.io/library/caddy:2-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
