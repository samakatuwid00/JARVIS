# docker/pi/Dockerfile — pinned Pi coding-agent for JARVIS sandbox spike
FROM node:22-bookworm
# Pi is provider-agnostic; provider/base_url injected at runtime via env.
RUN npm install -g @earendil-works/pi-coding-agent@0.84.3
ENV PI_HOME=/opt/pi
RUN mkdir -p "$PI_HOME"
WORKDIR /work
ENTRYPOINT ["pi"]
