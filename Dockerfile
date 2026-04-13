# 1. Use Python 3.12
FROM python:3.12-slim

# 2. Standard settings: keeps Python from buffering output
ENV PYTHONUNBUFFERED=1

# 3. Create the app directory
WORKDIR /app

# Install the C compiler and build tools
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    cargo \
    rustc \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

# 4. Install dependencies
# We copy this first to leverage Docker caching (builds are faster later)
COPY requirements312.txt .
RUN pip install --no-cache-dir -r requirements312.txt

# 5. Copy application code
COPY . .

# 6. The Launch Command
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 web_interface.fyp_data_hub:app