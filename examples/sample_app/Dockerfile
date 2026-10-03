# Image for the Aegis sample application.
#
# The HEALTHCHECK is what makes `container_health` meaningful. Without one, that
# tool reports "no_healthcheck" rather than "healthy", because a running
# container is not evidence of a working one.
#
# The sample app has no dependencies, so there is nothing to install and the
# build does not need a package index.

FROM python:3.12-alpine

WORKDIR /app

# Copied rather than ADDed, and only the one file, so the build context cannot
# quietly pull in host files the image did not need.
COPY app.py ./app.py

# Not published by default: `start_container` is what decides which ports to
# publish, and it validates them.
EXPOSE 8000

# Runs as a non-root user. The app binds an unprivileged port, so it needs none
# of the capabilities root would bring.
USER nobody

HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=3 \
    CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# -u keeps stdout unbuffered, so logs are readable while the container runs
# rather than arriving all at once when it stops.
CMD ["python", "-u", "./app.py"]