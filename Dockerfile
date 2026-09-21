FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py media.py setup.py app_server.py app.html ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "app_server.py"]
