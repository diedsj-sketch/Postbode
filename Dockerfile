FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home mailroom
COPY mailroom.py .
RUN mkdir /data && chown mailroom:mailroom /data
USER mailroom
ENV DATA_DIR=/data
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "60", "mailroom:application"]
