FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home school
WORKDIR /app
COPY school_repos ./school_repos
COPY static ./static
RUN mkdir /app/data && chown school:school /app/data
USER school
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["python", "-m", "school_repos.web", "--host", "0.0.0.0"]
