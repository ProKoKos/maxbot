FROM python:3.11-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire monorepo (shared/, db/, bot/, etc.)
COPY . .

CMD ["python", "-m", "bot.main"]
