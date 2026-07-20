FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY src ./src
COPY tests ./tests

ENTRYPOINT ["python3", "-m", "fantasy_analytics"]
CMD ["--output", "/app/data/discovery"]
