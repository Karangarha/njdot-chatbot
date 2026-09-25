# Mock OpenAI + Anthropic API (sandbox only — never deployed).
FROM python:3.13-slim
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
# tiktoken decodes the token-id arrays langchain's OpenAIEmbeddings sends;
# the encoding is downloaded once at build time.
# (Optional "ca" build secret: only for building behind a TLS-inspecting proxy.)
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca REQUESTS_CA_BUNDLE=/run/secrets/ca; fi && \
    pip install --no-cache-dir tiktoken==0.12.0 && \
    (python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" || \
     echo "WARN: could not pre-download cl100k_base; mount a tiktoken cache at $TIKTOKEN_CACHE_DIR")
COPY server.py /app/server.py
EXPOSE 4010
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4010/health')"
CMD ["python", "-u", "/app/server.py"]
