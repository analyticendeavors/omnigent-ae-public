# The egress proxy image for a user's OmniGent host container (2026-10-20):
# Canonical's Squid image with squid.conf baked in, so nothing on the box is
# bind-mounted into the container. Built by deploy/host-container.sh build,
# with deploy/host-container/ as the build context.
# The tag and digest are pinned together; to move them, pick a newer
# ubuntu/squid tag on Docker Hub, `docker pull` it, and copy both from
# `docker image inspect --format '{{index .RepoDigests 0}}'`.
FROM ubuntu/squid:6.6-24.04_edge@sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c

COPY squid.conf /etc/squid/squid.conf
RUN /usr/sbin/squid -k parse -f /etc/squid/squid.conf
