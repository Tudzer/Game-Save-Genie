# Serves the docs/ landing page as a container, for hosts that want one.
#
# GitHub Pages serves the same directory with no container at all and is the
# simpler option; this exists so the site can also run on Railway or anywhere
# else that takes a Dockerfile. Build context is the repo root:
#
#   docker build -f packaging/site/Dockerfile -t gsg-site .
#   docker run --rm -p 8080:8080 -e PORT=8080 gsg-site
FROM caddy:2-alpine

COPY packaging/site/Caddyfile /etc/caddy/Caddyfile
COPY docs/ /srv/

# Fails the build rather than the request if the page is missing.
RUN test -f /srv/index.html && caddy validate --config /etc/caddy/Caddyfile
