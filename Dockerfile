FROM python:3.12-alpine
RUN apk add --no-cache ca-certificates tzdata ffmpeg
WORKDIR /app
COPY requirements.txt LICENSE ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY web/ ./web/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "src/web_settings.py"]
