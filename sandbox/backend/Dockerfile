# Backend sandbox that mirrors the Azure App Service "NJDOT" Web App.
#
# Base image: Microsoft's own App Service Linux runtime image for Python 3.13
# (the same image App Service runs the app in; PYTHON_VERSION in
# .github/workflows/azure-deploy-backend.yml is '3.13'). Bump the tag to the
# one your Web App reports in Kudu (https://<app>.scm.azurewebsites.net/Env)
# if you want an exact match.
#
# Build context is backend/, the same folder the deploy workflow uploads
# (`path: backend/*`), extracted to /home/site/wwwroot like App Service does.
FROM mcr.microsoft.com/appsvc/python:3.13_20260910.5.tuxprod

COPY . /home/site/wwwroot
WORKDIR /home/site/wwwroot

# Equivalent of Oryx's server-side build (SCM_DO_BUILD_DURING_DEPLOYMENT=1):
# a virtualenv named "antenv" built from requirements.txt at the app root.
# The image's startup script (oryx create-script -virtualEnvName antenv)
# activates it before running the startup command.
# The optional "ca" build secret is only for building behind a TLS-inspecting
# proxy (e.g. a corporate network); without it this is a plain pip install.
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi && \
    python -m venv antenv && \
    antenv/bin/pip install --no-cache-dir --upgrade pip && \
    antenv/bin/pip install --no-cache-dir -r requirements.txt

# App Service platform settings. SSH and the diagnostics cron are turned off
# because nothing in the sandbox connects to them.
ENV PORT=8000 \
    WEBSITE_SITE_NAME=NJDOT \
    WEBSITE_HOSTNAME=localhost:8000 \
    WEBSITE_SSH_ENABLED=0 \
    WEBSITE_USE_DIAGNOSTIC_SERVER=false \
    SCM_DO_BUILD_DURING_DEPLOYMENT=1

EXPOSE 8000

# ENTRYPOINT is the image's /opt/startup/init_container.sh; its argument is
# the Web App's "Startup Command" (docker-compose.yml passes
# AZURE_STARTUP_COMMAND).
#
# Why "python -m uvicorn" and not the bare "uvicorn ..." written in the
# setup notes of .github/workflows/azure-deploy-backend.yml: Oryx's generated
# startup.sh exposes the antenv virtualenv only through PYTHONPATH — its bin/
# is never put on PATH, and the runtime image has no global uvicorn — so the
# bare command dies with "uvicorn: not found" (reproduced in this image).
CMD ["python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
