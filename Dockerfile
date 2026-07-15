FROM python:3.12-slim

WORKDIR /app

COPY requirements_base.txt .
RUN pip install --no-cache-dir -r requirements_base.txt

COPY . .
RUN mkdir -p /tmp/api_archiver

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
