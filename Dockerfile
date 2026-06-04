FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/cache && chmod -R 777 /app/cache

ENV PORT=8000
EXPOSE 8000

CMD ["/entrypoint.sh"]
