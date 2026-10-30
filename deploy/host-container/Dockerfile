# A second user's OmniGent host, as a container on the box (2026-10-20,
# deploy/README.md "Second user's host (container)").
#
# Stage 1 is upstream's own host image (`--target host` of
# upstream/deploy/docker/Dockerfile), built from the patched submodule, so the
# host runs the same OmniGent, AE patches included, as the server's release:
# python 3.12 with omnigent in /opt/venv, node 22, the claude, codex and pi
# CLIs, git, tmux, gh (pinned upstream), bubblewrap, curl. This stage adds the
# rest of what agent sessions need, pinned, and a non-root user:
#
#   docker build -f upstream/deploy/docker/Dockerfile --target host -t omnigent-ae-host-base:<tag> upstream
#   docker build -f deploy/host-container/Dockerfile --build-arg BASE=omnigent-ae-host-base:<tag> \
#     -t omnigent-ae-host:<tag> deploy/host-container
#
# deploy/host-container.sh build runs both, tagged with OMNIGENT_IMAGE_TAG from
# config/server.env, after checking that the submodule sits on the pin in
# upstream.lock.
#
# Not installed on purpose: packages/ae_omni_policies. Its MCP server (the
# `ae` tools: park_fork, omni_capacity, ...) files into the owner's task-store and
# reads the owner's slots; a second user's sessions have no business there, and the
# scheduled agents that need it run on the owner's host only.
ARG BASE=omnigent-ae-host-base:local
FROM ${BASE}

# Claude Code: upstream installs whatever npm calls latest; pin the version the
# box's own host runs (2026-10-20). Codex and pi stay as upstream ships them.
ARG CLAUDE_CODE_VERSION=2.1.304
# Azure CLI in its own venv, so its dependency set never touches omnigent's.
ARG AZURE_CLI_VERSION=2.91.0
# The in-container user. Not 1000 (<user> on the box) and not root, so a file
# that ever did cross the boundary would belong to nobody on the box.
ARG AGENT_UID=61000

USER root

# jq: register-team-mcp.sh (deploy/host-config-sync.sh) builds its `claude mcp add`
# commands with it (2026-10-29).
RUN apt-get update \
 && apt-get install -y --no-install-recommends jq \
 && rm -rf /var/lib/apt/lists/* \
 && jq --version

RUN npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version | grep -q "^${CLAUDE_CODE_VERSION} "

RUN python -m venv /opt/az \
 && /opt/az/bin/pip install --no-cache-dir "azure-cli==${AZURE_CLI_VERSION}" \
 && ln -s /opt/az/bin/az /usr/local/bin/az \
 && /opt/az/bin/python -c "import sys, azure.cli.core as c; sys.exit(c.__version__ != '${AZURE_CLI_VERSION}')"

RUN groupadd -g "${AGENT_UID}" agent \
 && useradd -m -d /home/agent -s /bin/bash -u "${AGENT_UID}" -g agent agent \
 && mkdir -p /home/agent/.omnigent /home/agent/.claude /home/agent/.config/gh /home/agent/.azure /home/agent/workspaces \
 && chown -R agent:agent /home/agent

COPY entrypoint.sh /usr/local/bin/ae-host-entrypoint
RUN chmod 0755 /usr/local/bin/ae-host-entrypoint

# OMNIGENT_DISABLE_KEYRING: there is no keyring daemon in the container, so the
# stored login lives in ~/.omnigent/auth_tokens.json (the box host does the same).
# AZURE_CORE_COLLECT_TELEMETRY: az would otherwise phone home on every call.
ENV HOME=/home/agent \
    USER=agent \
    OMNIGENT_DISABLE_KEYRING=true \
    AZURE_CORE_COLLECT_TELEMETRY=0

USER agent
WORKDIR /home/agent
CMD ["/usr/local/bin/ae-host-entrypoint"]
