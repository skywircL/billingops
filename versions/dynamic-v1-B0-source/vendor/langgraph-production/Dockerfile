FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY evals ./evals

RUN pip install --no-cache-dir .

# Checkpoints, effect claims, and the approval ledger share one SQLite file.
# Pass a path under /app/data and mount a volume there to keep them across
# container restarts, which is the whole point of them being on disk.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# The default command is the gate, so `docker run <image>` either prints a
# passing report or exits non zero.
CMD ["python", "-m", "langgraph_production.evaluation"]
