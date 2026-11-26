FROM public.ecr.aws/docker/library/ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends apparmor && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/bin/bash", "-euc", "if [[ ! -f /sys/kernel/security/apparmor/profiles ]]; then mount -t securityfs securityfs /sys/kernel/security; fi; exec apparmor_parser \"$@\"", "apparmor"]
