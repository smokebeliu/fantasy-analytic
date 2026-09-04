FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests

RUN pip install --no-cache-dir .

ENTRYPOINT ["python3", "-m", "fantasy_analytics"]
CMD ["--output", "/app/data/discovery"]
