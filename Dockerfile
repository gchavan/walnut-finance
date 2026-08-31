FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY walnut ./walnut
RUN pip install --no-cache-dir -e .

ENV WALNUT_WEB_HOST=0.0.0.0
ENV WALNUT_WEB_PORT=8766
ENV WALNUT_DATA_DIR=/app/data

EXPOSE 8766
CMD ["walnut", "serve"]
