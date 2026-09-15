# Example image for running sc in a container — see compose.example.yml.
# The engine version is baked in at build time; bump it or override:
#   docker build --build-arg SC_VERSION=v0.4.0 -t sc .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG SC_VERSION=v0.3.0

# git, plus a current GitHub CLI from its official apt repository.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends git gh \
    && rm -rf /var/lib/apt/lists/*

# GH_TOKEN in the environment is all the authentication the container needs:
# gh reads it directly and git uses gh as its credential helper.
RUN git config --system credential."https://github.com".helper '!gh auth git-credential'

RUN uv tool install "git+https://github.com/SteffenPL/sc.git@${SC_VERSION}"

ENV PATH="/root/.local/bin:$PATH" \
    XDG_STATE_HOME=/state \
    GIT_TERMINAL_PROMPT=0

# Mount the configuration at /config/sc.toml; `sc` finds it automatically.
WORKDIR /config
CMD ["sc", "watch"]
