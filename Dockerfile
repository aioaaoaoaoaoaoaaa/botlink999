FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

WORKDIR /app

# Copy requirements và install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code
COPY kiemgao.py .

# Set environment variables
ENV PYTHONUNBUFFERED=1

CMD ["python", "kiemgao.py"]
