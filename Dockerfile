FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "python -u -m app.bot & exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-4000}"]
