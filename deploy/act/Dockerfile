# Local runner image for scripts/run_ci_locally.sh (docs/local_ci.md).
# catthehacker/ubuntu:act-24.04 is act's medium image; it lacks the GitHub CLI
# that ci.yml's "Detect changed projects" job calls, and the full image that
# ships gh is ~20 GB. Add only gh, from GitHub's own apt repository.
FROM catthehacker/ubuntu:act-24.04
RUN mkdir -p -m 755 /etc/apt/keyrings \
 && wget -qO /etc/apt/keyrings/githubcli-archive-keyring.gpg https://cli.github.com/packages/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*
