# Casper's public site (Warm Paper). Static pages; each deployment's URLs are
# filled in at container start (deploy/40-casper-urls.sh).
FROM nginx:1.27-alpine
COPY . /site
COPY deploy/40-casper-urls.sh /docker-entrypoint.d/40-casper-urls.sh
EXPOSE 8080
