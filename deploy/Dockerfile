# Stage 2 of the omnigent-ae image. Stage 1 is upstream's own server image built
# from the patched tree (upstream/deploy/docker/Dockerfile), so the web bundle
# carries the UI patches. This stage adds the AE layer on top.
#
# Build (from the repo root; `just image` runs exactly this):
#
#   docker build -f upstream/deploy/docker/Dockerfile -t omnigent-ae-base:local upstream
#   docker build -f deploy/Dockerfile --build-arg BASE=omnigent-ae-base:local -t omnigent-ae:local .
#
# Phase 1 (2026-09-23) installs the policies package into the base image's venv
# (/opt/venv is first on PATH there, so `pip` is the venv's) and ships a reference
# copy of the server config, which the box reads in place since 2026-09-24
# (OMNIGENT_CONFIG=/opt/ae/config/server.yaml; config/server.env.example).
# Phase 3 (2026-09-23) installs the extension package the same way: its bundle
# is committed under src/ae_omni_extension/dist, so no Node is needed here, and
# upstream discovers it through the `omnigent.extensions` entry point at startup.
# Phase 5 (2026-09-23) copies the agent specs to /opt/ae/agents so the
# container can register the steward and the shutdown note through
# OMNIGENT_BUILTIN_AGENT_DIRS=/opt/ae/agents/steward:/opt/ae/agents/shutdown
# (set in the deployment's environment, not here: reviewer-codex is in the
# same directory and off by default). The server only registers the specs; the
# MCP server they spawn runs on the host that fires them, which needs the
# policies package in its own Python (README, Phase 5).
ARG BASE=omnigent-ae-base:local
FROM ${BASE}

LABEL org.opencontainers.image.source="https://github.com/analyticendeavors/omnigent-ae" \
      org.opencontainers.image.description="OmniGent with Analytic Endeavors' branch-discipline layer" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY packages/ae_omni_policies /opt/ae/packages/ae_omni_policies
COPY packages/ae_omni_extension /opt/ae/packages/ae_omni_extension
COPY config/server.yaml /opt/ae/config/server.yaml
COPY config/server.env.example /opt/ae/config/server.env.example
COPY agents /opt/ae/agents
RUN pip install --no-cache-dir /opt/ae/packages/ae_omni_policies /opt/ae/packages/ae_omni_extension \
 && python -c "import ae_omni_policies, ae_omni_extension, ae_omni_policies.mcp" \
 && test -f /opt/ae/agents/steward/agent.yaml \
 && test -f /opt/ae/agents/shutdown/agent.yaml \
 && test -f /app/entrypoint.py \
 && omni extensions doctor analyticendeavors.parking-lot

# Upstream's CMD runs /app/entrypoint.py, which ignores policy_modules and
# debug_router_modules; the AE wrapper passes both from the same config file
# and then runs that script unchanged (found on the first box deploy,
# 2026-09-24: without it every /v1/ae route answered 404).
CMD ["python", "-m", "ae_omni_policies.docker_entrypoint"]
