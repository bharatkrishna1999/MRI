# Use lightweight Python image
FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable logs immediately
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Copy files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port (Render will override with $PORT anyway)
EXPOSE 10000

# Start the app using Render's dynamic port
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
