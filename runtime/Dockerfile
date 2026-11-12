# ToolKernel runtime image: one container per agent session.
# The helper library and generated server packages are mounted read-only at run time,
# so this image only needs Python and the data stack.
FROM python:3.12-slim

ARG EXTRA_PACKAGES=""
RUN pip install --no-cache-dir pandas numpy python-dateutil pyyaml ${EXTRA_PACKAGES} \
 && useradd --uid 1000 --create-home tk \
 && mkdir -p /workspace/saved /workspace/.outputs \
 && chown -R tk /workspace

USER 1000
WORKDIR /workspace
ENV PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOOLKERNEL_RUNTIME_HOST=0.0.0.0 \
    TOOLKERNEL_RUNTIME_PORT=8888 \
    HOME=/tmp
EXPOSE 8888
CMD ["python", "-m", "toolkernel._server"]
