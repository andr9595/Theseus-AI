# Theseus AI - the app is pure Python 3 standard library, so this image
# carries no pip requirements for it. What it does need is:
#   - git            the app shells out to it directly (diff, snapshot, commit, push)
#   - openssh-client  `git push` over ssh needs a real `ssh` binary on PATH
#   - curl            scripts/install-deps.sh uses it to fetch the agent CLIs
#     and `gh`, run from Settings -> Agents *inside* the running container so
#     an image rebuild is never the answer to "I want to add codex"
# ca-certificates is what makes any of the above trust a TLS cert.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Theseus AI" \
      org.opencontainers.image.description="A local, deliberating multi-agent coding council" \
      org.opencontainers.image.source="https://github.com/andr9595/Theseus-AI" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        curl \
        ca-certificates \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# A non-root user to drop into once the entrypoint has fixed ownership on the
# mounted volume - see docker-entrypoint.sh. The uid/gid here are only the
# image's own default; PUID/PGID at runtime is what the host actually uses.
RUN useradd --create-home --uid 1000 --user-group aicouncil

WORKDIR /app
COPY . /app
RUN chown -R aicouncil:aicouncil /app

# Everything installed via Settings -> Agents (the agent CLIs, `gh`) lands
# under $HOME - ~/.local/bin, ~/.codex, ~/.claude, ~/.config/gh - which is why
# that whole directory is the one volume this image needs mounted. It is
# *not* baked into the image: install-deps.sh needs neither Node nor sudo, so
# doing it at first run costs seconds and means the CLIs' own updaters keep
# working exactly as they do on a bare-metal install.
ENV HOME=/home/aicouncil
ENV XDG_CONFIG_HOME=/home/aicouncil/.config
ENV PATH="/home/aicouncil/.local/bin:${PATH}"

EXPOSE 8760
VOLUME ["/home/aicouncil"]

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "aicouncil", "--no-browser", "--host", "0.0.0.0", "--allow-lan"]
